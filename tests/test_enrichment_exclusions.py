"""Unit tests for dropping processor-excluded buildings from enrichment outputs (datapipeline/main.py).

Run from the project root:

    python -m unittest tests.test_enrichment_exclusions -v
"""
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from datapipeline.main import DataPipeline


def _write(path: Path, osm_ids: list) -> None:
    """Write one square footprint per osm_id to a GeoPackage."""
    geoms = [box(i * 10, 0, i * 10 + 5, 5) for i in range(len(osm_ids))]
    gpd.GeoDataFrame({"osm_id": osm_ids}, geometry=geoms, crs="EPSG:3035").to_file(path, driver="GPKG")


def _ids(path: Path) -> list:
    return sorted(gpd.read_file(path)["osm_id"])


class DropExcludedBuildingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_enriched_file_keeps_only_processed_buildings(self):
        _write(self.dir / "Res_1.gpkg", ["a", "b", "shed"])
        _write(self.dir / "Res_1_processed.gpkg", ["a", "b"])
        _write(self.dir / "Res_1_enriched.gpkg", ["a", "b", "shed"])
        _write(self.dir / "Oth_1_processed.gpkg", ["c"])
        _write(self.dir / "Oth_1_enriched_energy.gpkg", ["c", "fence"])

        DataPipeline._drop_excluded_buildings(self.dir)

        self.assertEqual(_ids(self.dir / "Res_1_enriched.gpkg"), ["a", "b"])
        self.assertEqual(_ids(self.dir / "Oth_1_enriched_energy.gpkg"), ["c"])
        self.assertEqual(_ids(self.dir / "Res_1.gpkg"), ["a", "b", "shed"])

    def test_enriched_file_without_processed_sibling_is_unchanged(self):
        _write(self.dir / "Res_2_enriched.gpkg", ["a", "shed"])

        DataPipeline._drop_excluded_buildings(self.dir)

        self.assertEqual(_ids(self.dir / "Res_2_enriched.gpkg"), ["a", "shed"])


if __name__ == "__main__":
    unittest.main()
