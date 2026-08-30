import warnings
from abc import ABC
import re
from pathlib import Path
from typing import Optional

from config.config_table_structure import *
from src.config_loader import *
from src.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class UtilsMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

    def __del__(self):
        self.cur.close()
        self.conn.close()

    def create_temp_tables(self, plz: int) -> None:
        """Create PLZ-suffixed temporary tables and session-local views."""
        self.drop_temp_tables(plz)
        for base_name, query in TEMP_CREATE_QUERIES.items():
            table_name = f"{base_name}_{plz}"
            # create a dedicated table for each PLZ
            self.cur.execute(query.replace(base_name, table_name))

            # expose a session-local view with the common name
            self.cur.execute(f"CREATE TEMP VIEW {base_name} AS SELECT * FROM {table_name}")
            # self.cur.execute(f"CREATE OR REPLACE VIEW {base_name} AS SELECT * FROM {table_name}") #only for debugging

    def ensure_temp_views(self, plz: int) -> None:
        """Recreate temp views if they were lost (e.g. due to implicit rollback)."""
        for base_name in TEMP_CREATE_QUERIES.keys():
            table_name = f"{base_name}_{plz}"
            self.cur.execute(
                "SELECT 1 FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND n.nspname LIKE 'pg_temp_%%'",
                (base_name,),
            )
            if not self.cur.fetchone():
                self.cur.execute(f"CREATE TEMP VIEW {base_name} AS SELECT * FROM {table_name}")

    def create_buildings_tem_indexes(self, plz: int) -> None:
        """Create indexes for buildings temp table after bulk insert."""
        table_name = f"buildings_tem_{plz}"
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_geom ON {table_name} USING GIST (geom)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_center ON {table_name} USING GIST (center)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_vertice_id ON {table_name} (vertice_id)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_connection_point ON {table_name} (connection_point)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_kcid ON {table_name} (kcid)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_kcid_bcid ON {table_name} (kcid, bcid)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_plz ON {table_name} (plz)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_type ON {table_name} (type)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_f_class ON {table_name} (f_class)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_osm_id ON {table_name} (osm_id)")

    def create_ways_tem_indexes(self, plz: int) -> None:
        """Create indexes for ways temp table after bulk insert."""
        table_name = f"ways_tem_{plz}"
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_geom ON {table_name} USING GIST (geom)")
        self.cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_way_id ON {table_name} (way_id)")

    def drop_temp_tables(self, plz: int) -> None:
        """Drop PLZ-suffixed tables and their views."""
        for base_name in TEMP_CREATE_QUERIES.keys():
            self.cur.execute(f"DROP VIEW IF EXISTS {base_name} CASCADE")
            self.cur.execute(f"DROP TABLE IF EXISTS {base_name}_{plz} CASCADE")
        self.cur.execute("DROP VIEW IF EXISTS ways_tem_vertices_pgr CASCADE")
        # Drop the vertices table created by pgr_createTopology (correct naming pattern)
        self.cur.execute(f"DROP TABLE IF EXISTS ways_tem_{plz}_vertices_pgr CASCADE")

    def analyze_temp_tables(self, plz: int) -> None:
        """Run ANALYZE on PLZ-suffixed temporary tables to update planner statistics."""
        for base_name in TEMP_CREATE_QUERIES.keys():
            self.cur.execute(f"ANALYZE {base_name}_{plz}")

    def refresh_materialized_views(self) -> None:
        # Commit any pending transaction before switching to autocommit.
        try:
            self.conn.commit()
        except Exception:
            self.conn.rollback()

        old_autocommit = self.conn.autocommit
        self.conn.autocommit = True
        try:
            for name, query in REFRESH_QUERIES.items():
                self.logger.info(f"Refreshing materialized view: {name} ...")
                try:
                    self.cur.execute(query)
                except Exception:
                    # CONCURRENTLY fails on first refresh when MV has no unique index;
                    # fall back to regular refresh.
                    self.cur.execute(query.replace("CONCURRENTLY ", ""))
                self.logger.info(f"Refreshed materialized view: {name}")
        finally:
            self.conn.autocommit = old_autocommit

    def commit_changes(self):
        self.conn.commit()

    def get_list_from_plz(self, plz: int, country_code: str | None = None) -> list:
        query = """SELECT DISTINCT kcid, bcid
                   FROM grid_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s"""
        params = {"p": plz, "v": VERSION_ID}
        if country_code:
            query += "\n                     AND country_code = %(cc)s"
            params["cc"] = country_code
        query += """
                   ORDER BY kcid, bcid;"""
        self.cur.execute(query, params)
        cluster_list = self.cur.fetchall()

        return cluster_list

    def delete_transformers_from_buildings_tem(self, vertices: list) -> None:
        """
        Deletes selected transformers from buildings_tem
        :param vertices:
        :return:
        """
        query = """
                DELETE
                FROM buildings_tem
                WHERE vertice_id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_consumer_categories(self):
        """
        Returns: A dataframe with self-defined consumer categories and typical values
        """
        query = """SELECT *
                   FROM consumer_categories"""
        cc_df = pd.read_sql_query(query, self.conn)
        cc_df.set_index("definition", drop=False, inplace=True)
        cc_df.sort_index(inplace=True)
        self.logger.debug("Consumer categories fetched.")
        return cc_df

    @staticmethod
    def normalize_state_code(state_code: Optional[str]) -> Optional[str]:
        """Normalize state identifiers to a stable DB key format."""
        if state_code is None:
            return None
        normalized = str(state_code).strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        return normalized or None

    @staticmethod
    def _default_state_name_from_code(state_code: str) -> str:
        return str(state_code).replace("_", " ").strip().title()

    def _ensure_country_state_tables(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(CREATE_QUERIES["country"])
            cur.execute(CREATE_QUERIES["state"])
            cur.execute(SEED_QUERIES["country"])
        self.conn.commit()

    def _resolve_country_code(self, country: Optional[str]) -> Optional[str]:
        """Resolve country argument that can be ISO code or full country name."""
        if country is None:
            return None
        candidate = str(country).strip()
        if not candidate:
            return None
        if len(candidate) == 2 and candidate.isalpha():
            return candidate.upper()
        return get_country_code(candidate.lower())

    def _load_regions_config(self) -> dict:
        config_path = Path(__file__).resolve().parents[2] / "datapipeline" / "config" / "regions.yaml"
        if not config_path.exists():
            self.logger.warning("regions.yaml not found at %s", config_path)
            return {}
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def ensure_state_entry(
            self,
            country: str,
            state_code: str,
            state_name: Optional[str] = None,
            osm_relation_id: Optional[int] = None,
            nuts_code: Optional[str] = None,
    ) -> int:
        """Upsert one state row for a country."""
        country_code = self._resolve_country_code(country)
        state_norm = self.normalize_state_code(state_code)
        if not country_code:
            raise ValueError("country must not be empty")
        if not state_norm:
            raise ValueError("state_code must not be empty")

        self._ensure_country_state_tables()
        display_name = (state_name or self._default_state_name_from_code(state_norm)).strip()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO state (state_code, state_name, country_code, osm_relation_id, nuts_code)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (state_code, country_code) DO UPDATE
                SET state_name = EXCLUDED.state_name,
                    osm_relation_id = COALESCE(EXCLUDED.osm_relation_id, state.osm_relation_id),
                    nuts_code = COALESCE(EXCLUDED.nuts_code, state.nuts_code)
                """,
                (state_norm, display_name, country_code, osm_relation_id, nuts_code),
            )
            affected = cur.rowcount
        self.conn.commit()
        return affected

    def sync_states_from_regions(self, country: Optional[str] = None) -> int:
        """Sync state table from datapipeline/config/regions.yaml."""
        self._ensure_country_state_tables()
        country_filter = self._resolve_country_code(country)
        regions = self._load_regions_config()
        if not regions:
            return 0

        affected = 0
        with self.conn.cursor() as cur:
            for country_key, country_cfg in regions.items():
                country_code = self._resolve_country_code(str(country_key))
                if not country_code:
                    continue
                if country_filter and country_code != country_filter:
                    continue

                states = (country_cfg or {}).get("states") or {}
                for state_key, state_cfg in states.items():
                    state_norm = self.normalize_state_code(str(state_key))
                    if not state_norm:
                        continue

                    state_name = str((state_cfg or {}).get("name") or state_key).strip()
                    osm_relation_id = (state_cfg or {}).get("osm_relation_id")
                    try:
                        osm_relation_id = int(osm_relation_id) if osm_relation_id is not None else None
                    except (TypeError, ValueError):
                        osm_relation_id = None
                    nuts_code = (state_cfg or {}).get("nuts_code")

                    cur.execute(
                        """
                        INSERT INTO state (state_code, state_name, country_code, osm_relation_id, nuts_code)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (state_code, country_code) DO UPDATE
                        SET state_name = EXCLUDED.state_name,
                            osm_relation_id = COALESCE(EXCLUDED.osm_relation_id, state.osm_relation_id),
                            nuts_code = COALESCE(EXCLUDED.nuts_code, state.nuts_code)
                        """,
                        (state_norm, state_name, country_code, osm_relation_id, nuts_code),
                    )
                    affected += cur.rowcount
        self.conn.commit()
        return affected

    def normalize_postcode_state_codes(self, country: Optional[str] = None) -> int:
        """Normalize postcode.state_code values to canonical key format."""
        country_filter = self._resolve_country_code(country)
        normalized_expr = (
            "NULLIF(regexp_replace(regexp_replace(lower(trim(state_code)), '[^a-z0-9]+', '_', 'g'),"
            " '^_+|_+$', '', 'g'), '')"
        )
        with self.conn.cursor() as cur:
            if country_filter:
                cur.execute(
                    f"""
                    UPDATE postcode
                    SET state_code = {normalized_expr}
                    WHERE country_code = %s
                      AND state_code IS NOT NULL
                      AND state_code IS DISTINCT FROM {normalized_expr}
                    """,
                    (country_filter,),
                )
            else:
                cur.execute(
                    f"""
                    UPDATE postcode
                    SET state_code = {normalized_expr}
                    WHERE state_code IS NOT NULL
                      AND state_code IS DISTINCT FROM {normalized_expr}
                    """
                )
            affected = cur.rowcount
        self.conn.commit()
        return affected

    def sync_states_from_postcodes(self, country: Optional[str] = None) -> int:
        """Create missing state rows from postcode records."""
        self._ensure_country_state_tables()
        country_filter = self._resolve_country_code(country)
        with self.conn.cursor() as cur:
            if country_filter:
                cur.execute(
                    """
                    INSERT INTO state (state_code, state_name, country_code)
                    SELECT DISTINCT
                        p.state_code,
                        INITCAP(REPLACE(p.state_code, '_', ' ')) AS state_name,
                        p.country_code
                    FROM postcode p
                    LEFT JOIN state s
                      ON s.state_code = p.state_code
                     AND s.country_code = p.country_code
                    WHERE p.country_code = %s
                      AND p.state_code IS NOT NULL
                      AND p.state_code <> ''
                      AND s.state_id IS NULL
                    """,
                    (country_filter,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO state (state_code, state_name, country_code)
                    SELECT DISTINCT
                        p.state_code,
                        INITCAP(REPLACE(p.state_code, '_', ' ')) AS state_name,
                        p.country_code
                    FROM postcode p
                    LEFT JOIN state s
                      ON s.state_code = p.state_code
                     AND s.country_code = p.country_code
                    WHERE p.state_code IS NOT NULL
                      AND p.state_code <> ''
                      AND s.state_id IS NULL
                    """
                )
            affected = cur.rowcount
        self.conn.commit()
        return affected

    def ensure_postcode_state_fk(self) -> None:
        """Ensure postcode has FK to state (important for proper cascade delete by state)."""
        self._ensure_country_state_tables()
        postcode_table = f"{TARGET_SCHEMA}.postcode"
        state_table = f"{TARGET_SCHEMA}.state"

        # fk_postcode_state_country is table-wide, so every country already in
        # postcode must have its state rows before it can be added. Callers only
        # sync the country they are processing, so backfill the rest here or the
        # constraint fails on a multi-country DB. Idempotent, inserts only gaps.
        self.sync_states_from_postcodes()

        with self.conn.cursor() as cur:
            cur.execute("ALTER TABLE postcode ADD COLUMN IF NOT EXISTS state_code varchar(50);")
            cur.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'fk_postcode_state_country'
                          AND conrelid = '{postcode_table}'::regclass
                    ) THEN
                        ALTER TABLE {postcode_table}
                        ADD CONSTRAINT fk_postcode_state_country
                        FOREIGN KEY (state_code, country_code)
                        REFERENCES {state_table} (state_code, country_code)
                        ON DELETE CASCADE;
                    END IF;
                END
                $$;
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_postcode_state ON postcode (state_code);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_postcode_country_state ON postcode (country_code, state_code);")
        self.conn.commit()

    def sync_postcode_result_state_codes(
            self,
            country: Optional[str] = None,
            state_code: Optional[str] = None
    ) -> int:
        """Propagate postcode.state_code into postcode_result for consistency."""
        country_filter = self._resolve_country_code(country)
        state_filter = self.normalize_state_code(state_code)
        params = {}
        where_parts = []
        if country_filter:
            where_parts.append("pr.country_code = %(cc)s")
            params["cc"] = country_filter
        if state_filter:
            where_parts.append("p.state_code = %(sc)s")
            params["sc"] = state_filter
        where_sql = ""
        if where_parts:
            where_sql = " AND " + " AND ".join(where_parts)

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE postcode_result pr
                   SET state_code = p.state_code
                  FROM postcode p
                 WHERE p.plz = pr.postcode_result_plz
                   AND p.country_code = pr.country_code
                   AND COALESCE(pr.state_code, '') IS DISTINCT FROM COALESCE(p.state_code, '')
                   {where_sql}
                """,
                params,
            )
            affected = cur.rowcount
        self.conn.commit()
        return affected

    def ensure_state_relationships(
            self,
            country: Optional[str] = None,
            state: Optional[str] = None,
            state_name: Optional[str] = None
    ) -> dict:
        """Ensure state registry and FK relations are in place for country/state scoped runs."""
        self._ensure_country_state_tables()
        country_code = self._resolve_country_code(country)
        normalized = {
            "country_code": country_code,
            "normalized_postcode_state_codes": self.normalize_postcode_state_codes(country=country_code),
            "states_synced_from_regions": self.sync_states_from_regions(country=country_code),
        }

        if country_code and state:
            normalized["state_entry_upserted"] = self.ensure_state_entry(
                country=country_code,
                state_code=state,
                state_name=state_name,
            )

        normalized["states_synced_from_postcodes"] = self.sync_states_from_postcodes(country=country_code)
        self.ensure_postcode_state_fk()
        normalized["postcode_result_state_synced"] = self.sync_postcode_result_state_codes(country=country_code)
        return normalized

    def get_state_grid_stats(self, country: str, version_id: Optional[str] = None) -> list[dict]:
        """Return state-level stats including grid counts for one country."""
        self._ensure_country_state_tables()
        country_code = self._resolve_country_code(country)
        if not country_code:
            raise ValueError("country must not be empty")

        if version_id is None:
            version_id = VERSION_ID

        # Keep the registry fresh before reading stats.
        self.sync_states_from_regions(country=country_code)
        self.sync_states_from_postcodes(country=country_code)
        self.sync_postcode_result_state_codes(country=country_code)

        query = """
            SELECT
                s.state_code,
                s.state_name,
                s.country_code,
                s.osm_relation_id,
                s.nuts_code,
                COALESCE(pc.postcode_count, 0)::bigint AS postcode_count,
                COALESCE(pr.postcode_result_count, 0)::bigint AS postcode_result_count,
                COALESCE(gr.grid_count, 0)::bigint AS grid_count
            FROM state s
            LEFT JOIN (
                SELECT p.country_code, p.state_code, COUNT(*) AS postcode_count
                FROM postcode p
                WHERE p.country_code = %(cc)s
                GROUP BY p.country_code, p.state_code
            ) pc
              ON pc.country_code = s.country_code
             AND pc.state_code = s.state_code
            LEFT JOIN (
                SELECT p.country_code, p.state_code, COUNT(*) AS postcode_result_count
                FROM postcode_result pr
                JOIN postcode p
                  ON p.country_code = pr.country_code
                 AND p.plz = pr.postcode_result_plz
                WHERE pr.version_id = %(v)s
                  AND p.country_code = %(cc)s
                GROUP BY p.country_code, p.state_code
            ) pr
              ON pr.country_code = s.country_code
             AND pr.state_code = s.state_code
            LEFT JOIN (
                SELECT p.country_code, p.state_code, COUNT(DISTINCT gr.grid_result_id) AS grid_count
                FROM grid_result gr
                JOIN postcode p
                  ON p.country_code = gr.country_code
                 AND p.plz = gr.plz
                WHERE gr.version_id = %(v)s
                  AND p.country_code = %(cc)s
                GROUP BY p.country_code, p.state_code
            ) gr
              ON gr.country_code = s.country_code
             AND gr.state_code = s.state_code
            WHERE s.country_code = %(cc)s
            ORDER BY grid_count DESC, postcode_count DESC, s.state_name ASC
        """

        with self.conn.cursor() as cur:
            cur.execute(query, {"cc": country_code, "v": version_id})
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

        return [dict(zip(columns, row)) for row in rows]

    def get_state_delete_impact(self, country: str, state_code: str) -> dict:
        """Estimate rows affected when deleting one state."""
        self._ensure_country_state_tables()
        country_code = self._resolve_country_code(country)
        state_norm = self.normalize_state_code(state_code)
        if not country_code:
            raise ValueError("country must not be empty")
        if not state_norm:
            raise ValueError("state_code must not be empty")

        query = """
            WITH plzs AS (
                SELECT p.plz
                FROM postcode p
                WHERE p.country_code = %(cc)s
                  AND p.state_code = %(sc)s
            ),
            grids AS (
                SELECT gr.grid_result_id
                FROM grid_result gr
                JOIN plzs p ON p.plz = gr.plz
                WHERE gr.country_code = %(cc)s
            )
            SELECT
                (SELECT COUNT(*) FROM state s WHERE s.country_code = %(cc)s AND s.state_code = %(sc)s) AS state_rows,
                (SELECT COUNT(*) FROM postcode p WHERE p.country_code = %(cc)s AND p.state_code = %(sc)s) AS postcode_rows,
                (SELECT COUNT(*) FROM postcode_result pr JOIN plzs p ON p.plz = pr.postcode_result_plz WHERE pr.country_code = %(cc)s) AS postcode_result_rows,
                (SELECT COUNT(*) FROM grid_result gr JOIN plzs p ON p.plz = gr.plz WHERE gr.country_code = %(cc)s) AS grid_result_rows,
                (SELECT COUNT(*) FROM lines_result lr JOIN grids g ON g.grid_result_id = lr.grid_result_id) AS lines_result_rows,
                (SELECT COUNT(*) FROM buildings_result br JOIN grids g ON g.grid_result_id = br.grid_result_id) AS buildings_result_rows,
                (SELECT COUNT(*) FROM transformer_positions tp JOIN grids g ON g.grid_result_id = tp.grid_result_id) AS transformer_positions_rows,
                (SELECT COUNT(*) FROM building_transformer_assignments bta JOIN grids g ON g.grid_result_id = bta.grid_result_id) AS building_transformer_assignments_rows,
                (SELECT COUNT(*) FROM clustering_parameters cp JOIN grids g ON g.grid_result_id = cp.grid_result_id) AS clustering_parameters_rows,
                (SELECT COUNT(*) FROM transformer_classified tc JOIN grids g ON g.grid_result_id = tc.grid_result_id) AS transformer_classified_rows,
                (SELECT COUNT(*) FROM ways_result wr JOIN plzs p ON p.plz = wr.plz WHERE wr.country_code = %(cc)s) AS ways_result_rows,
                (SELECT COUNT(*) FROM municipal_register mr WHERE mr.country_code = %(cc)s AND mr.state_code = %(sc)s) AS municipal_register_rows,
                (SELECT COUNT(*) FROM res r WHERE r.country_code = %(cc)s AND r.state_code = %(sc)s) AS res_rows,
                (SELECT COUNT(*) FROM oth o WHERE o.country_code = %(cc)s AND o.state_code = %(sc)s) AS oth_rows,
                (SELECT COUNT(*) FROM ways w WHERE w.country_code = %(cc)s AND w.state_code = %(sc)s) AS ways_rows,
                (SELECT COUNT(*) FROM transformers t WHERE t.country_code = %(cc)s AND t.state_code = %(sc)s) AS transformers_rows
        """

        params = {"cc": country_code, "sc": state_norm}
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            columns = [desc[0] for desc in cur.description]

        impact = dict(zip(columns, row))
        impact["country_code"] = country_code
        impact["state_code"] = state_norm
        return impact

    def delete_state_data(
            self,
            country: str,
            state_code: str,
            dry_run: bool = True,
            drop_state_row: bool = True
    ) -> dict:
        """Delete one state scope consistently across raw and generated tables."""
        self._ensure_country_state_tables()
        country_code = self._resolve_country_code(country)
        state_norm = self.normalize_state_code(state_code)
        if not country_code:
            raise ValueError("country must not be empty")
        if not state_norm:
            raise ValueError("state_code must not be empty")

        impact = self.get_state_delete_impact(country=country_code, state_code=state_norm)
        if dry_run:
            return {
                "dry_run": True,
                "country_code": country_code,
                "state_code": state_norm,
                "impact": impact,
                "deleted": {},
            }

        deleted = {}
        with self.conn.cursor() as cur:
            # Raw datapipeline tables (not FK-linked to postcode/state).
            cur.execute("DELETE FROM res WHERE country_code = %s AND state_code = %s", (country_code, state_norm))
            deleted["res_rows"] = cur.rowcount

            cur.execute("DELETE FROM oth WHERE country_code = %s AND state_code = %s", (country_code, state_norm))
            deleted["oth_rows"] = cur.rowcount

            cur.execute("DELETE FROM ways WHERE country_code = %s AND state_code = %s", (country_code, state_norm))
            deleted["ways_rows"] = cur.rowcount

            cur.execute(
                "DELETE FROM transformers WHERE country_code = %s AND state_code = %s",
                (country_code, state_norm),
            )
            deleted["transformers_rows"] = cur.rowcount

            cur.execute(
                "DELETE FROM municipal_register WHERE country_code = %s AND state_code = %s",
                (country_code, state_norm),
            )
            deleted["municipal_register_rows"] = cur.rowcount

            # Explicitly delete postcode_result rows for this state's postcodes first.
            # This works even on older DBs where FK chains may be incomplete.
            cur.execute(
                """
                DELETE FROM postcode_result pr
                USING postcode p
                WHERE p.country_code = %s
                  AND p.state_code = %s
                  AND p.country_code = pr.country_code
                  AND p.plz = pr.postcode_result_plz
                """,
                (country_code, state_norm),
            )
            deleted["postcode_result_rows"] = cur.rowcount

            cur.execute("DELETE FROM postcode WHERE country_code = %s AND state_code = %s", (country_code, state_norm))
            deleted["postcode_rows"] = cur.rowcount

            # Cleanup legacy rows that may have state_code set without postcode FK consistency.
            cur.execute(
                "DELETE FROM postcode_result WHERE country_code = %s AND state_code = %s",
                (country_code, state_norm),
            )
            deleted["postcode_result_legacy_rows"] = cur.rowcount

            if drop_state_row:
                cur.execute("DELETE FROM state WHERE country_code = %s AND state_code = %s", (country_code, state_norm))
                deleted["state_rows"] = cur.rowcount

        self.conn.commit()
        return {
            "dry_run": False,
            "country_code": country_code,
            "state_code": state_norm,
            "impact": impact,
            "deleted": deleted,
        }
