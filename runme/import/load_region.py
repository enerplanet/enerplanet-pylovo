"""Add one region's pylovo data to a database that already holds other regions.

The input is a file written by EnerPlanET's fixtures/export_pylovo.py (.sql or
.sql.gz). Its rows go into temporary staging tables first. There they are
checked, their generated ids are replaced by ids drawn from the target, and
everything is inserted in one transaction, so a failure leaves the target
unchanged. Postcodes the target already holds are refused, not merged: grids
of a loaded region are referenced by id from saved models.

Run from the project root, with the usual DBNAME, DBUSER, PASSWORD, HOST and
PORT variables pointing at the target:

    python -m runme.import.load_region region.sql.gz --dry-run
    python -m runme.import.load_region region.sql.gz
"""
import argparse
import gzip
import re
from pathlib import Path

import psycopg2

from config.config_table_structure import REFRESH_QUERIES

# Tables taken from the package, in foreign-key order. version is required to
# exist already and is never loaded.
STAGED_TABLES = (
    "country", "state", "equipment_data", "consumer_categories", "postcode", "res", "oth", "ways",
    "transformers", "postcode_result", "grid_result", "buildings_result", "lines_result",
    "transformer_positions",
)

# Shared reference rows and building inputs may already be present from a
# neighbouring region; an existing row wins. Everything else must be new.
INSERT_SQL = {
    "country": "INSERT INTO public.country SELECT * FROM pg_temp.country ON CONFLICT DO NOTHING",
    "state": "INSERT INTO public.state SELECT * FROM pg_temp.state ON CONFLICT DO NOTHING",
    "equipment_data": "INSERT INTO public.equipment_data SELECT * FROM pg_temp.equipment_data ON CONFLICT DO NOTHING",
    "consumer_categories": "INSERT INTO public.consumer_categories SELECT * FROM pg_temp.consumer_categories "
                           "WHERE NOT EXISTS (SELECT 1 FROM public.consumer_categories c "
                           "WHERE c.definition = pg_temp.consumer_categories.definition)",
    "postcode": "INSERT INTO public.postcode SELECT * FROM pg_temp.postcode",
    "res": "INSERT INTO public.res SELECT * FROM pg_temp.res ON CONFLICT DO NOTHING",
    "oth": "INSERT INTO public.oth SELECT * FROM pg_temp.oth ON CONFLICT DO NOTHING",
    "ways": "INSERT INTO public.ways SELECT * FROM pg_temp.ways",
    "transformers": "INSERT INTO public.transformers SELECT * FROM pg_temp.transformers ON CONFLICT DO NOTHING",
    "postcode_result": "INSERT INTO public.postcode_result SELECT * FROM pg_temp.postcode_result",
    "grid_result": "INSERT INTO public.grid_result SELECT * FROM pg_temp.grid_result",
    "buildings_result": "INSERT INTO public.buildings_result SELECT * FROM pg_temp.buildings_result",
    "lines_result": "INSERT INTO public.lines_result SELECT * FROM pg_temp.lines_result",
    "transformer_positions": "INSERT INTO public.transformer_positions SELECT * FROM pg_temp.transformer_positions",
}

SECTION = re.compile(r"^-- (\w+): (\d+) rows$")
SECTIONS_END = "-- CREATE MATERIALIZED VIEW populates"
INSERT_HEAD = re.compile(r"^INSERT INTO (\w+) \(")


def read_sections(path: Path) -> dict:
    """Return {table: (declared row count, INSERT statements as one string)} from an export file."""
    opener = gzip.open if path.suffix == ".gz" else open
    sections, current, lines = {}, None, []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            match = SECTION.match(line)
            if match or line.startswith(SECTIONS_END):
                if current:
                    sections[current[0]] = (current[1], "\n".join(lines))
                current, lines = ((match.group(1), int(match.group(2))) if match else None), []
                continue
            if current and line:
                lines.append(_qualify(line, current[0]))
    if not sections:
        raise ValueError(f"{path}: no '-- <table>: <n> rows' sections; not an export_pylovo.py file")
    return sections


def _qualify(line: str, table: str) -> str:
    """Point an INSERT at the staging table so no row reaches the target before the checks."""
    match = INSERT_HEAD.match(line)
    if not match:
        return line
    if match.group(1) != table:
        raise ValueError(f"INSERT INTO {match.group(1)} found in the {table} section")
    return f"INSERT INTO pg_temp.{table} (" + line[match.end():]


def stage(cur, sections: dict) -> dict:
    """Load the package into temporary tables shaped like the target's; return staged row counts."""
    unknown = sorted(set(sections) - set(STAGED_TABLES) - {"version"})
    if unknown:
        raise ValueError(f"package holds tables this loader does not handle: {unknown}")
    counts = {}
    for table in STAGED_TABLES:
        cur.execute(f"CREATE TEMP TABLE {table} (LIKE public.{table}) ON COMMIT DROP")
        declared, statements = sections.get(table, (0, ""))
        if statements:
            cur.execute(statements)
        cur.execute(f"SELECT count(*) FROM pg_temp.{table}")
        counts[table] = cur.fetchone()[0]
        if counts[table] != declared:
            raise ValueError(f"{table}: package declares {declared} rows, staged {counts[table]}")
    return counts


def drop_user_rows(cur) -> dict:
    """Remove rows that belong to users or sites; a region package must not carry them."""
    removed = {}
    for label, sql in (
        ("user and site grids", "DELETE FROM pg_temp.grid_result WHERE plz = 'USER' OR plz ~ '^S[0-9]+$'"),
        ("site buildings", "DELETE FROM pg_temp.buildings_result WHERE osm_id LIKE 'site:%'"),
        ("user-placed transformers", "DELETE FROM pg_temp.transformers WHERE type = 'UserPlaced'"),
    ):
        cur.execute(sql)
        removed[label] = cur.rowcount
    cur.execute("""DELETE FROM pg_temp.postcode_result WHERE postcode_result_plz = 'USER'
                   OR postcode_result_plz ~ '^S[0-9]+$'""")
    cur.execute("DELETE FROM pg_temp.postcode WHERE plz = 'USER' OR plz ~ '^S[0-9]+$'")
    for table in ("buildings_result", "lines_result", "transformer_positions"):
        cur.execute(f"DELETE FROM pg_temp.{table} t WHERE NOT EXISTS "
                    f"(SELECT 1 FROM pg_temp.grid_result g WHERE g.grid_result_id = t.grid_result_id)")
    return removed


def skip_existing_ways(cur) -> int:
    """Drop staged ways already in the target; return how many.

    ways has no natural key and its ids restart with every import, so a road
    shared with a neighbouring region is recognised by its geometry.
    """
    cur.execute("""DELETE FROM pg_temp.ways s WHERE EXISTS (
                       SELECT 1 FROM public.ways w WHERE w.geom && s.geom AND ST_Equals(w.geom, s.geom))""")
    return cur.rowcount


def refuse_present_regions(cur) -> list:
    """Raise if the target already has any of the package's postcodes; return the package's postcodes."""
    cur.execute("SELECT version_id FROM pg_temp.postcode_result EXCEPT SELECT version_id FROM public.version")
    missing = [r[0] for r in cur.fetchall()]
    if missing:
        raise ValueError(f"target has no version row for version_id {missing}")
    cur.execute("""SELECT s.country_code, s.postcode_result_plz FROM pg_temp.postcode_result s
                   JOIN public.postcode_result p USING (version_id, country_code, postcode_result_plz)
                   UNION SELECT s.country_code, s.plz FROM pg_temp.postcode s
                   JOIN public.postcode p USING (country_code, plz) ORDER BY 1, 2""")
    present = cur.fetchall()
    if present:
        raise ValueError(f"target already holds postcode(s) {present}; this loader only adds new regions")
    cur.execute("SELECT country_code, postcode_result_plz FROM pg_temp.postcode_result ORDER BY 1, 2")
    return cur.fetchall()


def reassign_ids(cur) -> None:
    """Replace the package's generated ids with ids from the target's sequences and current maxima."""
    cur.execute("LOCK TABLE public.ways, public.postcode, public.consumer_categories IN SHARE ROW EXCLUSIVE MODE")
    cur.execute("""CREATE TEMP TABLE grid_map ON COMMIT DROP AS
                   SELECT grid_result_id AS old_id, nextval('grid_result_grid_result_id_seq') AS new_id
                   FROM pg_temp.grid_result""")
    for table in ("grid_result", "buildings_result", "lines_result", "transformer_positions"):
        cur.execute(f"UPDATE pg_temp.{table} t SET grid_result_id = m.new_id FROM grid_map m "
                    f"WHERE t.grid_result_id = m.old_id")
    cur.execute("UPDATE pg_temp.lines_result SET lines_result_id = nextval('lines_result_lines_result_id_seq')")
    _renumber(cur, "ways", "way_id")
    _renumber(cur, "postcode", "postcode_id")
    _renumber(cur, "consumer_categories", "consumer_category_id",
              "WHERE NOT EXISTS (SELECT 1 FROM public.consumer_categories c WHERE c.definition = s.definition)")
    cur.execute("""UPDATE pg_temp.state s SET state_id = nextval('state_state_id_seq')
                   WHERE NOT EXISTS (SELECT 1 FROM public.state t
                                     WHERE t.state_code = s.state_code AND t.country_code = s.country_code)""")


def _renumber(cur, table: str, column: str, where: str = "") -> None:
    """Number staged rows after the target's current maximum; the caller holds a lock on the table."""
    cur.execute(f"""UPDATE pg_temp.{table} s SET {column} = n.new_id
                    FROM (SELECT ctid AS row_ref,
                                 (SELECT COALESCE(max({column}), 0) FROM public.{table})
                                 + row_number() OVER (ORDER BY {column}) AS new_id
                          FROM pg_temp.{table} s {where}) n
                    WHERE s.ctid = n.row_ref""")


def insert_all(cur) -> dict:
    """Insert every staged table into the target; return the rows each insert added."""
    added = {}
    for table in STAGED_TABLES:
        cur.execute(INSERT_SQL[table])
        added[table] = cur.rowcount
    return added


def refresh_views(conn) -> None:
    """Refresh the grid views after commit; plain REFRESH, since CONCURRENTLY needs unique indexes."""
    with conn.cursor() as cur:
        for name in REFRESH_QUERIES:
            cur.execute(f"REFRESH MATERIALIZED VIEW {name}")
            conn.commit()


def load_region(conn, path: Path, dry_run: bool) -> None:
    """Stage, check, renumber and insert one package; roll back instead of committing on dry_run."""
    sections = read_sections(path)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '15s'")
        staged = stage(cur, sections)
        removed = drop_user_rows(cur)
        postcodes = refuse_present_regions(cur)
        removed["ways already present"] = skip_existing_ways(cur)
        reassign_ids(cur)
        added = insert_all(cur)
    print(f"postcodes: {', '.join(f'{cc} {plz}' for cc, plz in postcodes)}")
    print("removed from package: " + ", ".join(f"{k} {v}" for k, v in removed.items()))
    for table in STAGED_TABLES:
        print(f"  {table}: staged {staged[table]}, added {added[table]}")
    if dry_run:
        conn.rollback()
        print("dry run: rolled back, target unchanged")
        return
    conn.commit()
    refresh_views(conn)
    print("committed and views refreshed")


def main() -> None:
    """Parse arguments and load one region package into the configured database."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", type=Path, help="export_pylovo.py output, .sql or .sql.gz")
    parser.add_argument("--dry-run", action="store_true", help="run every step, then roll back")
    args = parser.parse_args()
    from src.config_loader import DBNAME, DBUSER, HOST, PASSWORD, PORT
    with psycopg2.connect(dbname=DBNAME, user=DBUSER, password=PASSWORD, host=HOST, port=PORT) as conn:
        load_region(conn, args.package, args.dry_run)


if __name__ == "__main__":
    main()
