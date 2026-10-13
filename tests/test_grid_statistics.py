"""Unit tests for the /grid-statistics selection scope (api/services/grid_statistics.py).

They need no database, only the DB environment variables that importing
src.config_loader requires. Run from the project root:

    PYTHONPATH=api python -m unittest tests.test_grid_statistics -v
"""
import unittest

from services.grid_statistics import cable_paths, selection_statistics


def _line(line_id: int, from_bus: int, to_bus: int, km: float, grid: int = 1) -> dict:
    return {"lines_result_id": line_id, "grid_result_id": grid, "std_type": "NAYY 4x150 SE",
            "from_bus": from_bus, "to_bus": to_bus, "length_km": km}


def _building(osm_id: str, vertice: int, grid: int = 1, peak: float = 10.0) -> dict:
    return {"osm_id": osm_id, "grid_result_id": grid, "area": 100.0, "type": "residential",
            "peak_load_in_kw": peak, "vertice_id": vertice, "connection_point": None}


# Transformer bus 1 feeds street nodes 2 -> 3 -> 4; houses 20, 30, 40 hang off 2, 3, 4,
# and 1 -> 1 is the 1 m stub the cable installer writes at the transformer bus.
LINES = [_line(1, 1, 2, 0.1), _line(2, 2, 3, 0.1), _line(3, 3, 4, 0.1),
         _line(4, 2, 20, 0.02), _line(5, 3, 30, 0.02), _line(6, 4, 40, 0.02), _line(7, 1, 1, 0.001)]
BUILDINGS = [_building("a", 20), _building("b", 30), _building("c", 40)]
TRANSFORMERS = [{"grid_result_id": 1, "rated_power": 400, "ont_vertice_id": 1}]


class CablePathsTest(unittest.TestCase):
    def test_path_runs_from_house_back_to_transformer(self):
        ids = {l["lines_result_id"] for l in cable_paths([BUILDINGS[1]], LINES, TRANSFORMERS)}
        self.assertEqual(ids, {5, 2, 1})

    def test_shared_street_segment_counts_once(self):
        lines = cable_paths(BUILDINGS[:2], LINES, TRANSFORMERS)
        self.assertEqual(sorted(l["lines_result_id"] for l in lines), [1, 2, 4, 5])

    def test_building_on_the_transformer_bus_has_no_cable(self):
        self.assertEqual(cable_paths([_building("t", 1)], LINES, TRANSFORMERS), [])


class SelectionStatisticsTest(unittest.TestCase):
    def test_scopes_figures_and_keeps_whole_grid(self):
        rows = {"buildings": BUILDINGS, "lines": LINES, "transformers": TRANSFORMERS,
                "transformer_costs": {400: 10000.0}}
        stats = selection_statistics(rows, ["a"])
        self.assertEqual(stats["buildings"]["count"], 1)
        self.assertEqual(stats["cables"]["count"], 2)
        self.assertEqual(stats["cables"]["total_length_km"], 0.12)
        self.assertEqual(stats["transformers"]["utilization_percent"], 2.5)
        self.assertEqual(stats["whole_grid"], {
            "buildings": {"count": 3},
            "cables": {"count": 7, "total_length_km": 0.361},
            "transformers": {"utilization_percent": round(30 * (0.07 + 0.93 * 3 ** -0.75) / 400 * 100, 1)},
        })

    def test_unknown_buildings_give_an_empty_selection(self):
        rows = {"buildings": BUILDINGS, "lines": LINES, "transformers": TRANSFORMERS, "transformer_costs": {}}
        stats = selection_statistics(rows, ["missing"])
        self.assertEqual((stats["buildings"]["count"], stats["cables"]["count"], stats["transformers"]["count"]),
                         (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
