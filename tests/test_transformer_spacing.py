"""Unit test for dropping transformers that stand too close together.

No network and no database. Run from the project root:

    python -m unittest tests.test_transformer_spacing -v
"""
import unittest

import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from datapipeline.processors.transformer_processor import drop_close_transformers


def matrix_rule(gdf, min_distance):
    """Upstream pylovo's dense upper-triangle distance matrix, kept as the reference."""
    c = gdf.geometry.centroid
    d = c.apply(lambda p: c.distance(p))
    d = d.where(np.triu(np.ones(d.shape)).astype(bool))
    np.fill_diagonal(d.values, float("nan"))
    return gdf.drop(index=d[(d < min_distance).any(axis=1)].index)


class DropCloseTransformersTest(unittest.TestCase):
    def test_matches_the_distance_matrix_rule(self):
        rng = np.random.default_rng(7)
        xy = rng.uniform(0, 200, size=(400, 2))
        gdf = gpd.GeoDataFrame(
            {"osm_id": range(400)}, geometry=[Point(x, y).buffer(1) for x, y in xy], crs="EPSG:3035"
        ).set_index(np.arange(1000, 1400))
        expected = matrix_rule(gdf, 8)
        self.assertLess(len(expected), 400)  # the sample has close pairs
        self.assertEqual(list(drop_close_transformers(gdf, 8).index), list(expected.index))

    def test_keeps_the_later_of_a_close_pair(self):
        gdf = gpd.GeoDataFrame(geometry=[Point(0, 0), Point(5, 0), Point(100, 0)], crs="EPSG:3035")
        self.assertEqual(list(drop_close_transformers(gdf, 8).index), [1, 2])


if __name__ == "__main__":
    unittest.main()
