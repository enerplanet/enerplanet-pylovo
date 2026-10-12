"""SQL for generating a site's grid from its copied inputs instead of res, oth and transformers."""
from abc import ABC

from src.database.base_mixin import BaseMixin

# Generation identifies site rows by these osm_id keys. buildings_result is
# unique on (version_id, osm_id), so a site cannot store the real OSM id
# there without colliding with the postcode grid of the same building.
SITE_BUILDING_KEY = "'site:' || sb.site_id || ':' || sb.building_id"
SITE_TRANSFORMER_KEY = "'site_transformer:' || st.transformer_id"


class SiteMixin(BaseMixin, ABC):
    """Load a site's buildings, overrides and stations into the generation temp tables."""

    def set_site_buildings_table(self, site_id: int, plz: str) -> None:
        """Fill buildings_tem from site_building, skipping excluded buildings.

        Mirrors set_residential_buildings_table and set_other_buildings_table;
        a dwellings override replaces the household count the peak rule uses.
        """
        self.cur.execute(
            f"""INSERT INTO buildings_tem (osm_id, area, type, f_class, f_classes, geom, center, floors,
                    households_per_building, plz, height_max, height_ground, height_median,
                    floors_3dbag, bag_id, energy_label, energy_index, cbs_population,
                    cbs_households, cbs_avg_household_size, construction_year)
                SELECT {SITE_BUILDING_KEY}, sb.area,
                       CASE sb.kind WHEN 'res' THEN sb.building_t ELSE sb.use END,
                       sb.f_class, COALESCE(NULLIF(sb.f_classes, ''), sb.f_class),
                       sb.geom, ST_Centroid(sb.geom),
                       CASE sb.kind WHEN 'res' THEN COALESCE(sb.floors, 1) ELSE 1 END,
                       COALESCE(sb.dwellings, CASE sb.kind WHEN 'res' THEN sb.occupants::int END),
                       %(plz)s, sb.height_max, sb.height_ground, sb.height_median, sb.floors_3dbag,
                       sb.bag_id,
                       CASE sb.kind WHEN 'res' THEN sb.energy_label END,
                       CASE sb.kind WHEN 'res' THEN sb.energy_index END,
                       sb.cbs_population, sb.cbs_households, sb.cbs_avg_household_size,
                       CASE sb.kind WHEN 'res' THEN sb.constructi END
                FROM site_building sb
                WHERE sb.site_id = %(site)s AND NOT sb.excluded;""",
            {"site": site_id, "plz": plz},
        )

    def apply_site_demand_overrides(self, site_id: int, plz: str) -> None:
        """Apply measured peaks and add extra loads after set_building_peak_load.

        A measured peak moves the building to category measured_peak, which is
        not diversified. Each extra load becomes its own row at the building's
        position, so it connects at the same point and is diversified in its
        own category.
        """
        self.cur.execute(
            f"""UPDATE buildings_tem b
                SET peak_load_in_kw = sb.measured_peak_kw, f_class = 'measured_peak'
                FROM site_building sb
                WHERE sb.site_id = %(site)s AND sb.measured_peak_kw IS NOT NULL
                  AND b.osm_id = {SITE_BUILDING_KEY};""",
            {"site": site_id},
        )
        self.cur.execute(
            f"""INSERT INTO buildings_tem (osm_id, area, type, f_class, f_classes, geom, center, floors,
                    households_per_building, peak_load_in_kw, plz)
                SELECT {SITE_BUILDING_KEY} || ':' || l.category, 0, 'site_load', l.category,
                       l.category, sb.geom, ST_Centroid(sb.geom), 1, 1, l.kw, %(plz)s
                FROM site_building_load l
                JOIN site_building sb ON sb.site_id = l.site_id AND sb.building_id = l.building_id
                WHERE l.site_id = %(site)s AND NOT sb.excluded;""",
            {"site": site_id, "plz": plz},
        )

    def insert_site_transformers(self, site_id: int, plz: str) -> None:
        """Add the site's stations to buildings_tem as transformer rows."""
        self.cur.execute(
            f"""INSERT INTO buildings_tem (osm_id, geom, plz, center, type, peak_load_in_kw)
                SELECT {SITE_TRANSFORMER_KEY}, st.geom, %(plz)s, st.geom, 'Transformer', -1
                FROM site_transformer st
                WHERE st.site_id = %(site)s;""",
            {"site": site_id, "plz": plz},
        )

    def snap_site_transformers_to_network(self, max_distance_m: float = 200.0) -> int:
        """Attach each site station to its nearest road vertex; return how many stay unattached.

        set_vertice_id only attaches transformers lying within 1 m of a vertex,
        and stations get no connection line, so site stations snap instead,
        like the greenfield transformers placed on a vertex.
        """
        self.cur.execute(
            """UPDATE buildings_tem b
               SET vertice_id = (
                       SELECT v.id FROM ways_tem_vertices_pgr v
                       WHERE ST_DWithin(v.geom, b.center, %(d)s)
                       ORDER BY v.geom <-> b.center LIMIT 1
                   )
               WHERE b.type = 'Transformer' AND b.osm_id LIKE 'site_transformer:%%'
                 AND b.vertice_id IS NULL;
               UPDATE buildings_tem SET connection_point = vertice_id
               WHERE type = 'Transformer' AND osm_id LIKE 'site_transformer:%%'
                 AND vertice_id IS NOT NULL AND connection_point IS NULL;""",
            {"d": max_distance_m},
        )
        self.cur.execute(
            """SELECT count(*) FROM buildings_tem
               WHERE type = 'Transformer' AND osm_id LIKE 'site_transformer:%%' AND vertice_id IS NULL;"""
        )
        return self.cur.fetchone()[0]

    def get_site_unroutable_assignments(self, site_id: int) -> list:
        """Return building_ids whose fixed station is not reachable along the street network.

        Call after kcids are assigned: a building in another kcid than its
        station, or one without a road connection, cannot be cabled to it.
        """
        self.cur.execute(
            f"""SELECT a.building_id
                FROM site_transformer_assignment a
                JOIN site_transformer st
                  ON st.site_id = a.site_id AND st.transformer_id = a.transformer_id
                JOIN site_building sb ON sb.site_id = a.site_id AND sb.building_id = a.building_id
                LEFT JOIN buildings_tem tb ON tb.osm_id = {SITE_TRANSFORMER_KEY} AND tb.type = 'Transformer'
                LEFT JOIN buildings_tem bb ON bb.osm_id = {SITE_BUILDING_KEY}
                WHERE a.site_id = %s
                  AND (tb.kcid IS NULL OR bb.kcid IS NULL OR bb.kcid <> tb.kcid
                       OR bb.connection_point IS NULL)
                ORDER BY a.building_id;""",
            (site_id,),
        )
        return [r[0] for r in self.cur.fetchall()]

    def get_site_known_ratings(self, site_id: int, kcid: int) -> dict:
        """Return {transformer vertice_id: rated_kva} for the kcid's stations with a known rating."""
        self.cur.execute(
            f"""SELECT b.vertice_id, st.rated_kva
                FROM buildings_tem b
                JOIN site_transformer st ON b.osm_id = {SITE_TRANSFORMER_KEY}
                WHERE st.site_id = %s AND b.kcid = %s AND b.type = 'Transformer'
                  AND st.rated_kva IS NOT NULL;""",
            (site_id, kcid),
        )
        return dict(self.cur.fetchall())

    def get_site_fixed_assignments(self, site_id: int, kcid: int) -> dict:
        """Return {transformer vertice_id: [consumer connection points]} for fixed assignments.

        Assignments whose building lies in a different kcid than its station
        cannot be routed along the street network and are left out.
        """
        self.cur.execute(
            f"""SELECT tb.vertice_id, bb.connection_point
                FROM site_transformer_assignment a
                JOIN site_transformer st
                  ON st.site_id = a.site_id AND st.transformer_id = a.transformer_id
                JOIN site_building sb ON sb.site_id = a.site_id AND sb.building_id = a.building_id
                JOIN buildings_tem tb ON tb.osm_id = {SITE_TRANSFORMER_KEY} AND tb.type = 'Transformer'
                JOIN buildings_tem bb ON bb.osm_id = {SITE_BUILDING_KEY}
                WHERE a.site_id = %s AND tb.kcid = %s AND bb.kcid = tb.kcid
                  AND bb.connection_point IS NOT NULL;""",
            (site_id, kcid),
        )
        fixed = {}
        for vertice_id, connection_point in self.cur.fetchall():
            fixed.setdefault(vertice_id, []).append(int(connection_point))
        return fixed
