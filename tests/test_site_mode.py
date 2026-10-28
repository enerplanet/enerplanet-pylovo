"""Integration tests for site mode (src/site_mode).

They need a PostgreSQL database with pylovo's tables, the ways preprocessing
functions and buildings in TEST_POLYGON (for example the EnerPlanET
pylovo fixture), reached through the usual DBNAME, DBUSER, PASSWORD, HOST,
PORT and TARGET_SCHEMA variables. Never point them at a shared database:
they create, generate and delete sites. Run from the project root:

    python -m unittest tests.test_site_mode -v
"""
import json
import unittest

import psycopg2

from config.config_table_structure import CREATE_QUERIES, SITE_TABLES
from src import utils
from src.config_loader import CONSUMER_CATEGORIES, DBNAME, DBUSER, HOST, PASSWORD, PORT, TARGET_SCHEMA
from src.database.database_client import DatabaseClient
from src.site_mode import added_buildings, buildings, sites, transformers
from src.site_mode.errors import SiteValidationError
from src.site_mode.generation import generate_site

TEST_POLYGON = {"type": "Polygon", "coordinates": [[
    [8.8058, 53.0907], [8.8094, 53.0907], [8.8094, 53.0930], [8.8058, 53.0930], [8.8058, 53.0907]]]}

# Open ground inside TEST_POLYGON, more than 15 m from any building.
FREE_FOOTPRINT = {"type": "Polygon", "coordinates": [[
    [8.80678, 53.09232], [8.80690, 53.09232], [8.80690, 53.09239], [8.80678, 53.09239], [8.80678, 53.09232]]]}

NO_OVERRIDES = {"measured_peak_kw": None, "measured_yearly_kwh": None, "dwellings": None,
                "extra_loads": [], "excluded": False, "f_class": None}

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
        overrides = dict(NO_OVERRIDES, measured_peak_kw=40.0, extra_loads=[{"category": "heat_pump", "kw": 9.0}])
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
        buildings.patch_buildings(self.cur, self.site_id, [{"building_id": nearest, "overrides": dict(
            NO_OVERRIDES, measured_peak_kw=150.0)}])
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

    def _add_free_building(self) -> int:
        """Add one 3D-only building on open ground with two floors; return its building_id."""
        result = added_buildings.add_buildings(self.cur, self.site_id, [{
            "geom": FREE_FOOTPRINT, "source": "city2tabula", "source_id": "unittest-3d-1",
            "f_class": "yes", "floors": 2}])
        self.conn.commit()
        return result["buildings"][0]["properties"]["building_id"]

    def test_added_building_is_connected_with_default_demand(self):
        """A 3D-only building has no OSM type, yet it is cabled and keeps its default peak."""
        building_id = self._add_free_building()
        generate_site(self.site_id, self._site_revision())
        self.cur.execute("SELECT vertice_id IS NOT NULL, peak_load_in_kw FROM buildings_result WHERE osm_id = %s",
                         (f"site:{self.site_id}:{building_id}",))
        self.assertEqual(self.cur.fetchone(), (True, 29.0))

    def test_f_class_override_changes_default_demand(self):
        """An office is sized by floor area, not by households."""
        building_id = self._add_free_building()
        buildings.patch_buildings(self.cur, self.site_id, [{"building_id": building_id,
                                                            "overrides": dict(NO_OVERRIDES, f_class="office")}])
        props = next(f["properties"] for f in buildings.list_buildings(self.cur, self.site_id)["features"]
                     if f["properties"]["building_id"] == building_id)
        self.assertTrue(props["f_class_confirmed"])
        self.assertAlmostEqual(props["defaults"]["peak_kw"], props["area_m2"] * 55.0 / 1000)

    def test_deleted_building_id_is_not_reused(self):
        """Building ids stay stable: a new building never takes a deleted one's id."""
        first = self._add_free_building()
        added_buildings.delete_building(self.cur, self.site_id, first)
        self.conn.commit()
        second = self._add_free_building()
        self.assertGreater(second, first)
        with self.assertRaises(SiteValidationError):
            added_buildings.delete_building(self.cur, self.site_id, 1)

    def test_generation_leaves_shared_tables_unchanged(self):
        """A site reads its own copy; res, oth and transformers are never written."""
        self.cur.execute(SHARED_TABLES_SQL)
        before = self.cur.fetchall()
        generate_site(self.site_id, self._site_revision())
        self.cur.execute(SHARED_TABLES_SQL)
        self.assertEqual(self.cur.fetchall(), before)



class SiteTransformerCostTest(unittest.TestCase):
    """A station's cost is stored and returned, and a cost-only edit keeps the revision.

    Needs only the country row DE and the site tables, so it can run on its own:
    python -m unittest tests.test_site_mode.SiteTransformerCostTest -v
    """

    def setUp(self):
        self.conn = _connect()
        self.cur = self.conn.cursor()
        self.cur.execute(
            """INSERT INTO site (user_id, name, country_code, geom)
               VALUES ('test', 'cost test', 'DE', ST_Multi(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 3035)))
               RETURNING site_id""",
            (json.dumps(TEST_POLYGON),),
        )
        self.site_id = self.cur.fetchone()[0]
        self.cur.execute(
            """INSERT INTO site_building (site_id, building_id, kind, geom)
               SELECT site_id, b, 'res', geom FROM site, generate_series(1, 2) AS b WHERE site_id = %s""",
            (self.site_id,),
        )
        station = {"transformer_id": None, "rated_kva": 400, "cost_eur": None, "assigned_building_ids": [1],
                   "geom": {"type": "Point", "coordinates": [8.807, 53.092]}}
        self.revision = transformers.replace_transformers(self.cur, self.site_id, [station])
        self.conn.commit()

    def tearDown(self):
        self.cur.execute("DELETE FROM site WHERE site_id = %s", (self.site_id,))
        self.conn.commit()
        self.conn.close()

    def _replace(self, **changes) -> int:
        """Send back the station as GET returns it, with changes applied; return the revision."""
        station = transformers.list_transformers(self.cur, self.site_id)[0]
        station.update(changes)
        revision = transformers.replace_transformers(self.cur, self.site_id, [station])
        self.conn.commit()
        return revision

    def test_cost_only_edit_keeps_the_revision(self):
        self.assertEqual(self._replace(cost_eur=8500.0), self.revision)
        self.assertEqual(transformers.list_transformers(self.cur, self.site_id)[0]["cost_eur"], 8500.0)
        self.assertEqual(self._replace(cost_eur=None), self.revision)

    def test_size_assignment_or_position_edit_bumps_the_revision(self):
        self.assertEqual(self._replace(rated_kva=630), self.revision + 1)
        self.assertEqual(self._replace(assigned_building_ids=[1, 2]), self.revision + 2)
        moved = {"type": "Point", "coordinates": [8.808, 53.092]}
        self.assertEqual(self._replace(geom=moved), self.revision + 3)


if __name__ == "__main__":
    unittest.main()
