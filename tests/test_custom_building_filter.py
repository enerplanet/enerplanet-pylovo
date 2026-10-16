"""Integration tests for the custom buildings filter of /generate-grid (api/routers/grid.py).

They need a throwaway PostgreSQL database with pylovo's tables, reached through
the usual DBNAME, DBUSER, PASSWORD, HOST, PORT and TARGET_SCHEMA variables. They
insert and delete one custom building. Run from the project root:

    PYTHONPATH=api python -m unittest tests.test_custom_building_filter -v
"""
import asyncio
import json
import unittest

from pydantic import ValidationError

from models import CustomBuildingRequest, GridRequest
from routers.building import get_custom_buildings
from routers.grid import generate_grid_phase1
from src.database.database_client import DatabaseClient

POLYGON = {"type": "Polygon", "coordinates": [[
    [8.8058, 53.0907], [8.8094, 53.0907], [8.8094, 53.0930], [8.8058, 53.0930], [8.8058, 53.0907]]]}
OWNER = "owner-a"
OTHER = "owner-b"

# Same columns as the table api/routers/building.py creates on first use.
CUSTOM_BUILDINGS_SQL = """
CREATE TABLE IF NOT EXISTS custom_buildings (
    custom_building_id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    f_class VARCHAR(50) NOT NULL,
    building_type VARCHAR(50) NOT NULL,
    area FLOAT NOT NULL,
    peak_load_kw FLOAT NOT NULL,
    demand_energy FLOAT,
    geom GEOMETRY(Point, 4326),
    geom_area GEOMETRY(Polygon, 4326),
    is_public BOOLEAN DEFAULT FALSE,
    icon VARCHAR(50) DEFAULT 'building-2',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


INSERT_SQL = """
INSERT INTO custom_buildings (user_id, title, f_class, building_type, area, peak_load_kw, geom, is_public)
VALUES (%s, %s, 'house', 'residential', 100, 5, ST_SetSRID(ST_MakePoint(8.807, 53.092), 4326), %s)
RETURNING custom_building_id
"""


class CustomBuildingFilterTest(unittest.TestCase):
    """A custom building is returned to its owner only, even one stored with is_public."""

    def setUp(self):
        with DatabaseClient() as dbc:
            dbc.cur.execute(CUSTOM_BUILDINGS_SQL)
            dbc.cur.execute(INSERT_SQL, (OWNER, "private", False))
            self.building_id = dbc.cur.fetchone()[0]
            dbc.cur.execute(INSERT_SQL, (OTHER, "stored as public", True))
            self.public_id = dbc.cur.fetchone()[0]
            dbc.conn.commit()

    def tearDown(self):
        with DatabaseClient() as dbc:
            dbc.cur.execute("DELETE FROM custom_buildings WHERE custom_building_id = ANY(%s)",
                            ([self.building_id, self.public_id],))
            dbc.conn.commit()

    def _custom_ids(self, **request) -> list:
        """Ids of the custom buildings /generate-grid returns for POLYGON."""
        response = asyncio.run(generate_grid_phase1(GridRequest(geom=POLYGON, **request)))
        features = json.loads(json.dumps(response["buildings"], default=str)).get("features", [])
        return [-int(f["properties"]["osm_id"]) for f in features if int(f["properties"]["osm_id"]) < 0]

    def test_owner_sees_the_building(self):
        self.assertIn(self.building_id, self._custom_ids(user_id=OWNER))

    def test_quote_in_user_id_does_not_widen_the_filter(self):
        self.assertNotIn(self.building_id, self._custom_ids(user_id="x' OR '1'='1"))

    def test_excluded_building_is_left_out(self):
        self.assertNotIn(self.building_id, self._custom_ids(user_id=OWNER, excluded_building_ids=[self.building_id]))

    def test_other_users_public_building_is_not_in_the_grid(self):
        self.assertNotIn(self.public_id, self._custom_ids(user_id=OWNER))

    def test_owner_still_sees_a_building_stored_as_public(self):
        self.assertIn(self.public_id, self._custom_ids(user_id=OTHER))

    def test_listing_returns_only_own_buildings(self):
        listed = {b["id"] for b in asyncio.run(get_custom_buildings(OWNER))["buildings"]}
        self.assertIn(self.building_id, listed)
        self.assertNotIn(self.public_id, listed)

    def test_a_new_building_cannot_be_public(self):
        with self.assertRaises(ValidationError):
            CustomBuildingRequest(user_id=OWNER, title="t", f_class="residential", area=100,
                                  geometry={}, geometry_area={}, is_public=True)


if __name__ == "__main__":
    unittest.main()
