"""Generate a site's grid from its copied inputs with pylovo's normal algorithm."""
from typing import Callable

from src.database.database_client import DatabaseClient
from src.database.database_constructor import DatabaseConstructor
from src.grid_generator import GridGenerator
from src.site_mode.sites import site_plz

# The site's polygon becomes a postcode without a state, so it never appears
# in state listings or /boundary/available. Deleting the old row cascades to
# every result table of the previous generation.
RESET_POSTCODE_SQL = """
DELETE FROM postcode WHERE plz = %(plz)s;
UPDATE site SET generated_revision = NULL WHERE site_id = %(site)s;
INSERT INTO postcode (plz, country_code, state_code, note, geom)
SELECT %(plz)s, country_code, NULL, 'site ' || site_id, geom FROM site WHERE site_id = %(site)s;
"""

# origin: known = a station with a known rating, osm/user = a site station
# whose rating was computed, new = a station placed by the algorithm.
RECORD_TRANSFORMERS_SQL = """
INSERT INTO site_grid_transformer (grid_result_id, site_id, site_transformer_id, origin, loading_pct)
SELECT gr.grid_result_id, %(site)s, st.transformer_id,
       CASE WHEN st.transformer_id IS NULL THEN 'new'
            WHEN st.rated_kva IS NOT NULL THEN 'known'
            ELSE st.source END,
       l.loading_pct
FROM grid_result gr
LEFT JOIN unnest(%(kcids)s::int[], %(bcids)s::int[], %(ids)s::int[], %(pcts)s::float8[])
       AS l(kcid, bcid, site_transformer_id, loading_pct)
       ON l.kcid = gr.kcid AND l.bcid = gr.bcid
LEFT JOIN site_transformer st
       ON st.site_id = %(site)s AND st.transformer_id = l.site_transformer_id
WHERE gr.plz = %(plz)s;
"""


def _ensure_ways_functions(dbc: DatabaseClient) -> None:
    """Install the ways preprocessing functions if the database lacks them.

    The constructor installs them, but a database restored from a data dump,
    such as a deployment fixture, may not have them.
    """
    dbc.cur.execute("SELECT to_regprocedure('segment_intersecting_ways()') IS NOT NULL")
    if not dbc.cur.fetchone()[0]:
        DatabaseConstructor(dbc).load_ways_preprocessing_functions()


def _site_country(dbc: DatabaseClient, site_id: int) -> str:
    """Return the site's country code, raising LookupError if the site is gone."""
    dbc.cur.execute("SELECT country_code FROM site WHERE site_id = %s", (site_id,))
    row = dbc.cur.fetchone()
    if row is None:
        raise LookupError(f"Site {site_id} not found")
    return row[0]


def _record_results(dbc: DatabaseClient, site_id: int, revision: int, brownfield: dict) -> list:
    """Store per-grid station origin and loading; return overloaded site transformer ids."""
    keys = list(brownfield)
    dbc.cur.execute(RECORD_TRANSFORMERS_SQL, {
        "site": site_id, "plz": site_plz(site_id),
        "kcids": [k for k, _ in keys], "bcids": [b for _, b in keys],
        "ids": [brownfield[k][0] for k in keys], "pcts": [brownfield[k][1] for k in keys],
    })
    dbc.cur.execute(
        """SELECT site_transformer_id FROM site_grid_transformer
           WHERE site_id = %s AND loading_pct > 100 AND site_transformer_id IS NOT NULL
           ORDER BY site_transformer_id""",
        (site_id,),
    )
    overloaded = [r[0] for r in dbc.cur.fetchall()]
    dbc.cur.execute("UPDATE site SET generated_revision = %s WHERE site_id = %s", (revision, site_id))
    dbc.conn.commit()
    return overloaded


def generate_site(site_id: int, revision: int,
                  on_generator: Callable[[GridGenerator], None] | None = None) -> dict:
    """Replace the site's grid with one generated from its current inputs.

    Returns overloaded_transformer_ids (site stations whose fixed buildings
    exceed their known rating) and unroutable_building_ids (fixed assignments
    that could not be cabled along the street network, so the building was
    assigned automatically). on_generator, if given, receives the GridGenerator
    so a caller can read its current_stage. Raises on any generation error,
    leaving no result.
    """
    plz = site_plz(site_id)
    with DatabaseClient() as dbc:
        _ensure_ways_functions(dbc)
        country_code = _site_country(dbc, site_id)
        dbc.cur.execute(RESET_POSTCODE_SQL, {"plz": plz, "site": site_id})
        dbc.conn.commit()
    generator = GridGenerator(plz=plz, country_code=country_code, site_id=site_id)
    if on_generator is not None:
        on_generator(generator)
    try:
        generator.generate_grid_for_single_plz(plz, analyze_grids=False, raise_on_error=True)
        overloaded = _record_results(generator.dbc, site_id, revision, generator.site_brownfield)
        return {"overloaded_transformer_ids": overloaded,
                "unroutable_building_ids": generator.site_unroutable_building_ids}
    except Exception:
        with DatabaseClient() as dbc:
            dbc.cur.execute("DELETE FROM postcode WHERE plz = %s", (plz,))
            dbc.conn.commit()
        raise
