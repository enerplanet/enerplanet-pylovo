"""Integration tests for site mode (src/site_mode).

They need a PostgreSQL database with pylovo's tables, the ways preprocessing
functions and buildings in TEST_POLYGON (for example the EnerPlanET
pylovo fixture), reached through the usual DBNAME, DBUSER, PASSWORD, HOST,
PORT and TARGET_SCHEMA variables. Never point them at a shared database:
they create, generate and delete sites. Run from the project root:

    python -m unittest tests.test_site_mode -v
"""
import unittest

import psycopg2

from config.config_table_structure import CREATE_QUERIES, SITE_TABLES
from src import utils
from src.config_loader import CONSUMER_CATEGORIES, DBNAME, DBUSER, HOST, PASSWORD, PORT, TARGET_SCHEMA
from src.database.database_client import DatabaseClient
from src.site_mode import buildings, sites, transformers
from src.site_mode.errors import SiteValidationError
from src.site_mode.generation import generate_site

TEST_POLYGON = {"type": "Polygon", "coordinates": [[
    [8.8058, 53.0907], [8.8094, 53.0907], [8.8094, 53.0930], [8.8058, 53.0930], [8.8058, 53.0907]]]}

SHARED_TABLES_SQL = """
SELECT md5(string_agg(osm_id || f_class || ST_AsText(geom), ',' ORDER BY osm_id, f_class)) FROM res
UNION ALL SELECT md5(string_agg(osm_id || f_class || ST_AsText(geom), ',' ORDER BY osm_id, f_class)) FROM oth
UNION ALL SELECT md5(string_agg(osm_id || COALESCE(transformer_rated_power::text, ''), ','
                                ORDER BY osm_id)) FROM transformers
"""


def _connect():
    """Open a connection to the test database; callers commit after each store call."""
    return psycopg2.connect(dbname=DBNAME, user=DBUSER, password=PASSWORD, host=HOST, port=PORT,
                            options=f"-c search_path={TARGET_SCHEMA},public")


class SiteModeTest(unittest.TestCase):
    """Create a site from real buildings, apply site data, generate and check the result."""

    @classmethod
    def setUpClass(cls):
        with DatabaseClient() as dbc:
            for table_name in SITE_TABLES:
                dbc.cur.execute(CREATE_QUERIES[table_name])
            dbc.insert_consumer_categories_from_config(consumer_categories=CONSUMER_CATEGORIES)
        cls.conn = _connect()
        cls.cur = cls.conn.cursor()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.site_id = sites.create_site(self.cur, "unittest", "unittest site", "DE",
                                         TEST_POLYGON)["site_id"]
        self.conn.commit()
        self.stations = transformers.list_transformers(self.cur, self.site_id)
        if not self.stations:
            self.skipTest("TEST_POLYGON has no OSM transformer in this database")

    def tearDown(self):
        self.conn.rollback()
        sites.delete_site(self.cur, self.site_id)
        self.conn.commit()

    def _site_revision(self) -> int:
        return sites.get_site(self.cur, self.site_id)["revision"]

    def test_measured_peak_category_is_not_diversified(self):
        """A measured peak keeps its full value however many units share it."""
        measured = CONSUMER_CATEGORIES.set_index("definition").loc["measured_peak"]
        self.assertEqual(utils.oneSimultaneousLoad(150.0, 7, measured["sim_factor"]), 150.0)

    def test_extra_loads_with_measured_peak_are_rejected(self):
        """A measured peak may already contain an extra load, so the two cannot be combined."""
        overrides = {"measured_peak_kw": 40.0, "measured_yearly_kwh": None, "dwellings": None,
                     "extra_loads": [{"category": "heat_pump", "kw": 9.0}], "excluded": False}
        with self.assertRaises(SiteValidationError):
            buildings.patch_buildings(self.cur, self.site_id, [{"building_id": 1, "overrides": overrides}])

    def test_known_rating_is_kept_and_overload_reported(self):
        """Fixed buildings stay on a known station even past its rating, which is reported."""
        station = self.stations[0]
        self.cur.execute(
            """SELECT b.building_id FROM site_building b, site_transformer t
               WHERE b.site_id = %s AND t.transformer_id = %s
               ORDER BY ST_Distance(b.geom, t.geom) LIMIT 1""",
            (self.site_id, station["transformer_id"]),
        )
        nearest = self.cur.fetchone()[0]
        buildings.patch_buildings(self.cur, self.site_id, [{"building_id": nearest, "overrides": {
            "measured_peak_kw": 150.0, "measured_yearly_kwh": None, "dwellings": None,
            "extra_loads": [], "excluded": False}}])
        transformers.replace_transformers(self.cur, self.site_id, [{
            "transformer_id": station["transformer_id"], "geom": station["geom"], "rated_kva": 100,
            "assigned_building_ids": [nearest]}])
        self.conn.commit()
        result = generate_site(self.site_id, self._site_revision())
        self.assertEqual(result["unroutable_building_ids"], [])
        self.assertIn(station["transformer_id"], result["overloaded_transformer_ids"])
        self.cur.execute(
            """SELECT g.transformer_rated_power, br.peak_load_in_kw
               FROM site_grid_transformer s
               JOIN grid_result g ON g.grid_result_id = s.grid_result_id
               JOIN buildings_result br ON br.grid_result_id = s.grid_result_id
               WHERE s.site_id = %s AND s.site_transformer_id = %s AND br.osm_id = %s""",
            (self.site_id, station["transformer_id"], f"site:{self.site_id}:{nearest}"),
        )
        self.assertEqual(self.cur.fetchone(), (100, 150.0))

    def test_generation_leaves_shared_tables_unchanged(self):
        """A site reads its own copy; res, oth and transformers are never written."""
        self.cur.execute(SHARED_TABLES_SQL)
        before = self.cur.fetchall()
        generate_site(self.site_id, self._site_revision())
        self.cur.execute(SHARED_TABLES_SQL)
        self.assertEqual(self.cur.fetchall(), before)


if __name__ == "__main__":
    unittest.main()
