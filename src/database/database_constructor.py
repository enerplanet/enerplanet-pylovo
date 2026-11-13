import subprocess
import time
import warnings
import uuid
import re
from collections import Counter
from pathlib import Path

import psycopg2 as psy
from psycopg2 import sql as psy_sql
import sqlparse
import pandas as pd

from src.config_loader import *
from config.config_table_structure import *
import src.database.database_client as dbc
from src.infdb.infdb_client import InfdbClient
from src.data_import.import_transformers import process_trafos, get_trafos_processed_3035_geojson_path, \
    fetch_trafos, RELATION_ID, EPSG, get_trafos_processed_geojson_path

try:
    from datapipeline.processors.building_processor import BuildingProcessor
except Exception:
    BuildingProcessor = None


if BuildingProcessor is not None:
    RESIDENTIAL_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.RESIDENTIAL_TYPES}
    COMMERCIAL_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.COMMERCIAL_TYPES}
    INDUSTRIAL_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.INDUSTRIAL_TYPES}
    PUBLIC_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.PUBLIC_TYPES}
    AGRICULTURAL_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.AGRICULTURAL_TYPES}
    INFRASTRUCTURE_F_CLASSES = {str(v).strip().lower() for v in BuildingProcessor.INFRASTRUCTURE_TYPES}
else:
    # Conservative fallback sets when datapipeline module is unavailable.
    RESIDENTIAL_F_CLASSES = {"house", "detached", "apartments", "residential", "terrace", "semidetached_house"}
    COMMERCIAL_F_CLASSES = {"commercial", "retail", "office", "shop", "supermarket", "restaurant", "hotel"}
    INDUSTRIAL_F_CLASSES = {"industrial", "warehouse", "factory", "manufacture", "workshop"}
    PUBLIC_F_CLASSES = {"public", "school", "hospital", "university", "government", "civic"}
    AGRICULTURAL_F_CLASSES = {"farm", "farmhouse", "barn", "greenhouse", "agricultural"}
    INFRASTRUCTURE_F_CLASSES = {"station", "parking", "garage", "utility", "substation", "power"}


# uncomment for automated building import of buildings in regiostar_samples
# from raw_data.import_building_data import OGR_FILE_LIST


class DatabaseConstructor:
    """
    Constructs a ready to use src database. Be careful about overwriting the tables.
    It uses databaseClient to connect to the database and create tables and import data.
    """

    def __init__(self, dbc_obj=None):
        self.extensions_added = False

        if dbc_obj:
            self.dbc = dbc_obj
        else:
            self.dbc = dbc.DatabaseClient()

    def _normalize_state_code(self, state: str) -> str:
        normalized = self.dbc.normalize_state_code(state)
        if not normalized:
            raise ValueError("state must not be empty")
        return normalized

    @staticmethod
    def _coerce_to_multipolygon(geom):
        """Convert polygonal geometries to MultiPolygon and drop non-polygonal types."""
        from shapely.geometry import MultiPolygon, Polygon

        if geom is None or geom.is_empty:
            return None
        if isinstance(geom, Polygon):
            return MultiPolygon([geom])
        if isinstance(geom, MultiPolygon):
            return geom
        if geom.geom_type == "GeometryCollection":
            polygons = []
            for part in geom.geoms:
                mp = DatabaseConstructor._coerce_to_multipolygon(part)
                if mp is not None:
                    polygons.extend(list(mp.geoms))
            return MultiPolygon(polygons) if polygons else None
        return None

    def _merge_duplicate_postcodes(self, df: pd.DataFrame, source_name: str) -> pd.DataFrame:
        """Collapse duplicate postcode rows to one row per (plz, country_code).

        Some country postcode sources contain the same postcode split across
        multiple polygon fragments. The DB schema only allows one row per
        (plz, country_code), so merge those fragments into one MultiPolygon.
        """
        if "plz" not in df.columns or "geom" not in df.columns or "country_code" not in df.columns:
            return df

        dup_mask = df.duplicated(subset=["plz", "country_code"], keep=False)
        if not dup_mask.any():
            return df

        from shapely import wkb, make_valid
        from shapely.ops import unary_union

        def _load_geom(geom_hex):
            if pd.isna(geom_hex):
                return None
            raw_hex = str(geom_hex).strip()
            if not raw_hex:
                return None
            if raw_hex.startswith("\\x"):
                raw_hex = raw_hex[2:]
            geom = wkb.loads(raw_hex, hex=True)
            geom = make_valid(geom)
            return self._coerce_to_multipolygon(geom)

        merged_rows = []
        conflict_count = 0

        for _, group in df.groupby(["plz", "country_code"], sort=False, dropna=False):
            if len(group) == 1:
                merged_rows.append(group.iloc[0].to_dict())
                continue

            ordered = group.sort_values("qkm", ascending=False, na_position="last")
            base = ordered.iloc[0].to_dict()

            if "state_code" in group.columns:
                state_candidates = [
                    str(value).strip()
                    for value in ordered["state_code"].dropna().tolist()
                    if str(value).strip()
                ]
                if state_candidates:
                    state_counts = Counter(state_candidates)
                    if len(state_counts) > 1:
                        conflict_count += 1
                    base["state_code"] = max(
                        state_counts,
                        key=lambda value: (state_counts[value], value == state_candidates[0], value),
                    )

            if "note" in group.columns:
                note_candidates = [
                    str(value)
                    for value in ordered["note"].dropna().tolist()
                    if str(value).strip()
                ]
                if note_candidates:
                    base["note"] = note_candidates[0]

            if "population" in group.columns:
                population_values = pd.to_numeric(group["population"], errors="coerce")
                if population_values.notna().any():
                    base["population"] = int(population_values.fillna(0).sum())

            geoms = []
            for geom_hex in group["geom"].tolist():
                try:
                    geom = _load_geom(geom_hex)
                except Exception:
                    geom = None
                if geom is not None and not geom.is_empty:
                    geoms.append(geom)

            if geoms:
                merged_geom = unary_union(geoms)
                merged_geom = make_valid(merged_geom)
                merged_geom = self._coerce_to_multipolygon(merged_geom)
                if merged_geom is not None and not merged_geom.is_empty:
                    base["geom"] = merged_geom.wkb_hex
                    base["qkm"] = float(merged_geom.area / 1_000_000)

            merged_rows.append(base)

        merged = pd.DataFrame(merged_rows, columns=df.columns)
        print(
            f"Collapsed duplicate postcodes from {source_name}: "
            f"{len(df)} rows -> {len(merged)} unique (plz, country_code) rows"
        )
        if conflict_count:
            print(
                f"Warning: resolved {conflict_count} postcode groups with conflicting state_code values "
                f"by choosing the dominant/largest-area state."
            )
        return merged

    def create_schema(self):
        """
        Creates the target schema if it doesn't exist.
        """
        try:
            with self.dbc.conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TARGET_SCHEMA}")
                self.dbc.conn.commit()
                print(f"Schema '{TARGET_SCHEMA}' created or already exists.")
        except (Exception, psy.DatabaseError) as error:
            print(f"Error creating schema: {error}")
            raise error

    def get_table_name_list(self):
        with self.dbc.conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = %s""", (TARGET_SCHEMA,)
            )
            table_name_list = [tup[0] for tup in cur.fetchall()]

        return table_name_list

    def table_exists(self, table_name):
        if table_name in self.get_table_name_list():
            warnings.warn(f"{table_name} table is overwritten!")
            return True
        else:
            return False

    def batch_delete(self, table_name, where_clause="", params=None, batch_size=10000):
        """
        Delete rows in batches to avoid long-running transactions and reduce WAL pressure.
        Disables foreign key triggers during delete for speed.
        
        Args:
            table_name: Name of the table to delete from
            where_clause: Optional WHERE clause (without 'WHERE' keyword), e.g., "country_code = %s"
            params: Parameters for the WHERE clause
            batch_size: Number of rows to delete per batch (default 10000)
        
        Returns:
            Total number of deleted rows
        """
        total_deleted = 0
        with self.dbc.conn.cursor() as cur:
            # Disable FK triggers for fast delete (replica mode skips trigger execution)
            cur.execute("SET session_replication_role = 'replica';")
            
            try:
                while True:
                    if where_clause:
                        query = f"""
                            DELETE FROM {table_name} 
                            WHERE ctid IN (
                                SELECT ctid FROM {table_name} WHERE {where_clause} LIMIT %s
                            )
                        """
                        cur.execute(query, (*params, batch_size) if params else (batch_size,))
                    else:
                        query = f"""
                            DELETE FROM {table_name} 
                            WHERE ctid IN (
                                SELECT ctid FROM {table_name} LIMIT %s
                            )
                        """
                        cur.execute(query, (batch_size,))
                    
                    deleted = cur.rowcount
                    self.dbc.conn.commit()
                    total_deleted += deleted
                    if deleted < batch_size:
                        break
            finally:
                # Re-enable FK triggers
                if self.dbc.conn.status != psy.extensions.STATUS_IN_TRANSACTION:
                    # If transaction aborted (e.g., column missing), we need rollback first
                    self.dbc.conn.rollback()
                cur.execute("SET session_replication_role = 'origin';")
                self.dbc.conn.commit()
        
        if total_deleted > 0:
            print(f"Deleted {total_deleted} rows from {table_name} in batches")
        return total_deleted

    def create_table(self, table_name):
        # create extension if not exists for recognition of geom datatypes
        if not self.extensions_added:
            with self.dbc.conn.cursor() as cur:
                # create extension if not exists for recognition of geom datatypes
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
                print("CREATE EXTENSION postgis")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgRouting;")
                print("CREATE EXTENSION pgRouting")
                self.dbc.conn.commit()
                self.extensions_added = True

        if table_name == "all":
            try:
                with self.dbc.conn.cursor() as cur:
                    for table_name, query in CREATE_QUERIES.items():
                        cur.execute(query)
                        print(f"CREATE TABLE {table_name}")
                    # Execute seed queries after creating all tables
                    for seed_name, seed_query in SEED_QUERIES.items():
                        cur.execute(seed_query)
                        print(f"SEED DATA {seed_name}")
                    # Ensure equipment_data has newer columns on pre-existing databases
                    cur.execute("ALTER TABLE equipment_data ADD COLUMN IF NOT EXISTS equipment_only_cost_eur integer;")
                    cur.execute("ALTER TABLE equipment_data ADD COLUMN IF NOT EXISTS installed_cost_eur integer;")
                self.dbc.conn.commit()
            except (Exception, psy.DatabaseError) as error:
                raise error
        elif table_name in CREATE_QUERIES:
            try:
                with self.dbc.conn.cursor() as cur:
                    cur.execute(CREATE_QUERIES[table_name])
                    print(f"CREATE TABLE {table_name}")
                self.dbc.conn.commit()
            except (Exception, psy.DatabaseError) as error:
                raise error
        else:
            raise ValueError(
                f"Table name {table_name} is not a valid parameter value for the function create_table. See config.py"
            )

    def ogr_to_db(self, ogr_file_list, skip_failures: bool = False, country_code: str = None, state_code: str = None):
        """
            OGR/GDAL is a translator library for raster and vector geospatial data formats
            inserts building data specified into database
            
            Args:
                ogr_file_list: List of file dicts with 'path' and optional 'table_name'
                skip_failures: If True, skip rows that fail to insert
                country_code: If provided, overwrite country_code column during import
                state_code: If provided, set state_code column during import
        """

        for file_dict in ogr_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists(), file_path
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            # PROFESSIONAL PATTERN: Staging Table
            # If we need to modify data (country/state codes) to avoid PK conflicts, 
            # we must load into a staging table first, transform, then insert to main table.
            use_staging = (country_code is not None or state_code is not None)
            
            target_table = f"{TARGET_SCHEMA}.{table_name}"
            import_table_name = table_name
            staging_full_name = None

            # Ensure compatible schema before import so new building file fields are retained.
            if table_name in {"res", "oth"}:
                with self.dbc.conn.cursor() as cur:
                    cur.execute(f"ALTER TABLE {target_table} ADD COLUMN IF NOT EXISTS f_classes text;")
                    # Enrichment columns (3D BAG, EP-Online)
                    for col, dtype in [
                        ("height_max", "double precision"),
                        ("height_ground", "double precision"),
                        ("height_median", "double precision"),
                        ("floors_3dbag", "integer"),
                        ("bag_id", "varchar"),
                        ("energy_label", "varchar(5)"),
                        ("energy_index", "double precision"),
                        ("cbs_population", "double precision"),
                        ("cbs_households", "double precision"),
                        ("cbs_avg_household_size", "double precision"),
                    ]:
                        cur.execute(f"ALTER TABLE {target_table} ADD COLUMN IF NOT EXISTS {col} {dtype};")
                self.dbc.conn.commit()
            
            try:
                if use_staging:
                    # Create unique staging table name (UUID prevents race conditions)
                    staging_table_name = f"staging_{table_name}_{uuid.uuid4().hex[:8]}"
                    staging_full_name = f"{TARGET_SCHEMA}.{staging_table_name}"
                    import_table_name = staging_table_name # ogr2ogr targets this
                    print(f"Using staging table: {staging_full_name}")
                    
                    # Create staging table with same schema as target, but empty
                    with self.dbc.conn.cursor() as cur:
                        cur.execute(f"DROP TABLE IF EXISTS {staging_full_name}")
                        # We copy schema from target table to ensure compatibility
                        # Use INCLUDING DEFAULTS to get structure + default values
                        # Do NOT use INCLUDING ALL or CONSTRAINTS - we want fast inserts without PK checks here
                        cur.execute(f"CREATE TABLE {staging_full_name} (LIKE {target_table} INCLUDING DEFAULTS)")
                        self.dbc.conn.commit()

                command = [
                        "ogr2ogr",
                        "-append", # Always append to our prepared table (staging or prod)
                        "-progress",
                        "-f",
                        "PostgreSQL",
                        f"PG:dbname={DBNAME} user={DBUSER} password={PASSWORD} host={HOST} port={PORT}",
                        file_path,
                        "-nln",
                        f"{TARGET_SCHEMA}.{import_table_name}", 
                        "-nlt",
                        "PROMOTE_TO_MULTI",
                        "-t_srs",
                        "EPSG:3035",
                        "-lco",
                        "geometry_name=geom",
                        "-lco", f"SCHEMA={TARGET_SCHEMA}", 
                        "--config", "PG_USE_COPY", "YES",
                        "--config", "OGR_TRUNCATE", "NO",
                ]
                
                if skip_failures:
                    command.append("-skipfailures")

                result = subprocess.run(command, check=True, shell=False, stderr=subprocess.PIPE if skip_failures else None)
                
                # Handle Staging -> Production move
                if use_staging:
                    with self.dbc.conn.cursor() as cur:
                        # 1. Transform: Set correct codes in staging
                        print(f"Transforming data in {staging_full_name}...")
                        update_parts = []
                        params = []
                        if country_code:
                            update_parts.append("country_code = %s")
                            params.append(country_code)
                        if state_code:
                            update_parts.append("state_code = %s")
                            params.append(state_code)
                        
                        if update_parts:
                            cur.execute(f"""
                                UPDATE {staging_full_name} 
                                SET {', '.join(update_parts)}
                            """, tuple(params))

                        # Normalize building geometries to MultiPolygon so writes into res/oth are type-safe.
                        if table_name in {"res", "oth"}:
                            cur.execute(f"""
                                UPDATE {staging_full_name} AS s
                                SET geom = g.new_geom
                                FROM (
                                    SELECT
                                        q.ctid,
                                        CASE
                                            WHEN q.extracted IS NULL OR ST_IsEmpty(q.extracted)
                                                THEN NULL::geometry(MultiPolygon, 3035)
                                            WHEN GeometryType(q.extracted) = 'POLYGON'
                                                THEN ST_Multi(q.extracted)::geometry(MultiPolygon, 3035)
                                            WHEN GeometryType(q.extracted) = 'MULTIPOLYGON'
                                                THEN q.extracted::geometry(MultiPolygon, 3035)
                                            ELSE NULL::geometry(MultiPolygon, 3035)
                                        END AS new_geom
                                    FROM (
                                        SELECT ctid, ST_CollectionExtract(ST_MakeValid(geom), 3) AS extracted
                                        FROM {staging_full_name}
                                    ) q
                                ) g
                                WHERE s.ctid = g.ctid;
                            """)
                            cur.execute(f"DELETE FROM {staging_full_name} WHERE geom IS NULL OR ST_IsEmpty(geom);")
                            removed = cur.rowcount
                            if removed > 0:
                                print(f"Dropped {removed} unusable geometries from {staging_full_name}")

                        # The datapipeline leaves floors empty where neither OSM nor an enricher gave a count.
                        if table_name == "res":
                            cur.execute(f"UPDATE {staging_full_name} SET floors = 1 WHERE floors IS NULL;")

                        # 2. Publish: Insert from staging to production
                        print(f"Moving data from {staging_full_name} to {target_table}...")
                        cur.execute(f"""
                            INSERT INTO {target_table} 
                            SELECT * FROM {staging_full_name}
                            ON CONFLICT DO NOTHING
                        """)
                        self.dbc.conn.commit()
                        
                    print(f"Successfully moved data to {table_name} with codes {country_code}/{state_code}")
                    
                # Legacy error reporting (only for direct mode or if subprocess failed)
                if skip_failures and not use_staging:
                    error_list = result.stderr.decode().replace("\r", "").split("\n")
                    error_list = [e[e.find("ERROR: "):e.find("DETAIL: ")] for e in error_list]
                    error_list = [e.strip("\n") for e in error_list if "ERROR: " in e]
                    error_set = set(error_list)
                    
                    if error_set:
                        print(f"Warning: Error(s) occurred while processing {file_name}:")
                        for error in error_set:
                            print("\t" + error)

            finally:
                # Cleanup: Always drop staging table even if errors occurred
                if use_staging and staging_full_name:
                    try:
                        with self.dbc.conn.cursor() as cur:
                            cur.execute(f"DROP TABLE IF EXISTS {staging_full_name}")
                            self.dbc.conn.commit()
                    except Exception as e:
                        print(f"Warning: Failed to cleanup staging table {staging_full_name}: {e}")
            
            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")


    def transformers_to_db(self, clear_existing: bool = True):
        """Call the overpass api for transformer data and populate the transformers table.
        Delete raw_data/transformer_data/processed_trafos/*_trafos_processed.geojson to
        fetch fresh data from OSM.

        If clear_existing=True (default), all existing Transformer datasets are deleted before import
        to avoid duplicate primary keys.
        """
        # clear existing data to avoid duplicate primary keys
        if clear_existing and self.table_exists(table_name="transformers"):
            with self.dbc.conn.cursor() as cur:
                cur.execute("DELETE FROM transformers;")
            self.dbc.conn.commit()

        trafos_processed_geojson_path = get_trafos_processed_geojson_path(RELATION_ID)
        trafos_processed_3035_geojson_path = get_trafos_processed_3035_geojson_path(RELATION_ID)

        update_trafos = not os.path.isfile(trafos_processed_geojson_path)

        if update_trafos:
            print(f"{trafos_processed_geojson_path} does not exist -> fetch transformer data from API and process it")
            fetch_trafos(RELATION_ID)
            process_trafos(RELATION_ID)

        in_file = trafos_processed_geojson_path
        out_file = trafos_processed_3035_geojson_path

        if update_trafos or not os.path.isfile(out_file):
            # Convert the GeoJSON file to EPSG:3035 and write to a new file
            subprocess.run(
                [
                    "ogr2ogr",
                    "-f", "GeoJSON",
                    "-s_srs", f"EPSG:{str(EPSG)}",
                    "-t_srs", "EPSG:3035",
                    out_file,  # output
                    in_file  # input
                ],
                shell=False
            )

        trafo_dict = [
            {
                "path": out_file,
                "table_name": "transformers"
            }
        ]
        self.ogr_to_db(trafo_dict)

    def csv_to_db(self, csv_file_list, country_code: str = "DE", skip_delete: bool = False):

        for file_dict in csv_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists(), file_path
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            if self.table_exists(table_name=table_name) and not skip_delete:
                self.batch_delete(table_name, "country_code = %s", (country_code,))
            # read and write
            df = pd.read_csv(file_path, index_col=False, dtype={"plz": str})
            df = df.rename(columns={"einwohner": "population", "gid": "postcode_id"})
            # Drop postcode_id - let database auto-generate it
            if "postcode_id" in df.columns:
                df = df.drop(columns=["postcode_id"])
            # Add/override country_code column
            df["country_code"] = country_code

            if table_name == "postcode":
                # Keep schema compatible with country-specific postcode enrichments
                # (e.g. Netherlands CBS households metrics).
                with self.dbc.conn.cursor() as cur:
                    cur.execute("ALTER TABLE postcode ADD COLUMN IF NOT EXISTS cbs_households double precision;")
                    cur.execute("ALTER TABLE postcode ADD COLUMN IF NOT EXISTS cbs_avg_household_size double precision;")
                self.dbc.conn.commit()

            if table_name == "postcode" and "state_code" in df.columns:
                # Keep postcode state codes in canonical DB format and guarantee
                # referenced state rows exist before inserting postcode rows.
                def _normalize_state(value):
                    if pd.isna(value):
                        return None
                    return self.dbc.normalize_state_code(value)

                df["state_code"] = df["state_code"].apply(_normalize_state)

            if table_name == "postcode" and "geom" in df.columns:
                df = self._merge_duplicate_postcodes(df, source_name=str(file_path))

            if table_name == "postcode" and "state_code" in df.columns:
                unique_state_codes = sorted(
                    {
                        str(state_code).strip()
                        for state_code in df["state_code"].dropna().tolist()
                        if str(state_code).strip()
                    }
                )
                for state_code in unique_state_codes:
                    self.dbc.ensure_state_entry(
                        country=country_code,
                        state_code=state_code,
                    )

            df.to_sql(
                name=table_name,
                con=self.dbc.sqla_engine,
                if_exists="append",
                index=False,
            )

            if table_name == "postcode" and "geom" in df.columns:
                with self.dbc.conn.cursor() as cur:
                    cur.execute("""
                        UPDATE postcode
                        SET geom = ST_SetSRID(geom, 3035)
                        WHERE ST_SRID(geom) = 0 AND country_code = %s
                    """, (country_code,))
                    self.dbc.conn.commit()

            # Update planner statistics after bulk data load
            with self.dbc.conn.cursor() as cur:
                cur.execute(psy_sql.SQL("ANALYZE {};").format(psy_sql.Identifier(table_name)))
                self.dbc.conn.commit()

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")


    def load_postcode_from_infdb(self):
        """
        Load postcode data from InfDB and insert into local 'postcode' table in pylovo.
        """
        st = time.time()

        # Create InfdbClient instance to connect to remote InfDB
        infdb_client = InfdbClient()

        # Fetch postcode data from InfDB
        rows = infdb_client.fetch_postcode_from_infb()

        if not rows:
            raise ValueError("No postcode data retrieved from InfDB")

        # Optional: Clear existing data from postcode table (batch delete to avoid long locks)
        if self.table_exists(table_name="postcode"):
            self.batch_delete("pylovo.postcode")

        # Insert rows into pylovo postcode table using executemany
        insert_query = """
            INSERT INTO pylovo.postcode (plz, note, qkm, population, geom)
            VALUES (%s, %s, %s, %s, ST_Transform(%s::geometry, 3035))
        """
        with self.dbc.conn.cursor() as cur:
            cur.executemany(insert_query, rows)
            self.dbc.conn.commit()

        et = time.time()
        print(f"Postcode data imported from InfDB in {int(et - st)} s")


    def create_public_2po_table(self):
        """
        Reads the large SQL file in 10% chunks, executes complete statements on-the-fly,
        and defers incomplete statements until the next chunk.
        """
        cur = self.dbc.conn.cursor()

        # Path to your SQL file, which includes creation of the table
        sc_path = os.path.join(os.getcwd(), "raw_data", "ways", "ways_public_2po_4pgr.sql")
        file_size = os.path.getsize(sc_path)

        # We read 10% at a time.  (Or pick a chunk size in bytes that works for your environment.)
        chunk_size = max(1, file_size // 100)
        chars_read = 0

        leftover = ""  # Holds any partial statement that didn't end with a semicolon

        print("\nStart inserting ways into public_2po_4pgr table.")
        with open(sc_path, 'r', encoding='utf-8') as sc_file:
            while True:
                # Read next chunk
                data = sc_file.read(chunk_size)
                if not data:
                    # No more data to read
                    break

                chars_read += len(data)
                progress = round(chars_read * 100 / file_size)
                print(f"\rProgress: {progress}%", end="", flush=True)

                # Combine leftover from previous read with current chunk
                combined = leftover + data

                # Use sqlparse to split out complete statements
                statements = sqlparse.split(combined)

                # If sqlparse.split() returns multiple statements, the last one
                # might be incomplete. We’ll keep it as leftover if needed.
                if len(statements) > 1:
                    # Execute all statements except possibly the last
                    for stmt in statements[:-1]:
                        stmt = stmt.strip()
                        if stmt:
                            cur.execute(stmt)
                            self.dbc.conn.commit()

                    # Check if the last statement ends with a semicolon or not
                    last_stmt = statements[-1].strip()
                    if last_stmt.endswith(';'):
                        # It's a complete statement
                        cur.execute(last_stmt)
                        self.dbc.conn.commit()
                        leftover = ""
                    else:
                        leftover = last_stmt
                else:
                    # 0 or 1 statements from sqlparse
                    if len(statements) == 1:
                        # Could be complete or incomplete
                        stmt = statements[0].strip()
                        if stmt.endswith(';'):
                            # It's complete, execute it
                            cur.execute(stmt)
                            self.dbc.conn.commit()
                            leftover = ""
                        else:
                            # It's incomplete, keep it
                            leftover = stmt
                    else:
                        # No statements found. This can happen if combined was empty or whitespace.
                        # Just continue reading next chunk
                        pass
        print("\nInserted all ways into public_2po_4pgr table.")

    def ways_to_db(self, country_code: str = None, state_code: str = None):
        """This function transform the output of osm2po to the ways table, refer to the issue
        https://github.com/TongYe1997/Connector-syn-grid/issues/19"""

        st = time.time()

        cur = self.dbc.conn.cursor()

        # Get current max way_id to avoid duplicates when loading multiple countries
        cur.execute("SELECT COALESCE(MAX(way_id), 0) FROM ways")
        max_way_id = cur.fetchone()[0]

        # Transform to ways table (use ST_LineMerge to convert MultiLineString to LineString)
        # Offset way_id by current max to ensure uniqueness across countries
        # Set country_code and state_code during insert to avoid conflicts
        cc = f"'{country_code}'" if country_code else "'DE'"
        sc = f"'{state_code}'" if state_code else "NULL"
        query = f"""INSERT INTO ways (clazz, source, target, cost, reverse_cost, geom, way_id, country_code, state_code)
            SELECT  clazz,
                    source,
                    target,
                    cost,
                    reverse_cost,
                    ST_LineMerge(ST_Transform(geom_way, 3035)) as geom,
                    id + {max_way_id} AS way_id,
                    {cc} AS country_code,
                    {sc} AS state_code
            FROM public_2po_4pgr"""
        cur.execute(query)

        # Drop public_2po_4pgr table, as it is not needed anymore
        query = "DROP TABLE public_2po_4pgr"
        cur.execute(query)

        self.dbc.conn.commit()

        et = time.time()
        print(f"Ways are successfully imported to db in {int(et - st)} s")


    def load_ways_preprocessing_functions(self):
        """
        Loads and executes SQL function definitions into the database.

        The SQL files are grouped under two categories:
        1. Utility functions (e.g., spatial helpers, geometry splitting)
        2. Core functions (e.g., building-to-way connection logic, intersection segmentation)

        SQL files are loaded from:
            - src/ways_preprocessing/utils/
            - src/ways_preprocessing/core/
        """
        cur = self.dbc.conn.cursor()

        # Print once at the beginning
        print(f"Loading ways preprocessing functions into schema '{TARGET_SCHEMA}'.")

        function_paths = [
            os.path.join("src", "ways_preprocessing_functions", "utils"),
            os.path.join("src", "ways_preprocessing_functions", "core")
        ]

        try:
            for path in function_paths:
                abs_path = os.path.join(os.getcwd(), path)

                for filename in sorted(os.listdir(abs_path)):
                    if filename.endswith(".sql"):
                        full_file_path = os.path.join(abs_path, filename)
                        with open(full_file_path, 'r') as f:
                            sql = f.read()
                            cur.execute(sql)

            self.dbc.conn.commit()

        except Exception as e:
            print(f"[ERROR] Failed while executing SQL function from file '{filename}': {e}")
            self.dbc.conn.rollback()
            raise

    def drop_all_tables(self):
        """
        Drops all tables in the database
        """
        cur = self.dbc.conn.cursor()
        cur.execute(f"DROP SCHEMA {TARGET_SCHEMA} CASCADE")
        self.dbc.conn.commit()

    def dump_functions(self):
        """
        Load SQL functions from postgres_dump_functions.sql file.
        """
        sql_file = Path("src/postgres_dump_functions.sql")
        if not sql_file.exists():
            print(f"Warning: SQL functions file not found: {sql_file}")
            return
            
        print(f"Loading SQL functions from: {sql_file}")
        with self.dbc.conn.cursor() as cur:
            with open(sql_file, 'r') as f:
                sql_content = f.read()
                # Split by semicolon and execute each statement
                statements = sqlparse.split(sql_content)
                for stmt in statements:
                    stmt = stmt.strip()
                    if stmt:
                        try:
                            cur.execute(stmt)
                        except Exception as e:
                            print(f"Warning: Failed to execute statement: {e}")
            self.dbc.conn.commit()
        print("SQL functions loaded successfully")

    def _get_state_f_class_distribution(self, country_code: str, state_code: str) -> list[tuple[str, int]]:
        """Return [(f_class, count)] for all buildings of a state across res + oth tables."""
        query = """
            SELECT f_class_norm AS f_class, COUNT(*)::bigint AS cnt
            FROM (
                SELECT COALESCE(NULLIF(LOWER(TRIM(f_class)), ''), 'yes') AS f_class_norm
                FROM res
                WHERE country_code = %s AND state_code = %s
                UNION ALL
                SELECT COALESCE(NULLIF(LOWER(TRIM(f_class)), ''), 'yes') AS f_class_norm
                FROM oth
                WHERE country_code = %s AND state_code = %s
            ) s
            GROUP BY f_class_norm
            ORDER BY cnt DESC, f_class_norm
        """
        with self.dbc.conn.cursor() as cur:
            cur.execute(query, (country_code, state_code, country_code, state_code))
            return [(row[0], int(row[1])) for row in cur.fetchall()]

    def _get_known_consumer_definitions(self) -> set[str]:
        """Get current set of consumer_categories definitions."""
        with self.dbc.conn.cursor() as cur:
            cur.execute("SELECT definition FROM consumer_categories")
            return {self._normalize_f_class(row[0]) for row in cur.fetchall() if row[0]}

    @staticmethod
    def _normalize_f_class(value: str) -> str:
        """Normalize f_class for stable lookups/insertions."""
        if value is None:
            return "yes"
        norm = str(value).strip().lower()
        if not norm:
            return "yes"
        norm = re.sub(r"[\s\-/]+", "_", norm)
        norm = re.sub(r"[^a-z0-9_]", "", norm)
        norm = re.sub(r"_+", "_", norm).strip("_")
        if not norm or norm in {"none", "null", "nan", "unknown", "n_a", "na"}:
            return "yes"
        return norm

    @staticmethod
    def _infer_parent_category_for_f_class(f_class: str) -> str:
        """Infer parent_category for unknown f_class rows."""
        fc = DatabaseConstructor._normalize_f_class(f_class)

        # Exact high-confidence overrides for classes frequently seen in OSM/POI data.
        exact_map = {
            "apartment": "residential",
            "apartments": "residential",
            "townhouse": "residential",
            "town_house": "residential",
            "villa": "residential",
            "waschkueche": "residential",
            "community_center": "public",
            "community_centre": "public",
            "concert_hall": "public",
            "opera_house": "public",
            "presbytery": "public",
            "rectory": "public",
            "kingdom_hall": "public",
            "aviary": "public",
            "gym": "public",
            "arena": "public",
            "hall": "public",
            "gas_station": "commercial",
            "fuel": "commercial",
            "property_management": "commercial",
            "sewage_pumping_station": "industrial",
            "gasometer": "industrial",
            "container": "industrial",
            "containers": "industrial",
            "shipping_container": "industrial",
            "viaduct": "infrastructure",
            "bridge_support": "infrastructure",
            "stairs": "infrastructure",
            "staircase": "infrastructure",
            "steps": "infrastructure",
            "elevator": "infrastructure",
            "gate": "infrastructure",
            "city_gate": "infrastructure",
            "archway": "infrastructure",
            "canopy": "infrastructure",
            "ramp": "infrastructure",
            "watchtower": "infrastructure",
            "airport": "infrastructure",
            "boat": "infrastructure",
            "demolished": "infrastructure",
            "planned": "infrastructure",
            "proposed": "infrastructure",
            "greenhouse_horticulture": "agricultural",
        }
        if fc in exact_map:
            return exact_map[fc]

        if fc in RESIDENTIAL_F_CLASSES or fc in {"yes", "building", "unclassified", "other"}:
            return "residential"
        if fc in COMMERCIAL_F_CLASSES:
            return "commercial"
        if fc in INDUSTRIAL_F_CLASSES:
            return "industrial"
        if fc in PUBLIC_F_CLASSES:
            return "public"
        if fc in AGRICULTURAL_F_CLASSES:
            return "agricultural"
        if fc in INFRASTRUCTURE_F_CLASSES:
            return "infrastructure"

        # Keyword fallback for unseen classes
        keyword_map = (
            ("residential", ("house", "apartment", "residential", "dormitory", "terrace", "villa", "townhouse")),
            ("public", (
                "school", "hospital", "university", "kindergarten", "church", "government", "clinic",
                "museum", "theatre", "theater", "library", "community", "civic", "culture", "castle",
                "presbyter", "rectory", "hall", "opera", "concert", "arena", "gym"
            )),
            ("industrial", (
                "industrial", "factory", "warehouse", "workshop", "manufactur", "silo", "mill",
                "pumping", "sewage", "container", "gasometer", "plant"
            )),
            ("agricultural", ("farm", "barn", "greenhouse", "agricultural", "livestock", "stable", "cowshed", "granary")),
            ("commercial", (
                "shop", "retail", "office", "supermarket", "restaurant", "hotel", "hairdresser",
                "doctor", "dentist", "pharmacy", "bank", "store", "market", "fuel", "car_wash", "service"
            )),
            ("infrastructure", (
                "station", "terminal", "parking", "garage", "substation", "utility", "power", "bridge",
                "viaduct", "stair", "elevator", "gate", "canopy", "ramp", "tower", "airport", "port", "harbor", "harbour"
            )),
        )
        for parent, keywords in keyword_map:
            if any(k in fc for k in keywords):
                return parent

        # Safe default for unknown non-residential classes
        return "commercial"

    def _get_parent_category_templates(self, cur) -> dict[str, tuple]:
        """
        Return one template row per parent_category from consumer_categories.

        Template tuple format:
        (load_method, peak_load, yearly_consumption, peak_load_per_m2, yearly_consumption_per_m2, sim_factor)
        """
        template_defs = {
            "residential": "residential",
            "commercial": "commercial",
            "public": "public",
            "industrial": "industrial",
            "agricultural": "agricultural",
            "infrastructure": "infrastructure",
        }
        templates: dict[str, tuple] = {}

        for parent, preferred_def in template_defs.items():
            cur.execute(
                """
                SELECT load_method, peak_load, yearly_consumption,
                       peak_load_per_m2, yearly_consumption_per_m2, sim_factor
                FROM consumer_categories
                WHERE LOWER(parent_category) = %s
                ORDER BY CASE WHEN LOWER(definition) = %s THEN 0 ELSE 1 END, consumer_category_id
                LIMIT 1
                """,
                (parent, preferred_def),
            )
            row = cur.fetchone()
            if row:
                templates[parent] = row

        return templates

    def _auto_register_missing_f_classes(self, missing_f_classes: list[str]) -> int:
        """Insert missing f_classes into consumer_categories using inferred parent-category templates."""
        if not missing_f_classes:
            return 0

        normalized_missing = sorted({self._normalize_f_class(fc) for fc in missing_f_classes})
        inserted = 0
        inserted_by_parent: dict[str, int] = {}
        with self.dbc.conn.cursor() as cur:
            templates = self._get_parent_category_templates(cur)

            cur.execute("""
                SELECT load_method, peak_load, yearly_consumption,
                       peak_load_per_m2, yearly_consumption_per_m2, sim_factor
                FROM consumer_categories
                WHERE definition = '_default'
                LIMIT 1
            """)
            default_row = cur.fetchone()

            if not default_row:
                # Fallback if _default row is missing
                default_row = (
                    "area",
                    None,
                    None,
                    29.0,
                    155.7,
                    float(SIM_FACTOR.get("commercial", 0.5)),
                )

            cur.execute("SELECT COALESCE(MAX(consumer_category_id), 0) FROM consumer_categories")
            next_id = int(cur.fetchone()[0]) + 1

            for f_class in normalized_missing:
                parent = self._infer_parent_category_for_f_class(f_class)
                template_row = templates.get(parent, default_row)
                (template_load_method, template_peak_load, template_yearly_consumption,
                 template_peak_load_per_m2, template_yearly_consumption_per_m2, template_sim_factor) = template_row

                cur.execute("""
                    INSERT INTO consumer_categories (
                        consumer_category_id, definition, load_method, parent_category,
                        peak_load, yearly_consumption, peak_load_per_m2, yearly_consumption_per_m2, sim_factor
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    next_id, f_class, template_load_method, parent,
                    template_peak_load, template_yearly_consumption,
                    template_peak_load_per_m2, template_yearly_consumption_per_m2, template_sim_factor
                ))
                if cur.rowcount > 0:
                    inserted += 1
                    inserted_by_parent[parent] = inserted_by_parent.get(parent, 0) + 1
                next_id += 1

        self.dbc.conn.commit()
        if inserted > 0:
            print(
                f"Auto-registered {inserted} missing f_class definitions into consumer_categories "
                f"(by parent={inserted_by_parent})"
            )
        return inserted

    def audit_f_class_coverage(self, country_code: str, state_code: str) -> None:
        """Audit f_class coverage against consumer_categories and optionally auto-register missing classes."""
        # Ensure configured baseline categories are present before auditing datapipeline imports
        self.dbc.insert_consumer_categories_from_config(consumer_categories=CONSUMER_CATEGORIES)

        raw_dist = self._get_state_f_class_distribution(country_code=country_code, state_code=state_code)
        # Normalize keys so variants like "community center" and "community_center"
        # are audited as the same f_class.
        dist_counter: dict[str, int] = {}
        for f_class, cnt in raw_dist:
            norm = self._normalize_f_class(f_class)
            dist_counter[norm] = dist_counter.get(norm, 0) + int(cnt)
        dist = sorted(dist_counter.items(), key=lambda x: (-x[1], x[0]))
        total_buildings = sum(cnt for _, cnt in dist)
        if total_buildings == 0:
            print(f"f_class audit: no buildings found for {country_code}/{state_code}, skipping.")
            return

        known_defs = self._get_known_consumer_definitions()
        missing_rows = [(f_class, cnt) for f_class, cnt in dist if f_class not in known_defs]
        missing_buildings = sum(cnt for _, cnt in missing_rows)

        print(
            f"f_class audit before auto-register: total={total_buildings}, "
            f"covered={total_buildings - missing_buildings}, missing={missing_buildings}"
        )
        if missing_rows:
            top_n = max(1, int(F_CLASS_AUDIT_TOP_N))
            preview = ", ".join([f"{f_class}:{cnt}" for f_class, cnt in missing_rows[:top_n]])
            print(f"Top uncovered f_class values: {preview}")

        if missing_rows and AUTO_REGISTER_MISSING_F_CLASSES:
            self._auto_register_missing_f_classes([f_class for f_class, _ in missing_rows])
            known_defs = self._get_known_consumer_definitions()
            missing_rows = [(f_class, cnt) for f_class, cnt in dist if f_class not in known_defs]
            missing_buildings = sum(cnt for _, cnt in missing_rows)
            print(
                f"f_class audit after auto-register: total={total_buildings}, "
                f"covered={total_buildings - missing_buildings}, missing={missing_buildings}"
            )

        missing_ratio = (missing_buildings / total_buildings) if total_buildings else 0.0
        warn_threshold = float(F_CLASS_AUDIT_WARN_THRESHOLD)
        fail_threshold = max(float(F_CLASS_AUDIT_FAIL_THRESHOLD), warn_threshold)

        if missing_ratio > fail_threshold:
            raise RuntimeError(
                f"f_class coverage check failed for {country_code}/{state_code}: "
                f"missing_ratio={missing_ratio:.4%} > fail_threshold={fail_threshold:.4%}"
            )
        if missing_ratio > warn_threshold:
            print(
                f"[WARNING] f_class coverage warning for {country_code}/{state_code}: "
                f"missing_ratio={missing_ratio:.4%} > warn_threshold={warn_threshold:.4%}"
            )
        else:
            print(
                f"f_class coverage OK for {country_code}/{state_code}: "
                f"missing_ratio={missing_ratio:.4%}"
            )

    def buildings_from_datapipeline(self, country: str, state: str):
        """
        Load building data from datapipeline output to res and oth tables.
        
        Args:
            country: Country name (e.g., 'germany')
            state: State name (e.g., 'hamburg')
        """
        # Get country code from centralized config
        country_code = get_country_code(country)
        
        buildings_dir = Path(f"raw_data/{country}/{state}/buildings")
        
        def _pick_building_file(prefix: str):
            """Pick best available building file for import.

            Preference order:
            1. Final enriched file (most suffixes like _enriched_energy_cbs, excluding *_processed)
            2. Processed file (_processed)
            3. Base file (Res_None.gpkg / Oth_None.gpkg)
            """
            # Enriched files: pick newest non-processed enrichment output.
            # Keep *_processed as lower-priority fallback because they may be stale
            # if enrichment was rerun without reprocessing.
            enriched = sorted(
                (f for f in buildings_dir.glob(f"{prefix}_*.gpkg")
                 if "_processed" not in f.stem
                 and any(s in f.stem for s in ("_enriched", "_energy", "_cbs"))),
                key=lambda f: len(f.stem),  # longest name = most enriched
                reverse=True,
            )
            if enriched:
                return enriched[0]
            # Processed files
            processed = sorted(buildings_dir.glob(f"{prefix}_*_processed.gpkg"))
            if processed:
                return processed[0]
            # Base files
            base = [f for f in sorted(buildings_dir.glob(f"{prefix}_*.gpkg"))
                    if "_processed" not in f.stem]
            if base:
                return base[0]
            return None

        res_file = _pick_building_file("Res")
        oth_file = _pick_building_file("Oth")
        
        ogr_file_list = []
        
        if res_file and res_file.exists():
            ogr_file_list.append({
                "path": str(res_file),
                "table_name": "res"
            })
            print(f"Found residential buildings: {res_file}")
        else:
            print(f"Warning: No residential buildings file found in {buildings_dir}")
            
        if oth_file and oth_file.exists():
            ogr_file_list.append({
                "path": str(oth_file),
                "table_name": "oth"
            })
            print(f"Found other buildings: {oth_file}")
        else:
            print(f"Warning: No other buildings file found in {buildings_dir}")
            
        if ogr_file_list:
            # Ensure state_code column exists in tables before delete/import.
            with self.dbc.conn.cursor() as cur:
                for table in ["res", "oth", "transformers", "ways", "postcode"]:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS state_code varchar(50);")
                self.dbc.conn.commit()

            # Only clear existing state data once replacement files are confirmed.
            state_lower = self._normalize_state_code(state)
            self.batch_delete("res", "country_code = %s AND state_code = %s", (country_code, state_lower))
            self.batch_delete("oth", "country_code = %s AND state_code = %s", (country_code, state_lower))
            print(f"Cleared existing building data for {country_code}/{state_lower} from res and oth tables")

            # Pass country_code and state_code to avoid PK conflicts with existing data
            self.ogr_to_db(ogr_file_list, country_code=country_code, state_code=state_lower)
            print(f"Imported buildings with country_code={country_code} and state_code={state_lower}")

            # Fix any invalid geometries so spatial index joins work without ST_MakeValid wrappers.
            with self.dbc.conn.cursor() as cur:
                for table in ["res", "oth"]:
                    cur.execute(f"""
                        UPDATE {table} AS t
                        SET geom = g.new_geom
                        FROM (
                            SELECT
                                q.ctid,
                                CASE
                                    WHEN q.extracted IS NULL OR ST_IsEmpty(q.extracted)
                                        THEN NULL::geometry(MultiPolygon, 3035)
                                    WHEN GeometryType(q.extracted) = 'POLYGON'
                                        THEN ST_Multi(q.extracted)::geometry(MultiPolygon, 3035)
                                    WHEN GeometryType(q.extracted) = 'MULTIPOLYGON'
                                        THEN q.extracted::geometry(MultiPolygon, 3035)
                                    ELSE NULL::geometry(MultiPolygon, 3035)
                                END AS new_geom
                            FROM (
                                SELECT ctid, ST_CollectionExtract(ST_MakeValid(geom), 3) AS extracted
                                FROM {table}
                                WHERE country_code = %s AND state_code = %s
                                  AND NOT ST_IsValid(geom)
                            ) q
                        ) g
                        WHERE t.ctid = g.ctid
                          AND g.new_geom IS NOT NULL;
                    """, (country_code, state_lower))
                    fixed = cur.rowcount
                    if fixed > 0:
                        print(f"Fixed {fixed} invalid geometries in {table}")

                    cur.execute(f"""
                        DELETE FROM {table}
                        WHERE country_code = %s AND state_code = %s
                          AND (geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom));
                    """, (country_code, state_lower))
                    dropped = cur.rowcount
                    if dropped > 0:
                        print(f"Dropped {dropped} non-polygon/invalid geometries in {table}")
                self.dbc.conn.commit()

            # Update planner statistics after bulk data load
            with self.dbc.conn.cursor() as cur:
                cur.execute("ANALYZE res;")
                cur.execute("ANALYZE oth;")
                self.dbc.conn.commit()
            print("Updated planner statistics for res and oth tables")

            self.audit_f_class_coverage(country_code=country_code, state_code=state_lower)
        else:
            raise FileNotFoundError(f"No building files found in {buildings_dir}")

    def transformers_from_datapipeline(self, country: str, state: str):
        """
        Load transformer data from datapipeline output.
        
        Args:
            country: Country name (e.g., 'germany')
            state: State name (e.g., 'hamburg')
        """
        # Get country code from centralized config
        country_code = get_country_code(country)
        
        transformers_dir = Path(f"raw_data/{country}/{state}/transformers")
        
        # Find the processed transformer file (look for various naming patterns)
        patterns = [
            "*_trafos_processed_3035_points_processed.geojson",
            "*_trafos_processed_3035_points.geojson", 
            "*_trafos_processed_3035.geojson"
        ]
        
        trafo_file = None
        for pattern in patterns:
            files = list(transformers_dir.glob(pattern))
            if files:
                trafo_file = files[0]
                break
                
        if not trafo_file:
            raise FileNotFoundError(f"No transformer file found in {transformers_dir}")
            
        print(f"Loading transformers from: {trafo_file}")
        
        # Clear existing transformers for this state only (batch delete)
        state_lower = self._normalize_state_code(state)
        self.batch_delete("transformers", "country_code = %s AND state_code = %s", (country_code, state_lower))
            
        # Import transformers using ogr2ogr with correct country_code and state_code
        ogr_file_list = [{
            "path": str(trafo_file),
            "table_name": "transformers"
        }]
        self.ogr_to_db(ogr_file_list, country_code=country_code, state_code=state_lower)
        print(f"Imported transformers with country_code={country_code} and state_code={state_lower}")

        # Update planner statistics after bulk data load
        with self.dbc.conn.cursor() as cur:
            cur.execute("ANALYZE transformers;")
            self.dbc.conn.commit()
        print("Updated planner statistics for transformers table")

    def ways_from_datapipeline(self, country: str, state: str):
        """
        Load ways/street network data from datapipeline output.
        
        Args:
            country: Country name (e.g., 'germany')
            state: State name (e.g., 'hamburg')
        """
        # Get country code from centralized config
        country_code = get_country_code(country)
        
        ways_dir = Path(f"raw_data/{country}/{state}/ways")
        sql_file = ways_dir / "ways_public_2po_4pgr.sql"
        
        if not sql_file.exists():
            raise FileNotFoundError(f"Ways SQL file not found: {sql_file}")
            
        print(f"Loading ways from: {sql_file}")
        
        # Clear existing ways data for this state only (batch delete)
        state_lower = self._normalize_state_code(state)
        self.batch_delete("ways", "country_code = %s AND state_code = %s", (country_code, state_lower))
        with self.dbc.conn.cursor() as cur:
            # Drop public_2po_4pgr if it exists from previous run
            cur.execute("DROP TABLE IF EXISTS public_2po_4pgr")
            self.dbc.conn.commit()
        print(f"Cleared existing ways data for {country_code}/{state_lower}")
        
        # Execute the SQL file to create and populate the ways table
        with self.dbc.conn.cursor() as cur:
            with open(sql_file, 'r') as f:
                sql_content = f.read()
                cur.execute(sql_content)
            self.dbc.conn.commit()
            
        print("Ways data loaded successfully")
        
        # Transform into our ways table format with correct country_code and state_code
        self.ways_to_db(country_code=country_code, state_code=state_lower)
        print(f"Imported ways with country_code={country_code} and state_code={state_lower}")

        # Update planner statistics after bulk data load
        with self.dbc.conn.cursor() as cur:
            cur.execute("ANALYZE ways;")
            self.dbc.conn.commit()
        print("Updated planner statistics for ways table")

    def assign_state_code_to_postcodes(self, country: str, state: str):
        """
        Assign state_code to postcodes based on spatial intersection with boundary.
        
        For postcodes without state_code, determines the state by checking which
        postcodes contain buildings from the loaded state data.
        
        Args:
            country: Country name (e.g., 'netherlands')
            state: State name (e.g., 'flevoland')
        """
        country_code = get_country_code(country)
        state_lower = self._normalize_state_code(state)
        self.dbc.ensure_state_entry(country=country_code, state_code=state_lower, state_name=state)
        
        print(f"Assigning state_code '{state_lower}' to postcodes with buildings...")

        # Bulk loads can leave planner statistics stale; refresh before the
        # spatial postcode/state assignment on large states like Berlin.
        with self.dbc.conn.cursor() as cur:
            cur.execute("ANALYZE postcode;")
            cur.execute("ANALYZE res;")
            cur.execute("ANALYZE oth;")
            self.dbc.conn.commit()

        def _print_progress(prefix: str, done: int, total: int, updated: int) -> None:
            total = max(total, 1)
            width = 28
            filled = min(width, int(width * done / total))
            pct = min(100, int(100 * done / total))
            bar = "#" * filled + "-" * (width - filled)
            print(
                f"\r{prefix} [{bar}] {done}/{total} ({pct}%) updated={updated}",
                end="",
                flush=True,
            )
            if done >= total:
                print()

        def _get_candidate_postcodes(source_table: str) -> list[str]:
            if source_table not in {"res", "oth"}:
                raise ValueError(f"Unsupported source table: {source_table}")

            with self.dbc.conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT p.plz
                    FROM postcode p
                    JOIN {source_table} s
                      ON s.country_code = %s
                     AND s.state_code = %s
                     AND s.geom && p.geom
                     AND ST_Intersects(ST_PointOnSurface(s.geom), p.geom)
                    WHERE p.country_code = %s
                      AND COALESCE(BTRIM(p.state_code), '') = ''
                    ORDER BY p.plz
                    """,
                    (country_code, state_lower, country_code),
                )
                return [row[0] for row in cur.fetchall()]

        def _assign_from_source(source_table: str, label: str, batch_size: int = 25) -> int:
            if source_table not in {"res", "oth"}:
                raise ValueError(f"Unsupported source table: {source_table}")

            candidate_plzs = _get_candidate_postcodes(source_table)
            total_candidates = len(candidate_plzs)
            print(f"  -> Updating from {label} for {country_code}/{state_lower} ({total_candidates} candidate postcodes)...")
            if total_candidates == 0:
                print(f"  -> No postcode corrections are needed before {label} update.")
                return 0

            total_updated = 0
            processed = 0
            query = """
                UPDATE postcode p
                SET state_code = %s
                WHERE p.country_code = %s
                  AND p.plz = ANY(%s)
                  AND COALESCE(BTRIM(p.state_code), '') = ''
            """

            for start in range(0, total_candidates, batch_size):
                batch_plzs = candidate_plzs[start:start + batch_size]
                with self.dbc.conn.cursor() as cur:
                    cur.execute(
                        query,
                        (state_lower, country_code, batch_plzs),
                    )
                    batch_updated = cur.rowcount
                    self.dbc.conn.commit()
                total_updated += batch_updated
                processed = min(start + len(batch_plzs), total_candidates)
                _print_progress(f"     {label}", processed, total_candidates, total_updated)

            return total_updated

        updated_count = _assign_from_source("res", "res")
        print(f"Assigned state_code '{state_lower}' to {updated_count} postcodes")

        updated_count = _assign_from_source("oth", "oth")
        if updated_count > 0:
            print(f"Assigned state_code '{state_lower}' to {updated_count} additional postcodes from oth buildings")

        synced_pr = self.dbc.sync_postcode_result_state_codes(country=country_code)
        if synced_pr > 0:
            print(f"Synchronized state_code for {synced_pr} postcode_result rows in {country_code}")
