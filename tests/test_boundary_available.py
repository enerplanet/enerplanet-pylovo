"""Integration test for GET /boundary/available (api/routers/boundary.py).

It needs a PostgreSQL database with pylovo's tables and generated grids, reached
through the usual DBNAME, DBUSER, PASSWORD, HOST, PORT and TARGET_SCHEMA
variables. It only reads. Run from the project root:

    PYTHONPATH=api python -m unittest tests.test_boundary_available -v
"""
import asyncio
import unittest

from routers.boundary import get_available_regions
from src.config_loader import VERSION_ID
from src.database.database_client import DatabaseClient

# Every postcode that has at least one grid, per region, read from the tables
# directly so the test holds for whatever grids the database contains.
GRID_POSTCODES_SQL = """
SELECT UPPER(pr.country_code), pr.state_code, pr.postcode_result_plz
FROM postcode_result pr
WHERE pr.version_id = %(version)s
  AND pr.state_code IS NOT NULL AND pr.state_code <> ''
  AND EXISTS (
      SELECT 1 FROM grid_result gr
      WHERE gr.version_id = pr.version_id
        AND gr.plz = pr.postcode_result_plz
        AND gr.country_code = pr.country_code)
"""


class AvailableRegionsPostcodesTest(unittest.TestCase):
    """Each region lists, sorted, exactly the postcodes that have grids in it."""

    def test_regions_list_their_grid_postcodes(self):
        expected = {}
        with DatabaseClient() as dbc:
            dbc.cur.execute(GRID_POSTCODES_SQL, {"version": str(VERSION_ID)})
            for country_code, state_code, plz in dbc.cur.fetchall():
                expected.setdefault((country_code, state_code), []).append(plz)
        self.assertTrue(expected, "the database holds no generated grids to test against")

        answer = asyncio.run(get_available_regions(version_id=None))
        listed = {(r["country_code"], r["state_code"]): r["postcodes"] for r in answer["regions"]}

        self.assertEqual(listed, {key: sorted(codes) for key, codes in expected.items()})


if __name__ == "__main__":
    unittest.main()
