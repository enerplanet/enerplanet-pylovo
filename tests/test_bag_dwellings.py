"""Unit tests for dwelling counts taken from BAG units (datapipeline/enrichment/bag3d.py).

No network and no database. Run from the project root:

    python -m unittest tests.test_bag_dwellings -v
"""
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

from datapipeline.enrichment.bag3d import count_units_in_footprints
from datapipeline.main import DataPipeline


def _footprints() -> gpd.GeoDataFrame:
    """A house, a block of three flats, a garage and a shop, each a 10 m square."""
    return gpd.GeoDataFrame(
        {
            "osm_id": ["house", "flats", "garage", "shop"],
            "Building_T": ["house", "apartments", "yes", "yes"],
            "Area": [100.0, 100.0, 100.0, 100.0],
            "Floors": [2.0, 9.0, 1.0, 1.0],
            "Occupants": [99, 99, 99, 99],
        },
        geometry=[box(i * 20, 0, i * 20 + 10, 10) for i in range(4)],
        crs="EPSG:3035",
    )


def _units() -> gpd.GeoDataFrame:
    """BAG units: one home, three flats and a shop with a flat above; the garage holds none."""
    return gpd.GeoDataFrame(
        {"gebruiksdoel": ["woonfunctie"] * 4 + ["winkelfunctie", "woonfunctie", "woonfunctie"]},
        geometry=[Point(5, 5), Point(22, 2), Point(25, 5), Point(28, 8), Point(62, 5), Point(65, 5), Point(500, 500)],
        crs="EPSG:3035",
    )


class CountUnitsTest(unittest.TestCase):
    def test_counts_all_units_and_dwellings_per_footprint(self):
        gdf = count_units_in_footprints(_footprints(), _units())

        self.assertEqual(gdf["bag_units"].tolist(), [1, 3, 0, 2])
        self.assertEqual(gdf["bag_dwellings"].tolist(), [1, 3, 0, 1])


class RecountWithBagUnitsTest(unittest.TestCase):
    def _recount(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Res_1_enriched.gpkg"
            gdf.to_file(path, driver="GPKG")
            DataPipeline._recount_dwellings(Path(tmp))
            return gpd.read_file(path)

    def test_bag_dwellings_replace_the_estimate_and_unitless_buildings_go(self):
        out = self._recount(count_units_in_footprints(_footprints(), _units()))

        self.assertEqual(out["osm_id"].tolist(), ["house", "flats", "shop"])
        self.assertEqual(out["Occupants"].tolist(), [1, 3, 1])

    def test_without_bag_counts_the_estimate_stays(self):
        out = self._recount(_footprints())

        self.assertEqual(out["osm_id"].tolist(), ["house", "flats", "garage", "shop"])
        self.assertEqual(out["Occupants"].tolist(), [1, 10, 1, 1])


if __name__ == "__main__":
    unittest.main()
