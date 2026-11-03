"""Unit tests for building floors through download and EUBUCCO enrichment.

No network and no database. Run from the project root:

    python -m unittest tests.test_building_floors -v
"""
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from datapipeline.downloaders.buildings import BuildingDownloader
from datapipeline.enrichment.eubucco import EUBUCCOEnricher

# Two 100 m² houses in EPSG:3035: one tagged with 3 levels in OSM, one untagged.
OSM_BUILDINGS = gpd.GeoDataFrame(
    {
        "osm_id": [1, 2],
        "osm_way_id": [None, None],
        "building": ["house", "house"],
        "other_tags": ['"building:levels"=>"3"', None],
    },
    geometry=[box(0, 0, 10, 10), box(100, 0, 110, 10)],
    crs="EPSG:3035",
)

# EUBUCCO says both houses have 2 floors.
EUBUCCO = gpd.GeoDataFrame(
    {"floors": [2.0, 2.0], "height": [7.0, 7.0], "type": ["residential"] * 2},
    geometry=[box(-1, -1, 11, 11), box(99, -1, 111, 11)],
    crs="EPSG:3035",
)


def download(tmp: Path) -> gpd.GeoDataFrame:
    """Run the residential download step on OSM_BUILDINGS and read its output."""
    out = tmp / "Res.gpkg"
    BuildingDownloader({"country": "germany", "state": "x"})._process_residential_gdf(OSM_BUILDINGS.copy(), out)
    return gpd.read_file(out).set_index("osm_id")


class BuildingFloorsTest(unittest.TestCase):
    def test_untagged_building_has_no_floor_count_after_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = download(Path(tmp))
        self.assertEqual(res.loc[1, "Floors"], 3)
        self.assertTrue(res["Floors"].isna()[2])
        self.assertEqual(res.loc[2, "Occupants"], 1)  # a house is one dwelling

    def test_eubucco_fills_untagged_floors_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = download(Path(tmp)).reset_index()
            enricher = EUBUCCOEnricher({"country": "germany"}, cache_dir=Path(tmp))
            joined = enricher._spatial_join(res, EUBUCCO).set_index("osm_id")
        self.assertEqual(joined.loc[1, "Floors"], 3)  # OSM wins
        self.assertEqual(joined.loc[2, "Floors"], 2)


if __name__ == "__main__":
    unittest.main()
