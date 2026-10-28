import warnings
import psycopg2 as psy
from sqlalchemy import create_engine
try:
    from typing import override
except ImportError:
    # Python < 3.12 doesn't have override
    def override(func):
        return func

from src import utils
from src.config_loader import *
from src.database.preprocessing_mixin import PreprocessingMixin
from src.database.clustering_mixin import ClusteringMixin
from src.database.grid_mixin import GridMixin
from src.database.analysis_mixin import AnalysisMixin
from src.database.utils_mixin import UtilsMixin
from src.database.site_mixin import SiteMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class DatabaseClient(PreprocessingMixin, ClusteringMixin, GridMixin, AnalysisMixin, UtilsMixin, SiteMixin):
    """Main database client handling connections."""

    def __init__(self, dbname=DBNAME, user=DBUSER, pw=PASSWORD, host=HOST, port=PORT, **kwargs):
        self._db_connect_kwargs = {
            "database": dbname,
            "user": user,
            "password": pw,
            "host": host,
            "port": port,
            "options": f"-c search_path={TARGET_SCHEMA},public",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
        self.logger = utils.create_logger(
            "DatabaseClient", log_file=kwargs.get("log_file", "../log.txt"), log_level=LOG_LEVEL
        )
        try:
            self.conn = psy.connect(**self._db_connect_kwargs)
            self.cur = self.conn.cursor()
            self.db_path = f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{dbname}"
            self.sqla_engine = create_engine(
                self.db_path,
                connect_args={"options": f"-c search_path={TARGET_SCHEMA},public"},
                pool_pre_ping=True
            )
        except psy.OperationalError as err:
            self.logger.warning(
                f"Connecting to {dbname} was not successful. Make sure, that you have established the SSH connection with correct port mapping."
            )
            raise err

        # init supers after everything is set up
        super().__init__()

        self.logger.debug(f"DatabaseClient is constructed and connected to {self.db_path}.")

    def _close_db_handles(self) -> None:
        """Close psycopg handles only (keeps SQLAlchemy engine alive)."""
        try:
            if hasattr(self, "cur") and self.cur:
                self.cur.close()
        except Exception:
            pass
        try:
            if hasattr(self, "conn") and self.conn:
                self.conn.close()
        except Exception:
            pass

    def reconnect(self, retries: int = 3, backoff: float = 1.0) -> None:
        """Recreate the psycopg connection + main cursor after a disconnect.

        Retries with exponential backoff so transient server restarts or
        OOM-recovery windows don't permanently kill the worker.
        """
        import time as _time

        self._close_db_handles()
        last_err = None
        for attempt in range(retries):
            try:
                self.conn = psy.connect(**self._db_connect_kwargs)
                self.cur = self.conn.cursor()
                if attempt > 0:
                    self.logger.info("DB reconnect succeeded on attempt %d", attempt + 1)
                return
            except Exception as e:
                last_err = e
                wait = backoff * (2 ** attempt)
                self.logger.warning(
                    "DB reconnect attempt %d/%d failed: %s  (retry in %.1fs)",
                    attempt + 1, retries, e, wait,
                )
                _time.sleep(wait)
        raise last_err  # type: ignore[misc]

    def ensure_connection(self, clear_transaction: bool = False) -> bool:
        """
        Ensure the primary psycopg connection/cursor are usable.

        Returns True if the current connection stayed in use, False if a reconnect was performed.
        """
        try:
            if not hasattr(self, "conn") or self.conn is None or self.conn.closed:
                raise psy.InterfaceError("connection already closed")
            if clear_transaction:
                self.conn.rollback()
            if (not hasattr(self, "cur")) or self.cur is None or getattr(self.cur, "closed", 1):
                self.cur = self.conn.cursor()
            with self.conn.cursor() as probe_cur:
                probe_cur.execute("SELECT 1")
            if getattr(self.cur, "closed", 1):
                self.cur = self.conn.cursor()
            return True
        except Exception:
            self.reconnect()
            if clear_transaction:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            return False

    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures proper cleanup."""
        self.close()
    
    def close(self):
        """Explicitly close all database connections."""
        self._close_db_handles()
        
        try:
            if hasattr(self, 'sqla_engine') and self.sqla_engine:
                self.sqla_engine.dispose()
        except Exception as e:
            print(f"Warning: Error disposing SQLAlchemy engine: {e}")
    
    def __del__(self):
        """Clean up database connections."""
        self.close()

    @override
    def get_connection(self):
        return self.conn

    @override
    def get_logger(self):
        return self.logger

    @override
    def get_sqla_engine(self):
        return self.sqla_engine

    def save_tables(
            self,
            plz,
            country_code: str = "DE",
            enable_nearest_fallback: bool = False,
    ):

        """Saves building and ways results from ZIP code-specific temporary tables to the permanent results tables.
           Removes duplicates from the temporary building table to avoid violating the unique constraint."""

        # suffixed table names for the current PLZ
        buildings_table = f"buildings_tem_{plz}"
        ways_table = f"ways_tem_{plz}"

        # Schema compatibility for richer class metadata.
        self.cur.execute("ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS f_classes text;")
        self.cur.execute(f"ALTER TABLE {buildings_table} ADD COLUMN IF NOT EXISTS f_classes text;")
        # Ensure enrichment columns exist in both tables
        for col, dtype in [
            ("height_max", "double precision"), ("height_ground", "double precision"),
            ("height_median", "double precision"), ("floors_3dbag", "integer"),
            ("bag_id", "varchar"), ("energy_label", "varchar(5)"), ("energy_index", "double precision"),
            ("cbs_population", "double precision"), ("cbs_households", "double precision"),
            ("cbs_avg_household_size", "double precision"),
        ]:
            self.cur.execute(f"ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS {col} {dtype};")
            self.cur.execute(f"ALTER TABLE {buildings_table} ADD COLUMN IF NOT EXISTS {col} {dtype};")

        # Save building results.
        # If a building was split into multiple rows (e.g. osm_id suffix '_poi_*'),
        # fold it back to one record and preserve all detected classes in f_classes.
        query = f"""
                    WITH normalized AS (
                        SELECT
                            bt.*,
                            split_part(bt.osm_id, '_poi_', 1) AS osm_id_base,
                            COALESCE(NULLIF(LOWER(TRIM(bt.f_class)), ''), 'yes') AS f_class_norm
                        FROM {buildings_table} bt
                    ),
                    ranked AS (
                        SELECT
                            n.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY n.osm_id_base, n.plz
                                ORDER BY
                                    CASE
                                        WHEN n.f_class_norm IN ('yes', 'building', 'residential', 'house', 'unclassified', 'other')
                                            THEN 1
                                        ELSE 0
                                    END,
                                    n.f_class_norm
                            ) AS rn
                        FROM normalized n
                    ),
                    class_set AS (
                        SELECT DISTINCT
                            r.osm_id_base,
                            r.plz,
                            r.f_class_norm,
                            CASE
                                WHEN r.f_class_norm IN ('yes', 'building', 'residential', 'house', 'unclassified', 'other')
                                    THEN 1
                                ELSE 0
                            END AS generic_rank
                        FROM ranked r
                    ),
                    aggregated AS (
                        SELECT
                            cs.osm_id_base,
                            cs.plz,
                            (ARRAY_AGG(cs.f_class_norm ORDER BY cs.generic_rank, cs.f_class_norm))[1] AS primary_f_class,
                            ARRAY_TO_STRING(
                                ARRAY_AGG(cs.f_class_norm ORDER BY cs.generic_rank, cs.f_class_norm),
                                ';'
                            ) AS f_classes
                        FROM class_set cs
                        GROUP BY cs.osm_id_base, cs.plz
                    ),
                    selected AS (
                        SELECT
                            r.osm_id_base AS osm_id,
                            r.kcid,
                            r.bcid,
                            r.plz,
                            r.area,
                            r.type,
                            r.geom,
                            r.households_per_building,
                            r.center,
                            r.peak_load_in_kw,
                            r.vertice_id,
                            r.floors,
                            r.connection_point,
                            a.primary_f_class,
                            a.f_classes,
                            r.height_max,
                            r.height_ground,
                            r.height_median,
                            r.floors_3dbag,
                            r.bag_id,
                            r.energy_label,
                            r.energy_index,
                            r.cbs_population,
                            r.cbs_households,
                            r.cbs_avg_household_size,
                            r.construction_year
                        FROM ranked r
                        JOIN aggregated a
                            ON a.osm_id_base = r.osm_id_base
                           AND a.plz = r.plz
                        WHERE r.rn = 1
                    )
                    INSERT INTO buildings_result
                    (version_id, osm_id, grid_result_id, area, type, f_class, f_classes, geom, households_per_building, center,
                    peak_load_in_kw, vertice_id, floors, connection_point,
                    height_max, height_ground, height_median, floors_3dbag, bag_id, energy_label, energy_index,
                    cbs_population, cbs_households, cbs_avg_household_size, construction_year)
                    SELECT '{VERSION_ID}' as version_id,
                           s.osm_id,
                           gr.grid_result_id,
                           s.area,
                           COALESCE(NULLIF(s.type, ''), s.primary_f_class) AS type,
                           s.primary_f_class AS f_class,
                           COALESCE(NULLIF(s.f_classes, ''), s.primary_f_class) AS f_classes,
                           s.geom,
                           s.households_per_building,
                           s.center,
                           COALESCE(s.peak_load_in_kw, 0) as peak_load_in_kw,
                           s.vertice_id,
                           s.floors,
                           s.connection_point,
                           s.height_max, s.height_ground, s.height_median, s.floors_3dbag, s.bag_id, s.energy_label, s.energy_index,
                           s.cbs_population, s.cbs_households, s.cbs_avg_household_size, s.construction_year
                    FROM selected s
                    JOIN grid_result gr
                    ON s.plz = gr.plz
                   AND s.kcid = gr.kcid
                   AND s.bcid = gr.bcid
                   AND gr.version_id = '{VERSION_ID}'
                   AND gr.country_code = %(cc)s
                    WHERE COALESCE(s.type, '') != 'Transformer'
                    ON CONFLICT (version_id, osm_id) DO UPDATE SET
                        grid_result_id = EXCLUDED.grid_result_id,
                        area = EXCLUDED.area,
                        type = CASE
                            -- Do not let generic residential rows overwrite specific POI classes.
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN COALESCE(NULLIF(buildings_result.type, ''), buildings_result.f_class)
                            ELSE EXCLUDED.type
                        END,
                        f_class = CASE
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN buildings_result.f_class
                            ELSE EXCLUDED.f_class
                        END,
                        f_classes = CASE
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN COALESCE(NULLIF(buildings_result.f_classes, ''), buildings_result.f_class)
                            ELSE COALESCE(NULLIF(EXCLUDED.f_classes, ''), EXCLUDED.f_class)
                        END,
                        geom = EXCLUDED.geom,
                        households_per_building = EXCLUDED.households_per_building,
                        center = EXCLUDED.center,
                        peak_load_in_kw = EXCLUDED.peak_load_in_kw,
                        vertice_id = EXCLUDED.vertice_id,
                        floors = EXCLUDED.floors,
                        connection_point = EXCLUDED.connection_point,
                        height_max = COALESCE(EXCLUDED.height_max, buildings_result.height_max),
                        height_ground = COALESCE(EXCLUDED.height_ground, buildings_result.height_ground),
                        height_median = COALESCE(EXCLUDED.height_median, buildings_result.height_median),
                        floors_3dbag = COALESCE(EXCLUDED.floors_3dbag, buildings_result.floors_3dbag),
                        bag_id = COALESCE(EXCLUDED.bag_id, buildings_result.bag_id),
                        energy_label = COALESCE(EXCLUDED.energy_label, buildings_result.energy_label),
                        energy_index = COALESCE(EXCLUDED.energy_index, buildings_result.energy_index),
                        cbs_population = COALESCE(EXCLUDED.cbs_population, buildings_result.cbs_population),
                        cbs_households = COALESCE(EXCLUDED.cbs_households, buildings_result.cbs_households),
                        cbs_avg_household_size = COALESCE(EXCLUDED.cbs_avg_household_size, buildings_result.cbs_avg_household_size),
                        construction_year = COALESCE(EXCLUDED.construction_year, buildings_result.construction_year);"""
        self.cur.execute(query, {"cc": country_code})

        # Fallback: keep buildings that did not receive (kcid, bcid) assignments.
        # These are mapped to the nearest transformer/grid in the same PLZ so they
        # remain visible in downstream APIs/UI instead of being dropped.
        fallback_query = f"""
                    WITH normalized AS (
                        SELECT
                            bt.*,
                            split_part(bt.osm_id, '_poi_', 1) AS osm_id_base,
                            COALESCE(NULLIF(LOWER(TRIM(bt.f_class)), ''), 'yes') AS f_class_norm
                        FROM {buildings_table} bt
                    ),
                    ranked AS (
                        SELECT
                            n.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY n.osm_id_base, n.plz
                                ORDER BY
                                    CASE
                                        WHEN n.f_class_norm IN ('yes', 'building', 'residential', 'house', 'unclassified', 'other')
                                            THEN 1
                                        ELSE 0
                                    END,
                                    n.f_class_norm
                            ) AS rn
                        FROM normalized n
                    ),
                    class_set AS (
                        SELECT DISTINCT
                            r.osm_id_base,
                            r.plz,
                            r.f_class_norm,
                            CASE
                                WHEN r.f_class_norm IN ('yes', 'building', 'residential', 'house', 'unclassified', 'other')
                                    THEN 1
                                ELSE 0
                            END AS generic_rank
                        FROM ranked r
                    ),
                    aggregated AS (
                        SELECT
                            cs.osm_id_base,
                            cs.plz,
                            (ARRAY_AGG(cs.f_class_norm ORDER BY cs.generic_rank, cs.f_class_norm))[1] AS primary_f_class,
                            ARRAY_TO_STRING(
                                ARRAY_AGG(cs.f_class_norm ORDER BY cs.generic_rank, cs.f_class_norm),
                                ';'
                            ) AS f_classes
                        FROM class_set cs
                        GROUP BY cs.osm_id_base, cs.plz
                    ),
                    selected AS (
                        SELECT
                            r.osm_id_base AS osm_id,
                            r.kcid,
                            r.bcid,
                            r.plz,
                            r.area,
                            r.type,
                            r.geom,
                            r.households_per_building,
                            COALESCE(r.center, ST_Centroid(r.geom)) AS center,
                            r.peak_load_in_kw,
                            r.vertice_id,
                            r.floors,
                            r.connection_point,
                            a.primary_f_class,
                            a.f_classes,
                            r.height_max,
                            r.height_ground,
                            r.height_median,
                            r.floors_3dbag,
                            r.bag_id,
                            r.energy_label,
                            r.energy_index,
                            r.cbs_population,
                            r.cbs_households,
                            r.cbs_avg_household_size,
                            r.construction_year
                        FROM ranked r
                        JOIN aggregated a
                            ON a.osm_id_base = r.osm_id_base
                           AND a.plz = r.plz
                        WHERE r.rn = 1
                    ),
                    unassigned AS (
                        SELECT s.*
                        FROM selected s
                        LEFT JOIN grid_result gr
                            ON s.plz = gr.plz
                           AND s.kcid = gr.kcid
                           AND s.bcid = gr.bcid
                           AND gr.version_id = '{VERSION_ID}'
                           AND gr.country_code = %(cc)s
                        WHERE COALESCE(s.type, '') != 'Transformer'
                          AND gr.grid_result_id IS NULL
                    ),
                    mapped_in_plz AS (
                        SELECT
                            u.*,
                            nearest.grid_result_id AS mapped_grid_result_id
                        FROM unassigned u
                        LEFT JOIN LATERAL (
                            SELECT tp.grid_result_id
                            FROM transformer_positions tp
                            JOIN grid_result gr2
                              ON gr2.grid_result_id = tp.grid_result_id
                             AND gr2.version_id = tp.version_id
                            WHERE tp.version_id = '{VERSION_ID}'
                              AND gr2.plz = u.plz
                              AND gr2.country_code = %(cc)s
                            ORDER BY u.center <-> tp.geom
                            LIMIT 1
                        ) AS nearest ON TRUE
                    ),
                    still_unmapped AS (
                        SELECT m.*
                        FROM mapped_in_plz m
                        WHERE m.mapped_grid_result_id IS NULL
                    ),
                    mapped_any_plz AS (
                        SELECT
                            u.osm_id,
                            u.kcid,
                            u.bcid,
                            u.plz,
                            u.area,
                            u.type,
                            u.geom,
                            u.households_per_building,
                            u.center,
                            u.peak_load_in_kw,
                            u.vertice_id,
                            u.floors,
                            u.connection_point,
                            u.primary_f_class,
                            u.f_classes,
                            u.height_max,
                            u.height_ground,
                            u.height_median,
                            u.floors_3dbag,
                            u.bag_id,
                            u.energy_label,
                            u.energy_index,
                            u.cbs_population,
                            u.cbs_households,
                            u.cbs_avg_household_size,
                            u.construction_year,
                            nearest.grid_result_id AS mapped_grid_result_id
                        FROM still_unmapped u
                        JOIN LATERAL (
                            SELECT tp.grid_result_id
                            FROM transformer_positions tp
                            JOIN grid_result gr2
                              ON gr2.grid_result_id = tp.grid_result_id
                             AND gr2.version_id = tp.version_id
                            WHERE tp.version_id = '{VERSION_ID}'
                              AND gr2.country_code = %(cc)s
                            ORDER BY u.center <-> tp.geom
                            LIMIT 1
                        ) AS nearest ON TRUE
                    ),
                    mapped AS (
                        SELECT * FROM mapped_in_plz WHERE mapped_grid_result_id IS NOT NULL
                        UNION ALL
                        SELECT * FROM mapped_any_plz
                    )
                    INSERT INTO buildings_result
                    (version_id, osm_id, grid_result_id, area, type, f_class, f_classes, geom, households_per_building, center,
                    peak_load_in_kw, vertice_id, floors, connection_point,
                    height_max, height_ground, height_median, floors_3dbag, bag_id, energy_label, energy_index,
                    cbs_population, cbs_households, cbs_avg_household_size, construction_year)
                    SELECT '{VERSION_ID}' as version_id,
                           m.osm_id,
                           m.mapped_grid_result_id,
                           m.area,
                           COALESCE(NULLIF(m.type, ''), m.primary_f_class) AS type,
                           m.primary_f_class AS f_class,
                           COALESCE(NULLIF(m.f_classes, ''), m.primary_f_class) AS f_classes,
                           m.geom,
                           m.households_per_building,
                           m.center,
                           0 as peak_load_in_kw,
                           m.vertice_id,
                           m.floors,
                           m.connection_point,
                           m.height_max, m.height_ground, m.height_median, m.floors_3dbag, m.bag_id, m.energy_label, m.energy_index,
                    m.cbs_population, m.cbs_households, m.cbs_avg_household_size, m.construction_year
                    FROM mapped m
                    ON CONFLICT (version_id, osm_id) DO UPDATE SET
                        grid_result_id = EXCLUDED.grid_result_id,
                        area = EXCLUDED.area,
                        type = CASE
                            -- Do not let generic residential rows overwrite specific POI classes.
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN COALESCE(NULLIF(buildings_result.type, ''), buildings_result.f_class)
                            ELSE EXCLUDED.type
                        END,
                        f_class = CASE
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN buildings_result.f_class
                            ELSE EXCLUDED.f_class
                        END,
                        f_classes = CASE
                            WHEN EXCLUDED.f_class IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            AND COALESCE(buildings_result.f_class, '') NOT IN (
                                'yes','building','residential','house','apartments','apartment',
                                'detached','semidetached_house','terrace','townhouse',
                                'allotment_house','unclassified','other'
                            )
                            THEN COALESCE(NULLIF(buildings_result.f_classes, ''), buildings_result.f_class)
                            ELSE COALESCE(NULLIF(EXCLUDED.f_classes, ''), EXCLUDED.f_class)
                        END,
                        geom = EXCLUDED.geom,
                        households_per_building = EXCLUDED.households_per_building,
                        center = EXCLUDED.center,
                        peak_load_in_kw = EXCLUDED.peak_load_in_kw,
                        vertice_id = EXCLUDED.vertice_id,
                        floors = EXCLUDED.floors,
                        connection_point = EXCLUDED.connection_point,
                        height_max = COALESCE(EXCLUDED.height_max, buildings_result.height_max),
                        height_ground = COALESCE(EXCLUDED.height_ground, buildings_result.height_ground),
                        height_median = COALESCE(EXCLUDED.height_median, buildings_result.height_median),
                        floors_3dbag = COALESCE(EXCLUDED.floors_3dbag, buildings_result.floors_3dbag),
                        bag_id = COALESCE(EXCLUDED.bag_id, buildings_result.bag_id),
                        energy_label = COALESCE(EXCLUDED.energy_label, buildings_result.energy_label),
                        energy_index = COALESCE(EXCLUDED.energy_index, buildings_result.energy_index),
                        cbs_population = COALESCE(EXCLUDED.cbs_population, buildings_result.cbs_population),
                        cbs_households = COALESCE(EXCLUDED.cbs_households, buildings_result.cbs_households),
                        cbs_avg_household_size = COALESCE(EXCLUDED.cbs_avg_household_size, buildings_result.cbs_avg_household_size),
                        construction_year = COALESCE(EXCLUDED.construction_year, buildings_result.construction_year);"""
        if enable_nearest_fallback:
            self.cur.execute(fallback_query, {"cc": country_code})
            self.logger.info(
                "Fallback mapped %s previously-unassigned buildings for PLZ %s",
                self.cur.rowcount,
                plz,
            )
        else:
            self.logger.debug(
                "Nearest-grid fallback disabled for PLZ %s (strict assignment mode).",
                plz,
            )

        # Buildings_result keeps the most specific, source-of-truth f_class/f_classes
        # from res/oth so UI/API don't show stale generic classes from earlier runs.
        sync_query = f"""
                    WITH resolved AS (
                        SELECT
                            br.osm_id,
                            COALESCE(cls.f_class, br.f_class) AS resolved_f_class,
                            COALESCE(
                                NULLIF(cls.f_classes, ''),
                                NULLIF(br.f_classes, ''),
                                COALESCE(cls.f_class, br.f_class)
                            ) AS resolved_f_classes
                        FROM buildings_result br
                        JOIN grid_result gr
                          ON gr.grid_result_id = br.grid_result_id
                         AND gr.version_id = br.version_id
                        LEFT JOIN LATERAL (
                            SELECT
                                t.f_class,
                                COALESCE(NULLIF(t.f_classes, ''), t.f_class) AS f_classes
                            FROM (
                                SELECT r.f_class, r.f_classes FROM res r WHERE r.osm_id = br.osm_id
                                UNION ALL
                                SELECT o.f_class, o.f_classes FROM oth o WHERE o.osm_id = br.osm_id
                            ) t
                            ORDER BY
                                CASE WHEN COALESCE(NULLIF(t.f_classes, ''), t.f_class) LIKE '%%;%%' THEN 0 ELSE 1 END,
                                CASE
                                    WHEN t.f_class IN (
                                        'yes','building','residential','house','apartments','apartment',
                                        'detached','semidetached_house','terrace','townhouse',
                                        'allotment_house','unclassified','other'
                                    ) THEN 1
                                    ELSE 0
                                END,
                                t.f_class
                            LIMIT 1
                        ) cls ON TRUE
                        WHERE br.version_id = '{VERSION_ID}'
                          AND gr.plz = %(p)s
                          AND gr.country_code = %(cc)s
                    )
                    UPDATE buildings_result br
                    SET f_class = r.resolved_f_class,
                        f_classes = r.resolved_f_classes
                    FROM resolved r
                    WHERE br.version_id = '{VERSION_ID}'
                      AND br.osm_id = r.osm_id;"""
        self.cur.execute(sync_query, {"p": str(plz), "cc": country_code})
        self.logger.info(
            "Synced f_class/f_classes from source for %s buildings in PLZ %s",
            self.cur.rowcount,
            plz,
        )

        # Save ways results
        query = f"""INSERT INTO ways_result
                        SELECT '{VERSION_ID}' as version_id, clazz, source, target, cost, reverse_cost, geom, way_id,
                        %(p)s as plz, %(cc)s as country_code FROM {ways_table}
                        ON CONFLICT DO NOTHING;"""

        self.cur.execute(query, vars={"p": str(plz), "cc": country_code})

    def delete_plz_from_all_tables(self, plz: int, version_id: str, country_code: str | None = None) -> None:
        """
        Deletes all entries of corresponding networks in all tables for the given Version ID and plz.
        :param plz: Postal code
        :param version_id: Version ID
        """
        query = """DELETE
                   FROM postcode_result
                   WHERE version_id = %(v)s
                     AND postcode_result_plz = %(p)s"""
        params = {"v": version_id, "p": str(plz)}
        if country_code:
            query += "\n                     AND country_code = %(cc)s"
            params["cc"] = country_code
        query += ";"
        self.cur.execute(query, params)
        self.conn.commit()
        self.logger.info(
            "All data for PLZ %s, version %s%s deleted",
            plz,
            version_id,
            f", country {country_code}" if country_code else "",
        )

    def reconcile_missing_buildings_for_postcode_result(
            self,
            country_code: str = "DE",
            state_code: str = None,
            include_out_of_scope: bool = True,
    ) -> int:
        """Backfill buildings missing from buildings_result after PLZ processing.

        Uses indexed temp tables instead of a single monolithic CTE for
        performance.  The logic is identical to the original approach:
          1. Collect missing buildings into an indexed temp table.
          2. Flag in-scope vs out-of-scope via ST_Intersects on the indexed temp.
          3. Map in-scope buildings to nearest transformer within their postcode.
          4. Fallback: map still-unmapped in-scope to nearest transformer any PLZ.
          5. Optionally map out-of-scope buildings to nearest transformer.
          6. INSERT all mapped buildings into buildings_result.
        """
        self.cur.execute("ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS f_classes text;")
        # Ensure enrichment columns exist
        for col, dtype in [
            ("height_max", "double precision"), ("height_ground", "double precision"),
            ("height_median", "double precision"), ("floors_3dbag", "integer"),
            ("bag_id", "varchar"), ("energy_label", "varchar(5)"), ("energy_index", "double precision"),
            ("cbs_population", "double precision"), ("cbs_households", "double precision"),
            ("cbs_avg_household_size", "double precision"),
        ]:
            self.cur.execute(f"ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS {col} {dtype};")
        for table in ["res", "oth"]:
            for col, dtype in [
                ("height_max", "double precision"), ("height_ground", "double precision"),
                ("height_median", "double precision"), ("floors_3dbag", "integer"),
                ("bag_id", "varchar"), ("energy_label", "varchar(5)"), ("energy_index", "double precision"),
                ("cbs_population", "double precision"), ("cbs_households", "double precision"),
                ("cbs_avg_household_size", "double precision"),
            ]:
                self.cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype};")

        params = {"cc": country_code}
        state_filter_res = ""
        state_filter_oth = ""
        if state_code:
            params["sc"] = state_code.strip().lower()
            state_filter_res = " AND r.state_code = %(sc)s"
            state_filter_oth = " AND o.state_code = %(sc)s"

        # ------------------------------------------------------------------
        # Step 1: Materialise missing buildings into an indexed temp table
        # ------------------------------------------------------------------
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_missing;")
        self.cur.execute(f"""
            CREATE TEMP TABLE _reconcile_missing AS
            SELECT
                s.osm_id, s.area, s.type, s.f_class, s.f_classes,
                ST_MakeValid(s.geom) AS geom,
                ST_Centroid(ST_MakeValid(s.geom)) AS center,
                s.households_per_building, s.floors,
                s.height_max, s.height_ground, s.height_median,
                s.floors_3dbag, s.bag_id, s.energy_label, s.energy_index,
                s.cbs_population, s.cbs_households, s.cbs_avg_household_size,
                s.construction_year
            FROM (
                SELECT
                    r.osm_id, r.area,
                    COALESCE(NULLIF(r.building_t, ''), COALESCE(NULLIF(r.f_class, ''), 'yes')) AS type,
                    COALESCE(NULLIF(LOWER(TRIM(r.f_class)), ''), 'yes') AS f_class,
                    COALESCE(NULLIF(LOWER(TRIM(r.f_classes)), ''),
                             COALESCE(NULLIF(LOWER(TRIM(r.f_class)), ''), 'yes')) AS f_classes,
                    r.geom,
                    COALESCE(r.occupants::double precision, 0) AS households_per_building,
                    COALESCE(r.floors, 1) AS floors,
                    r.height_max, r.height_ground, r.height_median,
                    r.floors_3dbag, r.bag_id, r.energy_label, r.energy_index,
                    r.cbs_population, r.cbs_households, r.cbs_avg_household_size,
                    r.constructi AS construction_year
                FROM res r
                WHERE r.country_code = %(cc)s{state_filter_res}
                UNION ALL
                SELECT
                    o.osm_id, o.area,
                    COALESCE(NULLIF(o.use, ''), COALESCE(NULLIF(o.f_class, ''), 'yes')) AS type,
                    COALESCE(NULLIF(LOWER(TRIM(o.f_class)), ''), 'yes') AS f_class,
                    COALESCE(NULLIF(LOWER(TRIM(o.f_classes)), ''),
                             COALESCE(NULLIF(LOWER(TRIM(o.f_class)), ''), 'yes')) AS f_classes,
                    o.geom,
                    0::double precision AS households_per_building,
                    1::integer AS floors,
                    o.height_max, o.height_ground, o.height_median,
                    o.floors_3dbag, o.bag_id,
                    NULL::varchar(5) AS energy_label, NULL::double precision AS energy_index,
                    o.cbs_population, o.cbs_households, o.cbs_avg_household_size,
                    NULL::text AS construction_year
                FROM oth o
                WHERE o.country_code = %(cc)s{state_filter_oth}
            ) s
            LEFT JOIN buildings_result br
              ON br.version_id = '{VERSION_ID}' AND br.osm_id = s.osm_id
            WHERE br.osm_id IS NULL AND s.geom IS NOT NULL
              AND ST_Area(s.geom) >= 25;
        """, params)

        self.cur.execute("CREATE INDEX ON _reconcile_missing USING gist (geom);")
        self.cur.execute("CREATE INDEX ON _reconcile_missing USING gist (center);")
        self.cur.execute("CREATE INDEX ON _reconcile_missing (osm_id);")
        self.cur.execute("ANALYZE _reconcile_missing;")

        self.cur.execute("SELECT count(*) FROM _reconcile_missing;")
        missing_count = self.cur.fetchone()[0]
        self.logger.info("Reconcile: %s missing buildings found", missing_count)
        if missing_count == 0:
            self.cur.execute("DROP TABLE IF EXISTS _reconcile_missing;")
            return 0

        # ------------------------------------------------------------------
        # Step 2: Flag in-scope vs out-of-scope using spatial join
        # ------------------------------------------------------------------
        BATCH_SIZE = 10000
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_scope;")
        # Spatial join is much faster than correlated EXISTS subquery:
        # PostgreSQL uses both GIST indexes to find overlaps efficiently.
        self.cur.execute(f"""
            CREATE TEMP TABLE _reconcile_scope AS
            SELECT
                m.osm_id,
                bool_or(pr.version_id IS NOT NULL) AS in_postcode_scope
            FROM _reconcile_missing m
            LEFT JOIN postcode_result pr
              ON ST_Intersects(m.geom, pr.geom)
             AND pr.version_id = '{VERSION_ID}'
             AND pr.country_code = %(cc)s
            GROUP BY m.osm_id;
        """, params)
        self.cur.execute("CREATE INDEX ON _reconcile_scope (osm_id);")
        self.cur.execute("ANALYZE _reconcile_scope;")

        self.cur.execute("SELECT count(*) FILTER (WHERE in_postcode_scope), "
                         "count(*) FILTER (WHERE NOT in_postcode_scope) FROM _reconcile_scope;")
        in_count, out_count = self.cur.fetchone()
        self.logger.info("Scope check done: %s in-scope, %s out-of-scope", in_count, out_count)

        # ------------------------------------------------------------------
        # Step 3: In-scope → nearest transformer within same postcode (batched)
        # ------------------------------------------------------------------
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_mapped;")
        self.cur.execute("""
            CREATE TEMP TABLE _reconcile_mapped (
                osm_id varchar,
                mapped_grid_result_id bigint
            );
        """)

        self.cur.execute("""
            SELECT m.osm_id FROM _reconcile_missing m
            JOIN _reconcile_scope sc ON sc.osm_id = m.osm_id AND sc.in_postcode_scope
            ORDER BY m.osm_id;
        """)
        in_scope_ids = [row[0] for row in self.cur.fetchall()]
        in_scope_count = len(in_scope_ids)
        self.logger.info("Step 3: mapping %s in-scope buildings to nearest transformer in same PLZ", in_scope_count)

        for batch_start in range(0, len(in_scope_ids), BATCH_SIZE):
            batch_ids = in_scope_ids[batch_start:batch_start + BATCH_SIZE]
            self.cur.execute(f"""
                INSERT INTO _reconcile_mapped (osm_id, mapped_grid_result_id)
                SELECT m.osm_id, nearest.grid_result_id
                FROM _reconcile_missing m
                LEFT JOIN LATERAL (
                    SELECT tp.grid_result_id
                    FROM transformer_positions tp
                    JOIN grid_result gr2
                      ON gr2.grid_result_id = tp.grid_result_id
                     AND gr2.version_id = tp.version_id
                    JOIN postcode_result pr2
                      ON pr2.version_id = gr2.version_id
                     AND pr2.country_code = gr2.country_code
                     AND pr2.postcode_result_plz = gr2.plz
                    WHERE tp.version_id = '{VERSION_ID}'
                      AND gr2.country_code = %(cc)s
                      AND ST_Intersects(m.geom, pr2.geom)
                    ORDER BY m.center <-> tp.geom
                    LIMIT 1
                ) AS nearest ON TRUE
                WHERE m.osm_id = ANY(%(batch_ids)s);
            """, {**params, "batch_ids": batch_ids})
            done = min(batch_start + BATCH_SIZE, in_scope_count)
            self.logger.info("In-scope mapping: %s/%s (%.1f%%)", done, in_scope_count,
                             done * 100.0 / max(in_scope_count, 1))

        # ------------------------------------------------------------------
        # Step 4: Still-unmapped in-scope → nearest transformer any PLZ
        # ------------------------------------------------------------------
        self.cur.execute("""
            SELECT rm.osm_id FROM _reconcile_mapped rm
            WHERE rm.mapped_grid_result_id IS NULL;
        """)
        unmapped_ids = [row[0] for row in self.cur.fetchall()]
        self.logger.info("Step 4: %s in-scope buildings still unmapped, trying any PLZ", len(unmapped_ids))

        for batch_start in range(0, len(unmapped_ids), BATCH_SIZE):
            batch_ids = unmapped_ids[batch_start:batch_start + BATCH_SIZE]
            self.cur.execute(f"""
                INSERT INTO _reconcile_mapped (osm_id, mapped_grid_result_id)
                SELECT m.osm_id, nearest.grid_result_id
                FROM _reconcile_missing m
                JOIN LATERAL (
                    SELECT tp.grid_result_id
                    FROM transformer_positions tp
                    JOIN grid_result gr2
                      ON gr2.grid_result_id = tp.grid_result_id
                     AND gr2.version_id = tp.version_id
                    WHERE tp.version_id = '{VERSION_ID}'
                      AND gr2.country_code = %(cc)s
                    ORDER BY m.center <-> tp.geom
                    LIMIT 1
                ) AS nearest ON TRUE
                WHERE m.osm_id = ANY(%(batch_ids)s);
            """, {**params, "batch_ids": batch_ids})
            done = min(batch_start + BATCH_SIZE, len(unmapped_ids))
            self.logger.info("Fallback mapping: %s/%s (%.1f%%)", done, len(unmapped_ids),
                             done * 100.0 / max(len(unmapped_ids), 1))

        # Remove NULL placeholders so they don't interfere with final insert
        self.cur.execute("DELETE FROM _reconcile_mapped WHERE mapped_grid_result_id IS NULL;")

        # ------------------------------------------------------------------
        # Step 5: Out-of-scope → nearest transformer (if enabled, batched)
        # ------------------------------------------------------------------
        if include_out_of_scope:
            self.cur.execute("""
                SELECT sc.osm_id FROM _reconcile_scope sc
                LEFT JOIN _reconcile_mapped rm ON rm.osm_id = sc.osm_id
                WHERE NOT sc.in_postcode_scope AND rm.osm_id IS NULL;
            """)
            oos_ids = [row[0] for row in self.cur.fetchall()]
            self.logger.info("Step 5: mapping %s out-of-scope buildings", len(oos_ids))

            for batch_start in range(0, len(oos_ids), BATCH_SIZE):
                batch_ids = oos_ids[batch_start:batch_start + BATCH_SIZE]
                self.cur.execute(f"""
                    INSERT INTO _reconcile_mapped (osm_id, mapped_grid_result_id)
                    SELECT m.osm_id, nearest.grid_result_id
                    FROM _reconcile_missing m
                    JOIN LATERAL (
                        SELECT tp.grid_result_id
                        FROM transformer_positions tp
                        JOIN grid_result gr2
                          ON gr2.grid_result_id = tp.grid_result_id
                         AND gr2.version_id = tp.version_id
                        WHERE tp.version_id = '{VERSION_ID}'
                          AND gr2.country_code = %(cc)s
                        ORDER BY m.center <-> tp.geom
                        LIMIT 1
                    ) AS nearest ON TRUE
                    WHERE m.osm_id = ANY(%(batch_ids)s);
                """, {**params, "batch_ids": batch_ids})
                done = min(batch_start + BATCH_SIZE, len(oos_ids))
                self.logger.info("Out-of-scope mapping: %s/%s (%.1f%%)", done, len(oos_ids),
                                 done * 100.0 / max(len(oos_ids), 1))

        # ------------------------------------------------------------------
        # Step 6: Final INSERT into buildings_result
        # ------------------------------------------------------------------
        self.cur.execute(f"""
            INSERT INTO buildings_result
            (version_id, osm_id, grid_result_id, area, type, f_class, f_classes,
             geom, households_per_building, center,
             peak_load_in_kw, vertice_id, floors, connection_point,
             height_max, height_ground, height_median, floors_3dbag, bag_id,
             energy_label, energy_index,
             cbs_population, cbs_households, cbs_avg_household_size, construction_year)
            SELECT
                '{VERSION_ID}', m.osm_id, rm.mapped_grid_result_id,
                m.area, m.type, m.f_class, m.f_classes,
                m.geom, m.households_per_building, m.center,
                0, NULL, m.floors, NULL,
                m.height_max, m.height_ground, m.height_median,
                m.floors_3dbag, m.bag_id, m.energy_label, m.energy_index,
                m.cbs_population, m.cbs_households, m.cbs_avg_household_size,
                m.construction_year
            FROM _reconcile_mapped rm
            JOIN _reconcile_missing m ON m.osm_id = rm.osm_id
            WHERE rm.mapped_grid_result_id IS NOT NULL
            ON CONFLICT (version_id, osm_id) DO NOTHING;
        """)
        inserted = self.cur.rowcount

        # Clean up
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_mapped;")
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_scope;")
        self.cur.execute("DROP TABLE IF EXISTS _reconcile_missing;")

        self.logger.info(
            "Reconciled %s missing buildings for %s/%s (include_out_of_scope=%s)",
            inserted, country_code, state_code or "*", include_out_of_scope,
        )
        return inserted

    def delete_version_from_all_tables(self, version_id: str) -> None:
        """Delete all entries of the given version ID from all tables."""
        query = "DELETE FROM version WHERE version_id = %(v)s;"
        self.cur.execute(query, {"v": version_id})
        self.conn.commit()
        self.logger.info(f"Version {version_id} deleted from all tables")

    def delete_classification_version_from_related_tables(self, classification_id: str) -> None:
        """
        Deletes all rows with the given classification_id from related tables:
        transformer_classified, sample_set, and classification_version.

        :param classification_id: ID of the classification version to delete
        """
        query = "DELETE FROM classification_version WHERE classification_id = %(cid)s;"
        self.cur.execute(query, {"cid": classification_id})
        self.conn.commit()

        self.logger.info(f"Deleted classification ID {classification_id}.")

    def delete_plz_from_sample_set_table(self, classification_id: str, plz: int) -> None:
        """
        Deletes the row corresponding to the given classification ID and PLZ from the sample_set table.

        :param classification_id: ID of the classification version
        :param plz: Postal code to be removed
        """
        query = """
                DELETE
                FROM sample_set
                WHERE classification_id = %(cid)s
                  AND plz = %(p)s; \
                """
        self.cur.execute(query, {"cid": classification_id, "p": plz})
        self.conn.commit()
        self.logger.info(f"Deleted PLZ {plz} for classification ID {classification_id} from sample_set table.")

    def delete_transformers(self) -> None:
        """all transformers are deleted from table transformers in database"""
        delete_query = "TRUNCATE TABLE transformers;"
        self.cur.execute(delete_query)
        self.conn.commit()
        self.logger.info('Transformers deleted.')

    def write_ags_log(self, ags: int) -> None:
        """write ags log to database: the amtliche gemeindeschluessel of the municipalities of which the buildings
        have already been imported to the database
        :param ags:  ags to be added
        :rtype ags: numpy integer 64
         """
        query = """INSERT INTO ags_log (ags)
                   VALUES (%(a)s); """
        self.cur.execute(query, {"a": int(ags), })
        self.conn.commit()
