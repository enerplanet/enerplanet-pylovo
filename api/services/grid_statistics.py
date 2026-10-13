"""Statistics for /grid-statistics: whole grids, or the part of them serving selected buildings."""
from collections import defaultdict

from src.config_loader import CABLE_COST_DICT, VN, V_BAND_LOW, V_BAND_HIGH

DEFAULT_CABLE_COST_PER_M = 20

BUILDINGS_SQL = """
    SELECT osm_id, grid_result_id, area, type, peak_load_in_kw, vertice_id, connection_point
    FROM buildings_result
    WHERE grid_result_id = ANY(%s);
"""

LINES_SQL = """
    SELECT lines_result_id, grid_result_id, std_type, from_bus, to_bus, length_km
    FROM lines_result
    WHERE grid_result_id = ANY(%s);
"""

TRANSFORMERS_SQL = """
    SELECT gr.grid_result_id, gr.transformer_rated_power, gr.ont_vertice_id
    FROM transformer_positions tp
    JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
    WHERE tp.grid_result_id = ANY(%s);
"""

TRANSFORMER_COST_SQL = """
    SELECT s_max_kva, COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur)
    FROM equipment_data
    WHERE typ = 'Transformer' AND s_max_kva IS NOT NULL;
"""


def _fetch(cur, sql: str, grid_ids: list, keys: tuple) -> list[dict]:
    """Run sql for grid_ids and return its rows as dicts keyed by keys, in column order."""
    cur.execute(sql, (grid_ids,))
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def fetch_grid_rows(cur, grid_ids: list) -> dict:
    """Read the buildings, lines and transformers of grid_ids plus the transformer cost table."""
    rows = {
        "buildings": _fetch(cur, BUILDINGS_SQL, grid_ids, (
            "osm_id", "grid_result_id", "area", "type", "peak_load_in_kw", "vertice_id", "connection_point")),
        "lines": _fetch(cur, LINES_SQL, grid_ids, (
            "lines_result_id", "grid_result_id", "std_type", "from_bus", "to_bus", "length_km")),
        "transformers": _fetch(cur, TRANSFORMERS_SQL, grid_ids, ("grid_result_id", "rated_power", "ont_vertice_id")),
    }
    cur.execute(TRANSFORMER_COST_SQL)
    rows["transformer_costs"] = {int(kva): float(cost) for kva, cost in cur.fetchall()}
    return rows


def cable_paths(buildings: list[dict], lines: list[dict], transformers: list[dict]) -> list[dict]:
    """Return the lines from each building back to its transformer bus, each line once.

    Lines run from the transformer side (from_bus) to the consumer side (to_bus),
    and a bus is the to_bus of at most one line per grid (src/cable_installer.py).
    The 1 m from_bus = to_bus stubs at the transformer bus are on no building's path.
    """
    parent = {(l["grid_result_id"], l["to_bus"]): l for l in lines if l["from_bus"] != l["to_bus"]}
    roots = {t["grid_result_id"]: t["ont_vertice_id"] for t in transformers}
    picked = {}
    for building in buildings:
        grid_id = building["grid_result_id"]
        bus = building["vertice_id"]
        if (grid_id, bus) not in parent:
            bus = building["connection_point"]
        visited = set()
        while (grid_id, bus) in parent and bus != roots.get(grid_id) and bus not in visited:
            visited.add(bus)
            line = parent[(grid_id, bus)]
            picked[line["lines_result_id"]] = line
            bus = line["from_bus"]
    return list(picked.values())


def _sum(values) -> float:
    """Sum the non-null values, as SQL SUM does, with 0 for none."""
    return sum(v for v in values if v is not None)


def _avg(values) -> float:
    """Average the non-null values, as SQL AVG does, with 0 for none."""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else 0


def _building_statistics(buildings: list[dict]) -> dict:
    """Count, area, peak and simultaneous load of buildings, with a per-type breakdown."""
    peaks = [b["peak_load_in_kw"] for b in buildings]
    count = len(buildings)
    sim_factor = 0.07 + (1 - 0.07) * (count ** (-0.75)) if count else 0
    by_type = defaultdict(list)
    for b in buildings:
        by_type[b["type"]].append(b["peak_load_in_kw"])
    return {
        "count": count,
        "total_area_m2": round(_sum(b["area"] for b in buildings), 2),
        "avg_area_m2": round(_avg(b["area"] for b in buildings), 2),
        "total_peak_load_kw": round(_sum(peaks), 2),
        "avg_peak_load_kw": round(_avg(peaks), 2),
        "simultaneous_load_kw": round(_sum(peaks) * sim_factor, 2),
        "building_types": [
            {"type": t, "count": len(p), "total_peak_kw": round(_sum(p), 2)}
            for t, p in sorted(by_type.items(), key=lambda item: -len(item[1]))
        ],
    }


def _cable_statistics(lines: list[dict]) -> tuple[dict, float]:
    """Count and length of lines with a per-type breakdown, and their unrounded total cable cost."""
    by_type = defaultdict(list)
    for line in lines:
        by_type[line["std_type"]].append(line["length_km"])
    cable_types, total_cost = [], 0
    for cable_type, lengths in sorted(by_type.items(), key=lambda item: -_sum(item[1])):
        length_km = float(_sum(lengths))
        cost = length_km * 1000 * CABLE_COST_DICT.get(cable_type, DEFAULT_CABLE_COST_PER_M)
        total_cost += cost
        cable_types.append({"type": cable_type, "count": len(lengths),
                            "length_km": round(length_km, 3), "cost_eur": round(cost, 2)})
    return {
        "count": len(lines),
        "total_length_km": round(_sum(l["length_km"] for l in lines), 3),
        "avg_length_km": round(_avg(l["length_km"] for l in lines), 4),
        "cable_types": cable_types,
    }, total_cost


def _transformer_cost(size_kva, costs: dict) -> float:
    """Cost of a transformer of size_kva, priced at the nearest listed size when not listed."""
    size = int(size_kva) if size_kva else 0
    if size in costs:
        return costs[size]
    return costs[min(costs, key=lambda kva: abs(kva - size))] if costs else 0


def _transformer_statistics(transformers: list[dict], simultaneous_load_kw: float) -> dict:
    """Count and capacity of transformers, and their utilisation by simultaneous_load_kw."""
    ratings = [t["rated_power"] for t in transformers if t["rated_power"] is not None]
    capacity = _sum(ratings)
    return {
        "count": len(transformers),
        "total_capacity_kva": round(float(capacity), 2),
        "avg_capacity_kva": round(_avg(ratings), 2),
        "min_capacity_kva": min(ratings, default=None),
        "max_capacity_kva": max(ratings, default=None),
        "utilization_percent": round(simultaneous_load_kw / float(capacity) * 100, 1) if capacity else 0,
    }


def statistics(buildings: list[dict], lines: list[dict], transformers: list[dict], costs: dict) -> dict:
    """Build the /grid-statistics statistics block from the given rows."""
    building_stats = _building_statistics(buildings)
    cable_stats, cable_cost = _cable_statistics(lines)
    trafo_costs = [{"size_kva": int(t["rated_power"]) if t["rated_power"] else 0,
                    "cost_eur": _transformer_cost(t["rated_power"], costs)} for t in transformers]
    trafo_cost = sum(t["cost_eur"] for t in trafo_costs)
    return {
        "buildings": building_stats,
        "transformers": _transformer_statistics(transformers, building_stats["simultaneous_load_kw"]),
        "cables": cable_stats,
        "costs": {
            "cable_cost_eur": round(cable_cost, 2),
            "transformer_cost_eur": round(trafo_cost, 2),
            "transformer_cost_breakdown": trafo_costs,
            "total_estimated_cost_eur": round(cable_cost + trafo_cost, 2),
        },
        "voltage": {"nominal_voltage_v": VN, "voltage_band_low": V_BAND_LOW, "voltage_band_high": V_BAND_HIGH},
    }


def selection_statistics(rows: dict, building_osm_ids: list) -> dict:
    """Statistics for the listed buildings, their transformers and their cable paths, plus whole_grid.

    whole_grid carries the unscoped buildings.count, cables.count,
    cables.total_length_km and transformers.utilization_percent.
    """
    wanted = set(building_osm_ids)
    buildings = [b for b in rows["buildings"] if b["osm_id"] in wanted]
    serving = {b["grid_result_id"] for b in buildings}
    transformers = [t for t in rows["transformers"] if t["grid_result_id"] in serving]
    lines = cable_paths(buildings, rows["lines"], transformers)
    scoped = statistics(buildings, lines, transformers, rows["transformer_costs"])
    whole = statistics(rows["buildings"], rows["lines"], rows["transformers"], rows["transformer_costs"])
    scoped["whole_grid"] = {
        "buildings": {"count": whole["buildings"]["count"]},
        "cables": {"count": whole["cables"]["count"], "total_length_km": whole["cables"]["total_length_km"]},
        "transformers": {"utilization_percent": whole["transformers"]["utilization_percent"]},
    }
    return scoped
