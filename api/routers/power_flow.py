"""Power flow and hosting capacity endpoints."""
import math
import re
import traceback

from fastapi import APIRouter, HTTPException

from models import PowerFlowRequest, HostingCapacityRequest
from src.database.database_client import DatabaseClient
from src.ev_hosting import calculate_hosting_capacity

router = APIRouter(tags=["power_flow"])

# Cable type mapping from DB std_type to pandapower standard types
CABLE_TYPE_MAP = {
    'NAYY 4x50': 'NAYY 4x50 SE',
    'NAYY 4x95': 'NAYY 4x95',
    'NAYY 4x120': 'NAYY 4x120 SE',
    'NAYY 4x150': 'NAYY 4x150 SE',
    'NAYY 4x185': 'NAYY 4x185',
    'NAYY 4x240': 'NAYY 4x240',
    'NYY 4x50': 'NYY 4x50',
    'NYY 4x95': 'NYY 4x95',
    'NYY 4x120': 'NYY 4x120',
    'NYY 4x150': 'NYY 4x150',
}

# Cable parameters fallback (r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka)
CABLE_PARAMS = {
    'NAYY 4x50': (0.641, 0.083, 210, 0.142),
    'NAYY 4x95': (0.320, 0.080, 240, 0.200),
    'NAYY 4x120': (0.253, 0.080, 250, 0.230),
    'NAYY 4x150': (0.206, 0.080, 260, 0.270),
    'NAYY 4x185': (0.164, 0.080, 270, 0.310),
    'NAYY 4x240': (0.125, 0.079, 280, 0.360),
    'default': (0.206, 0.080, 260, 0.270),
}

# Power factor by building type (cos_phi)
POWER_FACTOR_MAP = {
    'residential': 0.97,
    'house': 0.97,
    'apartments': 0.97,
    'terrace': 0.97,
    'apartment': 0.97,
    'dormitory': 0.97,
    'commercial': 0.92,
    'office': 0.92,
    'retail': 0.90,
    'shop': 0.90,
    'hotel': 0.90,
    'restaurant': 0.88,
    'industrial': 0.85,
    'factory': 0.85,
    'warehouse': 0.90,
    'public': 0.93,
    'school': 0.93,
    'hospital': 0.88,
    'agricultural': 0.85,
}


def _build_scope_filter(user_id, model_id, draft_id, alias: str) -> tuple[str, list]:
    """Build deterministic scope filter (model -> draft, with optional user)."""
    clauses = []
    params = []

    if model_id is not None:
        clauses.append(f"{alias}.model_id = %s")
        params.append(model_id)
    elif draft_id:
        clauses.append(f"{alias}.draft_id = %s")
        params.append(draft_id)

    if user_id:
        clauses.append(f"{alias}.user_id = %s")
        params.append(user_id)

    return " AND ".join(clauses), params


def _to_int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_cable_key(raw_name):
    """Normalize DB cable names to canonical keys used in local maps."""
    if not raw_name:
        return None

    normalized = " ".join(str(raw_name).replace("_", " ").upper().split())
    match = re.search(r"\b(NAYY|NYY)\s*4X?\s*(50|95|120|150|185|240)\b", normalized)
    if match:
        return f"{match.group(1)} 4x{match.group(2)}"

    return str(raw_name).strip()


@router.post("/hosting-capacity")
async def get_hosting_capacity(payload: HostingCapacityRequest):
    """
    Calculate EV hosting capacity for a given node/transformer area.

    Uses multi-constraint analysis per paper methodology:
    - Transformer thermal limit
    - Voltage drop limit (undervoltage for EV charging)
    - Cable thermal limit

    Returns the most limiting factor as the hosting capacity.
    """
    try:
        result = calculate_hosting_capacity(
            transformer_capacity_kva=payload.transformer_capacity_kva,
            current_peak_load_kw=payload.current_peak_load_kw,
            charger_power_kw=payload.charger_power_kw,
            simultaneity_factor=payload.simultaneity_factor,
            nominal_voltage_v=payload.nominal_voltage_v,
            voltage_limit_pu=payload.voltage_limit_pu,
            cable_impedance_ohm_per_km=payload.cable_impedance_ohm_per_km,
            cable_length_km=payload.cable_length_km,
            cable_max_current_a=payload.cable_max_current_a
        )
        return {
            "status": "success",
            "data": result
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/power-flow")
async def run_power_flow(payload: PowerFlowRequest):
    """Run power flow analysis on a grid using pandapower.

    Builds the pandapower network dynamically from stored grid data (lines, transformers, loads).
    """
    try:
        import pandapower as pp
        from collections import Counter
        from pandapower.topology import unsupplied_buses

        with DatabaseClient() as dbc:
            grid_id = payload.grid_result_id
            load_scaling = payload.load_scaling if payload.load_scaling is not None else 1.0

            # Get transformer info for this grid
            trafo_query = """
                SELECT gr.transformer_rated_power,
                       gr.ont_vertice_id,
                       ST_X(ST_Transform(tp.geom, 4326)) as lon,
                       ST_Y(ST_Transform(tp.geom, 4326)) as lat
                FROM grid_result gr
                JOIN transformer_positions tp ON tp.grid_result_id = gr.grid_result_id
                WHERE gr.grid_result_id = %s;
            """
            dbc.cur.execute(trafo_query, (grid_id,))
            trafo_row = dbc.cur.fetchone()

            if not trafo_row:
                raise HTTPException(status_code=404, detail=f"Grid {grid_id} not found")

            trafo_kva = trafo_row[0] or 400
            ont_vertice_id = _to_int_or_none(trafo_row[1])
            trafo_lon, trafo_lat = trafo_row[2], trafo_row[3]

            # Get buildings/loads for this grid
            building_filter = payload.building_osm_ids
            assignment_filter, assignment_params = _build_scope_filter(
                user_id=payload.user_id,
                model_id=payload.model_id,
                draft_id=payload.draft_id,
                alias="bta",
            )

            if assignment_filter:
                loads_where = f"""
                    (br.grid_result_id = %s
                     OR br.osm_id IN (
                         SELECT bta.building_osm_id FROM building_transformer_assignments bta
                         WHERE bta.grid_result_id = %s
                           AND {assignment_filter}
                     ))
                """
                loads_params = [grid_id, grid_id] + assignment_params
            else:
                loads_where = "br.grid_result_id = %s"
                loads_params = [grid_id]

            loads_query = f"""
                SELECT br.osm_id, br.type, br.peak_load_in_kw, br.area,
                       br.vertice_id, br.connection_point,
                       ST_X(ST_Transform(ST_Centroid(br.geom), 4326)) as lon,
                       ST_Y(ST_Transform(ST_Centroid(br.geom), 4326)) as lat
                FROM buildings_result br
                WHERE {loads_where}
            """
            if building_filter and len(building_filter) > 0:
                loads_query += " AND br.osm_id = ANY(%s)"
                loads_params.append(building_filter)

            dbc.cur.execute(loads_query, loads_params)
            loads_rows = dbc.cur.fetchall()

            print(f"[Power Flow] Grid {grid_id}: {len(loads_rows)} buildings loaded" +
                  (f" (filtered from {len(building_filter)} requested)" if building_filter else ""))

            # Get all lines for this grid
            lines_query = """
                SELECT lines_result_id, line_name, std_type, from_bus, to_bus, length_km
                FROM lines_result
                WHERE grid_result_id = %s;
            """
            dbc.cur.execute(lines_query, (grid_id,))
            lines_rows = dbc.cur.fetchall()

        # Get voltage limits and convergence settings
        min_vm_pu = payload.min_vm_pu if payload.min_vm_pu is not None else 0.9
        max_vm_pu = payload.max_vm_pu if payload.max_vm_pu is not None else 1.1
        max_iterations = payload.max_iterations if payload.max_iterations is not None else 50
        tolerance = payload.tolerance if payload.tolerance is not None else 1e-6

        print(f"[Power Flow] Grid {grid_id}: {len(lines_rows)} lines, {len(loads_rows)} loads")
        print(f"[Power Flow] Settings: vm_pu=[{min_vm_pu}, {max_vm_pu}], max_iter={max_iterations}, tol={tolerance}")

        # Build pandapower network
        net = pp.create_empty_network(name=f"Grid_{grid_id}")

        # Create MV bus (external grid connection point) - 20 kV
        mv_bus = pp.create_bus(net, name="MV_Bus", vn_kv=20.0, geodata=(trafo_lon, trafo_lat))

        # Create external grid at MV bus
        pp.create_ext_grid(net, bus=mv_bus, vm_pu=1.0, name="External_Grid")

        # Create LV bus at transformer secondary - 0.4 kV
        lv_bus = pp.create_bus(net, name="LV_Main", vn_kv=0.4, geodata=(trafo_lon, trafo_lat))

        # Create transformer (MV/LV)
        pp.create_transformer_from_parameters(
            net,
            hv_bus=mv_bus,
            lv_bus=lv_bus,
            name=f"Trafo_{grid_id}",
            sn_mva=trafo_kva / 1000.0,
            vn_hv_kv=20.0,
            vn_lv_kv=0.4,
            vkr_percent=1.0,
            vk_percent=4.0,
            pfe_kw=0.5,
            i0_percent=0.3,
            tap_pos=0,
            tap_neutral=0,
            tap_min=-2,
            tap_max=2,
            tap_step_percent=2.5
        )

        # Create a bus mapping for line connections
        connection_counts = Counter()
        unique_buses = set()
        for line_row in lines_rows:
            from_bus = _to_int_or_none(line_row[3])
            to_bus = _to_int_or_none(line_row[4])
            if from_bus is not None:
                unique_buses.add(from_bus)
                connection_counts[from_bus] += 1
            if to_bus is not None:
                unique_buses.add(to_bus)
                connection_counts[to_bus] += 1

        auto_root_bus_id = max(connection_counts, key=connection_counts.get) if connection_counts else None
        root_bus_id = ont_vertice_id if ont_vertice_id in unique_buses else auto_root_bus_id

        bus_map = {}
        if root_bus_id is not None:
            bus_map[root_bus_id] = lv_bus

        for bus_id in unique_buses:
            if bus_id not in bus_map:
                bus_map[bus_id] = pp.create_bus(net, name=f"Bus_{bus_id}", vn_kv=0.4)

        # Create lines with standard cable types
        pp_to_db_line_id = {}

        for line_row in lines_rows:
            line_id, line_name, std_type, from_bus_id_raw, to_bus_id_raw, length_km = line_row
            from_bus_id = _to_int_or_none(from_bus_id_raw)
            to_bus_id = _to_int_or_none(to_bus_id_raw)

            from_bus_pp = bus_map.get(from_bus_id, lv_bus)
            to_bus_pp = bus_map.get(to_bus_id, lv_bus)

            if from_bus_pp == to_bus_pp:
                continue

            db_cable = _normalize_cable_key(std_type)
            pp_std_type = CABLE_TYPE_MAP.get(db_cable, 'NAYY 4x150 SE')

            try:
                pp_idx = pp.create_line(
                    net,
                    from_bus=from_bus_pp,
                    to_bus=to_bus_pp,
                    length_km=max(length_km or 0.01, 0.001),
                    std_type=pp_std_type,
                    name=line_name or f"Line_{line_id}"
                )
                pp_to_db_line_id[pp_idx] = line_id
            except Exception:
                params = CABLE_PARAMS.get(db_cable, CABLE_PARAMS['default'])
                pp_idx = pp.create_line_from_parameters(
                    net,
                    from_bus=from_bus_pp,
                    to_bus=to_bus_pp,
                    length_km=max(length_km or 0.01, 0.001),
                    r_ohm_per_km=params[0],
                    x_ohm_per_km=params[1],
                    c_nf_per_km=params[2],
                    max_i_ka=params[3],
                    name=line_name or f"Line_{line_id}"
                )
                pp_to_db_line_id[pp_idx] = line_id

        # Find leaf buses
        bus_degrees = {}
        for _, row in net.line.iterrows():
            from_b, to_b = row['from_bus'], row['to_bus']
            bus_degrees[from_b] = bus_degrees.get(from_b, 0) + 1
            bus_degrees[to_b] = bus_degrees.get(to_b, 0) + 1

        leaf_buses = [b for b, deg in bus_degrees.items() if deg == 1 and b != lv_bus]

        if not leaf_buses:
            leaf_buses = [b for b in bus_degrees.keys() if b != lv_bus]

        if not leaf_buses:
            leaf_buses = [lv_bus]

        # Create loads from buildings
        total_load_mw = 0
        mapped_loads = 0
        fallback_loads = 0
        fallback_load_cursor = 0
        for load_row in loads_rows:
            osm_id, bldg_type, peak_load_kw, area = load_row[0], load_row[1], load_row[2], load_row[3]
            vertice_id = _to_int_or_none(load_row[4])
            connection_point = _to_int_or_none(load_row[5])

            if peak_load_kw is None or peak_load_kw <= 0:
                peak_load_kw = (area or 100) * 0.03

            peak_mw = peak_load_kw / 1000.0

            bldg_type_lower = (bldg_type or 'residential').lower()
            cos_phi = POWER_FACTOR_MAP.get(bldg_type_lower, 0.95)
            tan_phi = math.tan(math.acos(cos_phi))
            q_mvar = peak_mw * tan_phi

            load_bus = None
            for candidate in (connection_point, vertice_id):
                if candidate is not None and candidate in bus_map:
                    load_bus = bus_map[candidate]
                    mapped_loads += 1
                    break

            if load_bus is None:
                load_bus = leaf_buses[fallback_load_cursor % len(leaf_buses)]
                fallback_load_cursor += 1
                fallback_loads += 1

            pp.create_load(
                net,
                bus=load_bus,
                p_mw=peak_mw * load_scaling,
                q_mvar=q_mvar * load_scaling,
                name=f"Load_{osm_id}" if osm_id else f"Load_{bldg_type}"
            )
            total_load_mw += peak_mw

        print(
            f"[Power Flow] Load placement: mapped={mapped_loads}, "
            f"fallback={fallback_loads}, ont_root={ont_vertice_id}, selected_root={root_bus_id}"
        )

        if len(net.load) == 0:
            pp.create_load(net, bus=lv_bus, p_mw=0.001 * load_scaling, q_mvar=0.0003 * load_scaling, name="Min_Load")

        # Check for isolated buses
        try:
            isolated = unsupplied_buses(net)
            if len(isolated) > 0:
                loads_to_drop = net.load[net.load.bus.isin(isolated)].index
                net.load = net.load.drop(loads_to_drop)
                pp.drop_buses(net, isolated)
        except Exception as e:
            print(f"Warning: Could not check for isolated buses: {e}")

        # Set voltage limits
        net.bus['min_vm_pu'] = min_vm_pu
        net.bus['max_vm_pu'] = max_vm_pu
        net.bus['vm_pu'] = 1.0
        net.bus['va_degree'] = 0.0

        # Run power flow with fallback algorithms
        converged = False
        algorithms = ['nr', 'bfsw', 'gs']

        for algo in algorithms:
            try:
                pp.runpp(net, algorithm=algo, calculate_voltage_angles=True, max_iteration=max_iterations,
                        tolerance_mva=tolerance, init='flat')
                converged = True
                break
            except Exception as e:
                print(f"Power flow with {algo} failed: {e}")
                continue

        if not converged:
            trafo_capacity_mw = trafo_kva / 1000.0
            total_load = float(net.load['p_mw'].sum()) if len(net.load) > 0 else 0
            load_ratio = total_load / trafo_capacity_mw if trafo_capacity_mw > 0 else 0

            estimated_loading = min(load_ratio * 100, 999)
            estimated_line_results = []
            for idx in net.line.index:
                line_name = net.line.at[idx, 'name']
                estimated_line_results.append({
                    "line_id": int(idx),
                    "lines_result_id": pp_to_db_line_id.get(idx),
                    "name": line_name,
                    "loading_percent": round(estimated_loading, 2),
                    "i_ka": 0,
                    "p_from_mw": 0,
                    "p_to_mw": 0,
                    "pl_mw": 0,
                    "ql_mvar": 0,
                    "estimated": True
                })

            return {
                "status": "warning",
                "converged": False,
                "grid_result_id": grid_id,
                "load_scaling": load_scaling,
                "message": "Power flow did not converge. Network may have topology issues.",
                "network_info": {
                    "buses": len(net.bus),
                    "lines": len(net.line),
                    "loads": len(net.load),
                    "total_load_mw": total_load,
                    "trafo_capacity_mw": trafo_capacity_mw,
                    "load_to_capacity_ratio": round(load_ratio, 2),
                    "root_bus_id": root_bus_id,
                    "ont_vertice_id": ont_vertice_id,
                    "unique_buses_in_db": len(unique_buses),
                    "leaf_buses_count": len(leaf_buses),
                    "loads_mapped_to_bus": mapped_loads,
                    "loads_fallback_assigned": fallback_loads,
                    "lines_in_db": len(lines_rows),
                    "loads_in_db": len(loads_rows)
                },
                "summary": {
                    "min_voltage_pu": 0,
                    "max_voltage_pu": 0,
                    "max_line_loading_percent": round(estimated_loading, 2),
                    "max_trafo_loading_percent": round(estimated_loading, 2),
                    "total_losses_kw": 0,
                    "voltage_violations_count": 0,
                    "overloaded_lines_count": len(estimated_line_results)
                },
                "violations": {
                    "voltage": [],
                    "overloaded_lines": [{"line_id": l["line_id"], "name": l["name"], "loading_percent": l["loading_percent"]} for l in estimated_line_results[:10]]
                },
                "results": {
                    "buses": [],
                    "lines": estimated_line_results,
                    "transformers": [{
                        "trafo_id": 0,
                        "name": f"Trafo_{grid_id}",
                        "loading_percent": round(estimated_loading, 2),
                        "i_hv_ka": 0,
                        "i_lv_ka": 0,
                        "p_hv_mw": 0,
                        "p_lv_mw": total_load,
                        "estimated": True
                    }]
                }
            }

        # Extract results
        bus_results = []
        for idx, row in net.res_bus.iterrows():
            bus_name = net.bus.at[idx, 'name'] if idx in net.bus.index else f"Bus {idx}"
            bus_results.append({
                "bus_id": int(idx),
                "name": bus_name,
                "vm_pu": round(row['vm_pu'], 4),
                "va_degree": round(row['va_degree'], 2),
                "p_mw": round(row['p_mw'], 6),
                "q_mvar": round(row['q_mvar'], 6)
            })

        line_results = []
        for idx, row in net.res_line.iterrows():
            line_name = net.line.at[idx, 'name'] if idx in net.line.index else f"Line {idx}"
            line_results.append({
                "line_id": int(idx),
                "lines_result_id": pp_to_db_line_id.get(idx),
                "name": line_name,
                "loading_percent": round(row['loading_percent'], 2),
                "i_ka": round(row['i_ka'], 4),
                "p_from_mw": round(row['p_from_mw'], 6),
                "p_to_mw": round(row['p_to_mw'], 6),
                "pl_mw": round(row['pl_mw'], 8),
                "ql_mvar": round(row['ql_mvar'], 8)
            })

        trafo_results = []
        if len(net.res_trafo) > 0:
            for idx, row in net.res_trafo.iterrows():
                trafo_name = net.trafo.at[idx, 'name'] if idx in net.trafo.index else f"Trafo {idx}"
                trafo_results.append({
                    "trafo_id": int(idx),
                    "name": trafo_name,
                    "loading_percent": round(row['loading_percent'], 2),
                    "i_hv_ka": round(row['i_hv_ka'], 4),
                    "i_lv_ka": round(row['i_lv_ka'], 4),
                    "p_hv_mw": round(row['p_hv_mw'], 6),
                    "p_lv_mw": round(row['p_lv_mw'], 6),
                    "pl_mw": round(row['pl_mw'], 8)
                })

        # Calculate summary statistics
        min_voltage = net.res_bus['vm_pu'].min()
        max_voltage = net.res_bus['vm_pu'].max()
        max_line_loading = net.res_line['loading_percent'].max() if len(net.res_line) > 0 else 0
        max_trafo_loading = net.res_trafo['loading_percent'].max() if len(net.res_trafo) > 0 else 0
        total_losses_mw = net.res_line['pl_mw'].sum() if len(net.res_line) > 0 else 0

        # Check for violations
        voltage_violations = []
        for idx, row in net.res_bus.iterrows():
            if row['vm_pu'] < min_vm_pu or row['vm_pu'] > max_vm_pu:
                bus_name = net.bus.at[idx, 'name'] if idx in net.bus.index else f"Bus {idx}"
                voltage_violations.append({
                    "bus_id": int(idx),
                    "name": bus_name,
                    "vm_pu": round(row['vm_pu'], 4),
                    "violation": "undervoltage" if row['vm_pu'] < min_vm_pu else "overvoltage",
                    "limit": min_vm_pu if row['vm_pu'] < min_vm_pu else max_vm_pu
                })

        overloaded_lines = []
        for idx, row in net.res_line.iterrows():
            if row['loading_percent'] > 100:
                line_name = net.line.at[idx, 'name'] if idx in net.line.index else f"Line {idx}"
                overloaded_lines.append({
                    "line_id": int(idx),
                    "name": line_name,
                    "loading_percent": round(row['loading_percent'], 2)
                })

        return {
            "status": "success",
            "grid_result_id": grid_id,
            "load_scaling": load_scaling,
            "converged": net.converged,
            "settings": {
                "min_vm_pu": min_vm_pu,
                "max_vm_pu": max_vm_pu,
                "max_iterations": max_iterations,
                "tolerance": tolerance
            },
            "summary": {
                "min_voltage_pu": round(min_voltage, 4),
                "max_voltage_pu": round(max_voltage, 4),
                "max_line_loading_percent": round(max_line_loading, 2),
                "max_trafo_loading_percent": round(max_trafo_loading, 2),
                "total_losses_kw": round(total_losses_mw * 1000, 4),
                "voltage_violations_count": len(voltage_violations),
                "overloaded_lines_count": len(overloaded_lines)
            },
            "violations": {
                "voltage": voltage_violations,
                "overloaded_lines": overloaded_lines
            },
            "results": {
                "buses": bus_results,
                "lines": line_results,
                "transformers": trafo_results
            }
        }

    except ImportError:
        raise HTTPException(status_code=500, detail="pandapower not installed")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
