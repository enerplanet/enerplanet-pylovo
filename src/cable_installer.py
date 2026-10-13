"""Cable installation module for electrical grid generation."""

import numpy as np
import pandas as pd
from src.electrical_backend import IElectricalBackend, BusSpec, TransformerSpec, LineSpec, LoadSpec, ExtGridSpec
from src.config_loader import VN, V_BAND_LOW, VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM, VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM, SMALL_LOAD_THRESHOLD_KW, VOLTAGE_DROP_DISTRIBUTION_PERCENT, DEFAULT_POWER_FACTOR
from src import utils


class CableInstaller:
    """Handles cable installation for electrical grids using backend abstraction."""

    def __init__(self, backend: IElectricalBackend, dbc, logger, cables: list):
        """Initialize cable installer.

        Args:
            backend: Electrical backend instance (e.g., PandapowerBackend)
            dbc: Database client for accessing grid data
            logger: Logger instance
            cables: List of cable tuples from database (name, r_ohm_per_km, x_ohm_per_km, max_i_ka)
        """
        self.backend = backend
        self.dbc = dbc
        self.logger = logger

        # Cache cable data from database as DataFrame (single source of truth)
        self._cable_df = self._build_cable_dataframe(cables)

    @staticmethod
    def _build_cable_dataframe(cables: list) -> pd.DataFrame:
        """Build cable DataFrame from database tuples.

        Normalizes cable names to underscore format (e.g., "NAYY_4_120")
        which is compatible with both pandapower and OpenDSS backends.

        Args:
            cables: List of tuples (name, r_ohm_per_km, x_ohm_per_km, max_i_ka)

        Returns:
            DataFrame indexed by normalized cable name with electrical properties
        """
        from src.electrical_backend import normalize_cable_name
        cable_data = {}
        for name, r_ohm, x_ohm, max_i_ka in cables:
            normalized_name = normalize_cable_name(name)

            try:
                q_mm2 = int(name.split("_")[-1])
            except (ValueError, IndexError):
                q_mm2 = 0

            cable_data[normalized_name] = {
                'r_ohm_per_km': float(r_ohm),
                'x_ohm_per_km': float(x_ohm),
                'max_i_ka': float(max_i_ka),
                'q_mm2': q_mm2
            }

        return pd.DataFrame.from_dict(cable_data, orient="index")

    def create_lvmv_bus(self, plz: int, kcid: int, bcid: int, country_code: str | None = None) -> None:
        """Create LV and MV buses."""
        lv_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid, country_code)
        if lv_geodata is None:
            self.logger.error(f"Could not find transformer position for PLZ {plz}, KCID {kcid}, BCID {bcid}. "
                              f"Check if transformer_positions table is populated correctly.")
            return

        lv_bus_spec = BusSpec(
            name="LVbus 1",
            voltage_kv=VN * 1e-3,
            coordinates=lv_geodata
        )
        mv_geodata = (float(lv_geodata[0]), float(lv_geodata[1]) + 1.5 * 1e-4)
        mv_bus_spec = BusSpec(
            name="MVbus 1",
            voltage_kv=20.0,
            coordinates=mv_geodata
        )
        self.backend.create_component(lv_bus_spec)
        self.backend.create_component(mv_bus_spec)

        self.backend.create_component(ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1))

    def create_transformer(self, plz: int, kcid: int, bcid: int, country_code: str | None = None,
                           fixed_rating: bool = False) -> None:
        """
        Create a transformer based on the required rated power.

        Maps the required capacity to either a single standard transformer
        or a parallel configuration (2x) for specific larger loads. A fixed
        (known) rating is built as one unit of exactly that size.
        """
        transformer_rated_power = self.dbc.get_transformer_rated_power_from_bcid(plz, kcid, bcid, country_code)

        if fixed_rating:
            trafo_name = f"known {str(transformer_rated_power)} kva transformer"
            kva = transformer_rated_power
            parallel = 1
        elif transformer_rated_power in (100, 160, 250, 400, 630):
            trafo_name = f"single {str(transformer_rated_power)} kva transformer"
            kva = transformer_rated_power
            parallel = 1
        elif transformer_rated_power in (500, 800, 1260):
            trafo_name = f"double {str(transformer_rated_power * 0.5)} transformer"
            kva = transformer_rated_power * 0.5
            parallel = 2
        else:
            # Fallback: use 630 kVA transformers in parallel for large parallel transormers
            kva = 630
            parallel = max(1, int(np.ceil(float(transformer_rated_power) / 630.0)))
            trafo_name = f"{str(parallel)}-fold 630 transformer"

        trafo_spec = TransformerSpec(
            name=trafo_name,
            bus1="MVbus 1",
            bus2="LVbus 1",
            kva=kva,
            parallel=parallel,
            fixed_rating=fixed_rating,
        )

        self.backend.create_component(trafo_spec)
        # Backend rating is stored per physical transformer unit; parallel count carries the aggregate capacity.
        self.backend.set_transformer_rating(trafo_spec.name, kva * 1e-3)

    def create_connection_bus(self, connection_nodes: list):
        """Create connection buses."""
        node_geodata_map = self.dbc.get_nodes_geom_batch(connection_nodes)
        for node in connection_nodes:
            node_geodata = node_geodata_map.get(node) or self.dbc.get_node_geom(node)
            if not node_geodata:
                raise ValueError(f"Missing geometry for connection node {node}")
            bus_spec = BusSpec(
                name=f"Connection Nodebus {node}",
                voltage_kv=VN * 1e-3,
                coordinates=node_geodata,
            )
            self.backend.create_component(bus_spec)

    def create_consumer_bus_and_load(self, consumer_list: list, sim_load_per_building: dict, buildings_df: pd.DataFrame,
                                     load_type: dict) -> None:
        """Create consumer buses and loads with pre-diversified per-building power."""

        node_geodata_map = self.dbc.get_nodes_geom_batch(consumer_list)
        for consumer in consumer_list:
            node_geodata = node_geodata_map.get(consumer) or self.dbc.get_node_geom(consumer)
            if not node_geodata:
                raise ValueError(f"Missing geometry for consumer {consumer}")
            ltype = load_type[consumer]
            total_installed_kw = buildings_df[buildings_df["vertice_id"] == consumer]["peak_load_in_kw"].tolist()[0]
            simultaneous_load_kw = sim_load_per_building[consumer] * 1e3
            # Calculate reactive power from power factor
            phi = np.arccos(DEFAULT_POWER_FACTOR)
            kvar = simultaneous_load_kw * np.tan(phi)

            # Create bus
            bus_spec = BusSpec(
                name=f"Consumer Nodebus {consumer}",
                voltage_kv=VN * 1e-3,
                coordinates=node_geodata,
                zone=ltype
            )
            self.backend.create_component(bus_spec)
            self.backend.set_bus_zone(bus_spec.name, ltype)

            # Create one aggregated load per building with simultaneous load
            load_spec = LoadSpec(
                name=f"Load {consumer}",
                bus=f"Consumer Nodebus {consumer}",
                kw=simultaneous_load_kw,
                kvar=kvar,
                max_p_mw=total_installed_kw * 1e-3,
            )
            self.backend.create_component(load_spec)

    def install_consumer_cables(self, plz: int, bcid: int, kcid: int,
                                branch_deviation: float, branch_node_list: list,
                                ont_vertice: int, vertices_dict: dict, Pd: dict,
                                connection_available_cables: list[str],
                                local_length_dict: dict,
                                country_code: str | None = None) -> dict:
        """Install consumer connection cables."""
        # Normalize cable names from config to match internal format
        from src.electrical_backend import normalize_cable_name
        connection_available_cables = [normalize_cable_name(c) for c in connection_available_cables]

        consumer_list = self.dbc.get_vertices_from_connection_points(branch_node_list)
        branch_consumer_list = [n for n in consumer_list if n in vertices_dict.keys()]

        # Batch-fetch all paths from consumers to transformer in one pgr_Dijkstra call
        all_paths = self.dbc.get_paths_to_bus(branch_consumer_list, ont_vertice)
        edge_nodes = set()
        for vertice in branch_consumer_list:
            path_list = all_paths.get(vertice, [])
            if len(path_list) >= 2:
                edge_nodes.add(path_list[1])
                edge_nodes.add(path_list[0])
        edge_geodata_map = self.dbc.get_nodes_geom_batch(list(edge_nodes))

        for vertice in branch_consumer_list:
            path_list = all_paths.get(vertice, [])
            
            # Strict mode: skip consumers without a valid routed path.
            if len(path_list) < 2:
                self.logger.warning(
                    "Skipping consumer %s in PLZ %s (kcid=%s, bcid=%s): no routed path to transformer.",
                    vertice,
                    plz,
                    kcid,
                    bcid,
                )
                continue
            start_vid = path_list[1]
            end_vid = path_list[0]

            geodata = edge_geodata_map.get(start_vid) or self.dbc.get_node_geom(start_vid)
            start_node_geodata = (float(geodata[0]) + 5 * 1e-6 * branch_deviation,
                                  float(geodata[1]) + 5 * 1e-6 * branch_deviation)
            end_node_geodata = edge_geodata_map.get(end_vid) or self.dbc.get_node_geom(end_vid)
            line_geodata = [start_node_geodata, end_node_geodata]

            raw_length_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3
            if raw_length_km <= 0:
                self.logger.debug(
                    f"Non-positive consumer segment length for {start_vid}->{end_vid}: "
                    f"{raw_length_km:.6f} km. Using minimum length 0.001 km."
                )
            length_km = max(0.001, raw_length_km)
            count = 1
            sim_load = Pd.get(end_vid, 0)
            try:
                sim_load = float(sim_load)
            except (TypeError, ValueError):
                sim_load = 0.0
            if not np.isfinite(sim_load) or sim_load <= 0:
                self.logger.warning(
                    "Skipping consumer %s in PLZ %s (kcid=%s, bcid=%s): invalid simultaneous load=%s",
                    end_vid,
                    plz,
                    kcid,
                    bcid,
                    sim_load,
                )
                continue
            # sim_load is stored in MW; convert to current using apparent power.
            Imax = utils.required_line_current_ka(sim_load * 1e3, VN, DEFAULT_POWER_FACTOR, V_BAND_LOW)
            if not np.isfinite(Imax) or Imax <= 0:
                self.logger.warning(
                    "Skipping consumer %s in PLZ %s (kcid=%s, bcid=%s): invalid current Imax=%s",
                    end_vid,
                    plz,
                    kcid,
                    bcid,
                    Imax,
                )
                continue

            voltage_available_cables_df = None
            line_df = self._cable_df
            max_parallel = 128
            while count <= max_parallel:
                current_available_cables_df = line_df[
                    (line_df["max_i_ka"] >= Imax / count) & (line_df.index.isin(connection_available_cables))
                ].copy()

                if len(current_available_cables_df) == 0:
                    count += 1
                    continue

                current_available_cables_df["cable_impedence"] = np.sqrt(
                    current_available_cables_df["r_ohm_per_km"] ** 2 +
                    current_available_cables_df["x_ohm_per_km"] ** 2
                )

                if sim_load * 1e3 <= SMALL_LOAD_THRESHOLD_KW:
                    voltage_drop_limit = VN * VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM / 100
                else:
                    voltage_drop_limit = VN * VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM / 100

                denominator = Imax * length_km / count
                if denominator <= 0:
                    # Zero load (or degenerate segment) means no voltage-drop constraint is needed.
                    voltage_available_cables_df = current_available_cables_df
                else:
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"] <=
                        (voltage_drop_limit / denominator)
                    ]

                if len(voltage_available_cables_df) == 0:
                    count += 1
                    continue
                else:
                    break

            if voltage_available_cables_df is None or len(voltage_available_cables_df) == 0:
                self.logger.warning(
                    "No feasible consumer cable for %s in PLZ %s (kcid=%s, bcid=%s, Imax=%.6f kA, length=%.6f km) after %s parallels; skipping.",
                    end_vid,
                    plz,
                    kcid,
                    bcid,
                    Imax,
                    length_km,
                    max_parallel,
                )
                continue

            cable = voltage_available_cables_df.sort_values(by=["q_mm2"]).index.tolist()[0]
            local_length_dict[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Consumer Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
            )
            self.backend.create_component(line_spec)

            line_name = f"L{end_vid}"[:15]
            self.dbc.insert_lines(
                geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
                std_type=cable,
                from_bus=start_vid,
                to_bus=end_vid,
                length_km=length_km,
                country_code=country_code,
            )

        return local_length_dict

    def find_minimal_available_cable(self, Imax: float, distance: int = 0) -> tuple[str, int]:
        """Find the smallest cable that meets requirements."""
        if Imax is None or not np.isfinite(Imax) or Imax < 0:
            raise ValueError(f"Invalid Imax for cable sizing: {Imax}")
        count = 1
        cable = None
        line_df = self._cable_df
        max_parallel = 256

        while count <= max_parallel:
            current_available_cables = line_df[(line_df["max_i_ka"] >= Imax / count)]

            if len(current_available_cables) == 0:
                count += 1
                continue

            if distance != 0 and Imax != 0:
                current_available_cables["cable_impedence"] = np.sqrt(
                    current_available_cables["r_ohm_per_km"] ** 2 +
                    current_available_cables["x_ohm_per_km"] ** 2
                )

                voltage_available_cables = current_available_cables[
                    current_available_cables["cable_impedence"] <=
                    VN * VOLTAGE_DROP_DISTRIBUTION_PERCENT / 100 / (np.sqrt(3) * Imax * distance / count)
                ]

                if len(voltage_available_cables) == 0:
                    count += 1
                    continue
                else:
                    cable = voltage_available_cables.sort_values(by=["q_mm2"]).index.tolist()[0]
                    break
            else:
                cable = current_available_cables.sort_values(by=["q_mm2"]).index.tolist()[0]
                break

        if cable is None:
            raise ValueError(
                f"Could not find feasible cable for Imax={Imax}, distance={distance} after {max_parallel} parallels"
            )

        return cable, count

    def create_line_ont_to_lv_bus(self, plz: int, bcid: int, kcid: int,
                                   branch_start_node: int, branch_deviation: float,
                                   cable: str, count: int, ont_vertice: int,
                                   country_code: str | None = None):
        """Create line from transformer to connection node."""
        end_vid = branch_start_node
        node_geodata = self.dbc.get_node_geom(end_vid)
        node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                        float(node_geodata[1]) + 5 * 1e-6 * branch_deviation)

        coords = self.backend.get_bus_coordinates("LVbus 1")
        if coords:
            lvbus_geodata = (coords[0] + 5 * 1e-6 * branch_deviation, coords[1])
        else:
            lv_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid, country_code)
            lvbus_geodata = (float(lv_geodata[0]) + 5 * 1e-6 * branch_deviation, float(lv_geodata[1]))
        line_geodata = [lvbus_geodata, node_geodata]
        # When branch starts at transformer, use 1 meter minimum to avoid zero-impedance
        length_km = 0.001

        line_spec = LineSpec(
            name=f"Line to {end_vid}",
            bus1="LVbus 1",
            bus2=f"Connection Nodebus {end_vid}",
            cable_name=cable,
            length_km=length_km,
            parallel=count,
            coordinates=line_geodata,
        )
        self.backend.create_component(line_spec)

        line_name = f"L{end_vid}"[:15]
        self.dbc.insert_lines(
            geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
            std_type=cable,
            from_bus=ont_vertice,  # Use vertex ID directly (backend-agnostic)
            to_bus=end_vid,
            length_km=length_km,
            country_code=country_code,
        )

    def create_line_start_to_lv_bus(self, plz: int, bcid: int, kcid: int,
                                     branch_start_node: int, branch_deviation: float,
                                     vertices_dict: dict, cable: str, count: int,
                                     ont_vertice: int, country_code: str | None = None) -> int:
        """Create line from branch start to LV bus."""
        node_path_list = self.dbc.get_path_to_bus(branch_start_node, ont_vertice)
        node_geodata_map = self.dbc.get_nodes_geom_batch(node_path_list)

        line_geodata = []
        for p in node_path_list:
            node_geodata = node_geodata_map.get(p) or self.dbc.get_node_geom(p)
            node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                            float(node_geodata[1]) + 5 * 1e-6 * branch_deviation)
            line_geodata.append(node_geodata)

        coords = self.backend.get_bus_coordinates("LVbus 1")
        if coords:
            lvbus_geodata = (coords[0] + 5 * 1e-6 * branch_deviation, coords[1])
        else:
            # Fallback to database (for backends without geodata)
            lv_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid, country_code)
            lvbus_geodata = (float(lv_geodata[0]) + 5 * 1e-6 * branch_deviation, float(lv_geodata[1]))
        line_geodata.append(lvbus_geodata)
        line_geodata.reverse()

        length_km = vertices_dict[branch_start_node] * 1e-3
        length = count * length_km

        line_spec = LineSpec(
            name=f"Line to {branch_start_node}",
            bus1="LVbus 1",
            bus2=f"Connection Nodebus {branch_start_node}",
            cable_name=cable,
            length_km=length_km,
            parallel=count,
            coordinates=line_geodata,
        )
        self.backend.create_component(line_spec)

        line_name = f"L{branch_start_node}"[:15]
        self.dbc.insert_lines(
            geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
            std_type=cable,
            from_bus=ont_vertice,  # Use vertex ID directly (backend-agnostic)
            to_bus=branch_start_node,
            length_km=length_km,
            country_code=country_code,
        )

        return length

    def deviate_bus_geodata(self, branch_node_list: list, branch_deviation: float):
        """Update bus geodata for visualization (no-op for backends without geodata)."""
        offset = 5 * 1e-6 * branch_deviation
        for node in branch_node_list:
            bus_name = f"Connection Nodebus {node}"
            coords = self.backend.get_bus_coordinates(bus_name)
            if coords:
                self.backend.set_bus_coordinates(bus_name, coords[0] + offset, coords[1] + offset)

    def create_line_node_to_node(self, plz: int, kcid: int, bcid: int,
                                  branch_node_list: list, branch_deviation: float,
                                  vertices_dict: dict, local_length_dict: dict,
                                  cable: str, ont_vertice: int, count: float,
                                  country_code: str | None = None) -> dict:
        """Create lines between connection nodes."""
        # Batch-fetch all paths from branch nodes to transformer
        all_paths = self.dbc.get_paths_to_bus(branch_node_list, ont_vertice)
        node_geodata_cache = {}

        for i in range(len(branch_node_list) - 1):
            node_path_list = all_paths.get(branch_node_list[i], [])

            if branch_node_list[i + 1] not in node_path_list:
                node_path_list = self.dbc.get_path_to_bus(branch_node_list[i], branch_node_list[i + 1])

            node_path_list = node_path_list[: node_path_list.index(branch_node_list[i + 1]) + 1]
            node_path_list.reverse()

            start_vid = node_path_list[0]
            end_vid = node_path_list[-1]

            missing_nodes = [p for p in node_path_list if p not in node_geodata_cache]
            if missing_nodes:
                node_geodata_cache.update(self.dbc.get_nodes_geom_batch(missing_nodes))
            line_geodata = []
            for p in node_path_list:
                node_geodata = node_geodata_cache.get(p) or self.dbc.get_node_geom(p)
                node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                                float(node_geodata[1]) + 5 * 1e-6 * branch_deviation)
                line_geodata.append(node_geodata)

            raw_length_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3
            if raw_length_km <= 0:
                self.logger.debug(
                    f"Non-positive branch segment length for {start_vid}->{end_vid}: "
                    f"{raw_length_km:.6f} km. Using minimum length 0.001 km."
                )
            length_km = max(0.001, raw_length_km)
            local_length_dict[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Connection Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
            )
            self.backend.create_component(line_spec)

            line_name = f"L{end_vid}"[:15]
            self.dbc.insert_lines(
                geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
                std_type=cable,
                from_bus=start_vid,  # Use vertex ID directly (backend-agnostic)
                to_bus=end_vid,
                length_km=length_km,
                country_code=country_code,
            )

        return local_length_dict

    def _install_direct_cable(self, plz: int, bcid: int, kcid: int,
                               vertice: int, ont_vertice: int, vertices_dict: dict,
                               Pd: dict, connection_available_cables: list[str],
                               local_length_dict: dict, branch_deviation: float,
                               country_code: str | None = None) -> dict:
        """Install direct straight-line cable for unreachable buildings.
        
        When a building cannot be reached via the road network (pgr_dijkstra returns no path),
        this method creates a direct cable connection from the building to the transformer.
        
        Args:
            plz, bcid, kcid: Grid identifiers
            vertice: The unreachable building vertex ID
            ont_vertice: Transformer vertex ID
            vertices_dict: Dict of vertex -> cost (includes straight-line distances)
            Pd: Dict of vertex -> load
            connection_available_cables: List of allowed cable types
            local_length_dict: Running total of cable lengths by type
            branch_deviation: Offset for visual separation
        
        Returns:
            Updated local_length_dict
        """
        # Get geometry for straight-line connection
        end_vid = vertice
        end_node_geodata = self.dbc.get_node_geom(end_vid)
        ont_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid, country_code)
        
        if not end_node_geodata or not ont_geodata:
            self.logger.warning(f"Cannot install direct cable for vertex {vertice}: missing geometry")
            return local_length_dict
        
        start_node_geodata = (float(ont_geodata[0]) + 5 * 1e-6 * branch_deviation,
                              float(ont_geodata[1]) + 5 * 1e-6 * branch_deviation)
        line_geodata = [start_node_geodata, end_node_geodata]
        
        # Use straight-line distance (already calculated in vertices_dict with penalty)
        # Convert from km back to meters, then to km
        length_km = max(0.001, vertices_dict.get(end_vid, 0.1))  # minimum 1m
        
        # Calculate cable sizing based on load
        sim_load = Pd.get(end_vid, 0.001)  # Default 0.001 MW (1 kW) if not found
        # sim_load is stored in MW; convert to current using apparent power.
        Imax = utils.required_line_current_ka(sim_load * 1e3, VN, DEFAULT_POWER_FACTOR, V_BAND_LOW)
        
        # Find suitable cable
        count = 1
        line_df = self._cable_df
        while True:
            current_available_cables_df = line_df[
                (line_df["max_i_ka"] >= Imax / count) & (line_df.index.isin(connection_available_cables))
            ]
            
            if len(current_available_cables_df) == 0:
                count += 1
                if count > 10:  # Safety limit
                    self.logger.warning(f"Cannot find suitable cable for direct connection to {vertice}")
                    return local_length_dict
                continue
            
            cable = current_available_cables_df.sort_values(by=["q_mm2"]).index.tolist()[0]
            break
        
        local_length_dict[cable] += count * length_km
        self.logger.debug(f"Installing direct cable to unreachable building {vertice}: {cable} x{count}, {length_km:.3f}km")
        
        line_spec = LineSpec(
            name=f"Direct Line to {end_vid}",
            bus1="LVbus 1",
            bus2=f"Consumer Nodebus {end_vid}",
            cable_name=cable,
            length_km=length_km,
            parallel=count,
            coordinates=line_geodata,
        )
        self.backend.create_component(line_spec)
        
        line_name = f"D{end_vid}"[:15]  # 'D' for direct
        self.dbc.insert_lines(
            geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
            std_type=cable,
            from_bus=ont_vertice,
            to_bus=end_vid,
            length_km=length_km,
            country_code=country_code,
        )
        
        return local_length_dict
