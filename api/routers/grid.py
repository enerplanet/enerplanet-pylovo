"""Grid generation and statistics endpoints."""
import json
import math
import threading
import time
import traceback

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString
from fastapi import APIRouter, HTTPException

from models import GridRequest, GridStatisticsRequest
from src.ai_estimation import estimate_building_energy
from src.database.database_client import DatabaseClient
from src.config_loader import CABLE_COST_DICT, VN, V_BAND_LOW, V_BAND_HIGH
from services.mv_lines import generate_synthetic_mv_lines
from utils.helpers import infer_input_srid

router = APIRouter(tags=["grid"])

VERSION_ID = "1"
_RUNTIME_SCHEMA_READY = False
_RUNTIME_SCHEMA_LOCK = threading.Lock()


def _to_float_or_none(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed


def _to_int_or_none(value):
    num = _to_float_or_none(value)
    if num is None:
        return None
    return int(round(num))


def _geometry_point(geometry):
    if geometry is None or getattr(geometry, 'is_empty', False):
        return None
    return geometry if geometry.geom_type == 'Point' else geometry.centroid


def _approx_wgs84_distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    avg_lat = math.radians((lat1 + lat2) / 2.0)
    dx = (lon1 - lon2) * 111.32 * math.cos(avg_lat)
    dy = (lat1 - lat2) * 110.57
    return max(round(math.hypot(dx, dy), 4), 0.01)


def _make_direct_connection_line(trafo_geom, building_geom) -> tuple[LineString, float]:
    trafo_pt = _geometry_point(trafo_geom)
    building_pt = _geometry_point(building_geom)
    if trafo_pt is None or building_pt is None:
        raise ValueError("Cannot build custom LV line without transformer and building geometry")

    line = LineString([(trafo_pt.x, trafo_pt.y), (building_pt.x, building_pt.y)])
    length_km = _approx_wgs84_distance_km(trafo_pt.x, trafo_pt.y, building_pt.x, building_pt.y)
    return line, length_km


class _StageTimer:
    """Records elapsed time between named points in a request."""

    def __init__(self):
        self._last = time.perf_counter()
        self._marks = []

    def mark(self, label: str) -> None:
        now = time.perf_counter()
        self._marks.append((label, now - self._last))
        self._last = now

    def summary(self) -> str:
        return " | ".join(f"{label}={secs:.2f}s" for label, secs in self._marks if secs >= 0.01)


def _apply_ai_estimates_to_buildings(buildings: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Align initial map values with /estimate-energy so first recalc is stable."""
    if buildings.empty:
        return buildings

    cache: dict[tuple, dict] = {}
    est_yearly: list[float] = []
    est_peak: list[float] = []
    est_hh_size: list[float | None] = []
    est_hh_count: list[float | None] = []
    est_applied: list[bool] = []
    est_source: list[str] = []

    for _, row in buildings.iterrows():
        osm_num = _to_float_or_none(row.get("osm_id"))
        # Keep user custom buildings as-is (they may have explicit demand overrides).
        if osm_num is not None and osm_num < 0:
            est_yearly.append(_to_float_or_none(row.get("yearly_demand_kwh")) or 0.0)
            est_peak.append(_to_float_or_none(row.get("peak_load_in_kw")) or 0.0)
            est_hh_size.append(_to_float_or_none(row.get("household_size")))
            est_hh_count.append(_to_float_or_none(row.get("estimated_households")))
            est_applied.append(True)
            est_source.append("custom_building_input")
            continue

        f_class = str(row.get("f_class") or row.get("type") or "unknown")
        footprint_area = _to_float_or_none(row.get("area")) or 0.0
        floors = _to_int_or_none(row.get("floors_3dbag"))
        if floors is None or floors < 1:
            floors = _to_int_or_none(row.get("floors"))
        if floors is None or floors < 1:
            floors = 1
        total_floor_area = max(1.0, footprint_area * float(floors))

        year_of_construction = _to_int_or_none(row.get("construction_year"))
        # Do not force postcode-average household size onto a specific building.
        # Let AI estimator infer building-level household size from class/geometry.
        household_hint = _to_int_or_none(row.get("household_size"))
        energy_label_hint = row.get("energy_label") or None
        if isinstance(energy_label_hint, str):
            energy_label_hint = energy_label_hint.strip() or None

        cache_key = (
            f_class.strip().lower(),
            round(total_floor_area, 2),
            year_of_construction,
            household_hint,
            floors,
            energy_label_hint,
        )

        estimate = cache.get(cache_key)
        if estimate is None:
            estimate = estimate_building_energy(
                building_type=f_class,
                area_m2=total_floor_area,
                year=year_of_construction,
                household_size=household_hint,
                num_floors=floors,
                energy_label=energy_label_hint,
            )
            cache[cache_key] = estimate

        est_yearly.append(_to_float_or_none(estimate.get("yearly_demand_kwh")) or 0.0)
        est_peak.append(_to_float_or_none(estimate.get("peak_load_kw")) or 0.0)
        est_hh_size.append(_to_float_or_none(estimate.get("household_size_used")))
        est_hh_count.append(_to_float_or_none(estimate.get("estimated_households_used")))
        est_applied.append(True)
        est_source.append(str(estimate.get("source") or "consumer_categories_fclass_model"))

    buildings["yearly_demand_kwh"] = est_yearly
    buildings["peak_load_in_kw"] = est_peak
    buildings["household_size"] = est_hh_size
    buildings["estimated_households"] = est_hh_count
    buildings["ai_estimate_applied"] = est_applied
    buildings["ai_estimate_source"] = est_source
    return buildings


def _build_scope_filter(payload: GridRequest, alias: str) -> tuple[str, list]:
    """Build a deterministic model/draft/user scope filter for SQL clauses.

    Scope precedence is model -> draft. user_id is always applied as AND when present.
    """
    clauses = []
    params = []

    if payload.model_id is not None:
        clauses.append(f"{alias}.model_id = %s")
        params.append(payload.model_id)
    elif payload.draft_id:
        clauses.append(f"{alias}.draft_id = %s")
        params.append(payload.draft_id)

    if payload.user_id:
        clauses.append(f"{alias}.user_id = %s")
        params.append(payload.user_id)

    return " AND ".join(clauses), params


def ensure_runtime_schema(dbc: DatabaseClient) -> None:
    """Validate compatibility schema once during worker startup."""
    global _RUNTIME_SCHEMA_READY
    if _RUNTIME_SCHEMA_READY:
        return

    with _RUNTIME_SCHEMA_LOCK:
        if _RUNTIME_SCHEMA_READY:
            return
        try:
            # The Python lock only coordinates threads in one worker. A
            # transaction-scoped PostgreSQL advisory lock serializes schema
            # checks across all API workers and containers.
            dbc.cur.execute("SET LOCAL lock_timeout = '15s';")
            dbc.cur.execute("SELECT pg_advisory_xact_lock(7046029254386353131);")

            dbc.cur.execute(
                """
                WITH required(table_name, column_name) AS (
                    VALUES
                        ('grid_result', 'user_id'),
                        ('grid_result', 'model_id'),
                        ('grid_result', 'draft_id'),
                        ('buildings_result', 'f_classes'),
                        ('buildings_result', 'height_max'),
                        ('buildings_result', 'height_ground'),
                        ('buildings_result', 'height_median'),
                        ('buildings_result', 'floors_3dbag'),
                        ('buildings_result', 'bag_id'),
                        ('buildings_result', 'energy_label'),
                        ('buildings_result', 'energy_index'),
                        ('buildings_result', 'cbs_population'),
                        ('buildings_result', 'cbs_households'),
                        ('buildings_result', 'cbs_avg_household_size'),
                        ('res', 'f_classes'),
                        ('res', 'energy_label'),
                        ('res', 'energy_index'),
                        ('oth', 'f_classes'),
                        ('oth', 'energy_label'),
                        ('oth', 'energy_index'),
                        ('equipment_data', 'equipment_only_cost_eur'),
                        ('equipment_data', 'installed_cost_eur')
                ),
                existing AS (
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = ANY(current_schemas(FALSE))
                )
                SELECT
                    NOT EXISTS (
                        SELECT table_name, column_name FROM required
                        EXCEPT
                        SELECT table_name, column_name FROM existing
                    )
                    AND to_regclass('building_transformer_assignments') IS NOT NULL;
                """
            )
            if dbc.cur.fetchone()[0]:
                dbc.conn.commit()
                _RUNTIME_SCHEMA_READY = True
                print("[Schema] Runtime schema already current")
                return

            dbc.cur.execute("ALTER TABLE grid_result ADD COLUMN IF NOT EXISTS user_id VARCHAR(255);")
            dbc.cur.execute("ALTER TABLE grid_result ADD COLUMN IF NOT EXISTS model_id INTEGER;")
            dbc.cur.execute("ALTER TABLE grid_result ADD COLUMN IF NOT EXISTS draft_id VARCHAR(255);")

            dbc.cur.execute(
                """
                CREATE TABLE IF NOT EXISTS building_transformer_assignments (
                    assignment_id SERIAL PRIMARY KEY,
                    building_osm_id VARCHAR NOT NULL,
                    grid_result_id BIGINT NOT NULL,
                    user_id VARCHAR(255),
                    model_id INTEGER,
                    draft_id VARCHAR(255),
                    version_id VARCHAR(10) DEFAULT '1',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            dbc.cur.execute("ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS f_classes text;")
            for col, dtype in [
                ("height_max", "double precision"), ("height_ground", "double precision"),
                ("height_median", "double precision"), ("floors_3dbag", "integer"),
                ("bag_id", "varchar"), ("energy_label", "varchar(5)"), ("energy_index", "double precision"),
                ("cbs_population", "double precision"), ("cbs_households", "double precision"),
                ("cbs_avg_household_size", "double precision"),
            ]:
                dbc.cur.execute(f"ALTER TABLE buildings_result ADD COLUMN IF NOT EXISTS {col} {dtype};")

            # Backward-compatible optional columns used in joined queries.
            dbc.cur.execute("ALTER TABLE IF EXISTS res ADD COLUMN IF NOT EXISTS f_classes text;")
            dbc.cur.execute("ALTER TABLE IF EXISTS res ADD COLUMN IF NOT EXISTS energy_label VARCHAR(5);")
            dbc.cur.execute("ALTER TABLE IF EXISTS res ADD COLUMN IF NOT EXISTS energy_index DOUBLE PRECISION;")
            dbc.cur.execute("ALTER TABLE IF EXISTS oth ADD COLUMN IF NOT EXISTS f_classes text;")
            dbc.cur.execute("ALTER TABLE IF EXISTS oth ADD COLUMN IF NOT EXISTS energy_label VARCHAR(5);")
            dbc.cur.execute("ALTER TABLE IF EXISTS oth ADD COLUMN IF NOT EXISTS energy_index DOUBLE PRECISION;")
            dbc.cur.execute("ALTER TABLE IF EXISTS equipment_data ADD COLUMN IF NOT EXISTS equipment_only_cost_eur INTEGER;")
            dbc.cur.execute("ALTER TABLE IF EXISTS equipment_data ADD COLUMN IF NOT EXISTS installed_cost_eur INTEGER;")
            dbc.conn.commit()
            _RUNTIME_SCHEMA_READY = True
            print("[Schema] Runtime schema checks completed")
        except Exception as schema_err:
            dbc.conn.rollback()
            print(f"[Schema] Runtime schema check failed: {schema_err}")
            raise


def _fetch_state_boundary_bbox_for_centroid(dbc: DatabaseClient, lon: float, lat: float) -> dict | None:
    """Return state-level boundary bbox (WGS84 GeoJSON) for a centroid point."""
    dbc.cur.execute(
        """
        WITH point_3035 AS (
            SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) AS geom
        ),
        containing_states AS (
            SELECT pr.country_code, pr.state_code
            FROM postcode_result pr
            JOIN point_3035 pt ON ST_Intersects(pr.geom, pt.geom)
            WHERE pr.version_id = %s
              AND pr.state_code IS NOT NULL
              AND pr.state_code <> ''
            GROUP BY pr.country_code, pr.state_code
        ),
        selected_state AS (
            SELECT cs.country_code, cs.state_code
            FROM containing_states cs
            JOIN postcode_result pr
              ON pr.version_id = %s
             AND pr.country_code = cs.country_code
             AND pr.state_code = cs.state_code
            GROUP BY cs.country_code, cs.state_code
            ORDER BY ST_Area(ST_Envelope(ST_Extent(pr.geom)::geometry)) ASC
            LIMIT 1
        )
        SELECT ss.country_code,
               ss.state_code,
               COALESCE(s.state_name, ss.state_code) AS state_name,
               ST_AsGeoJSON(
                   ST_Transform(
                       ST_SetSRID(ST_Envelope(ST_Extent(pr.geom)::geometry), 3035),
                       4326
                   )
               ) AS boundary_geojson
        FROM selected_state ss
        JOIN postcode_result pr
          ON pr.version_id = %s
         AND pr.country_code = ss.country_code
         AND pr.state_code = ss.state_code
        LEFT JOIN state s
          ON s.country_code = ss.country_code
         AND s.state_code = ss.state_code
        GROUP BY ss.country_code, ss.state_code, s.state_name;
        """,
        (lon, lat, VERSION_ID, VERSION_ID, VERSION_ID),
    )
    row = dbc.cur.fetchone()
    if not row:
        return None

    country_code, state_code, state_name, boundary_geojson = row
    if not boundary_geojson:
        return None

    geometry = json.loads(boundary_geojson)
    country_code_str = str(country_code or "").upper()
    state_code_str = str(state_code or "")
    state_name_str = str(state_name or state_code_str or "Unknown")

    return {
        "region": {
            "name": state_name_str,
            "admin_level": 4,
            "country": None,
            "country_code": country_code_str,
            "state_code": state_code_str,
            "osm_id": None,
            "osm_type": None,
            "source": "postcode_result_bbox",
        },
        "boundary": {
            "type": "Feature",
            "properties": {
                "name": state_name_str,
                "admin_level": 4,
                "country_code": country_code_str,
                "state_code": state_code_str,
                "source": "postcode_result_bbox",
            },
            "geometry": geometry,
        },
    }


@router.post("/generate-grid")
async def generate_grid_phase1(payload: GridRequest):
    """
    Use the same algorithm and SQL pattern as your verified
    GeoPandas script so that Pylovo returns exactly the same
    buildings for a given polygon.
    """
    dbc = None
    try:
        request_started = time.perf_counter()
        stages = _StageTimer()
        dbc = DatabaseClient()
        input_srid = infer_input_srid(payload.geom)
        print(f"[Grid Query] Detected input SRID: {input_srid}")

        # 1. Buildings - use building_transformer_assignments for model-specific transformer assignments
        # The assignments table overrides the original grid_result_id for specific models/drafts
        
        # Build assignment filter based on model/draft/user context.
        assignment_filter, assignment_params = _build_scope_filter(payload, alias="bta")

        print(f"[Buildings Query] assignment_filter={assignment_filter}, assignment_params={assignment_params}")

        if assignment_filter:
            # Query with assignment override - uses COALESCE to prefer assignment grid_result_id
            # Params order: geom, assignment_params, version_id
            buildings_params = [json.dumps(payload.geom)] + assignment_params + [VERSION_ID]
            buildings = gpd.GeoDataFrame.from_postgis(
                f"""
                WITH poly AS (
                  SELECT ST_Transform(
                           ST_CollectionExtract(
                             ST_MakeValid(
                               ST_SetSRID(ST_GeomFromGeoJSON(%s), {input_srid})
                             ),
                             3
                           ),
                           3035
                         ) AS g
                )
                SELECT br.osm_id,
                       COALESCE(bta.grid_result_id, br.grid_result_id) AS grid_result_id,
                       ST_Transform(br.geom, 4326) AS geom,
                       br.type,
                       COALESCE(cls.f_class, br.f_class) AS f_class,
                       COALESCE(NULLIF(cls.f_classes, ''), NULLIF(br.f_classes, ''), COALESCE(cls.f_class, br.f_class)) AS f_classes,
                       br.area,
                       br.peak_load_in_kw,
                       CASE
                           WHEN COALESCE(cc.load_method, defcc.load_method, 'area') = 'household' THEN
                               COALESCE(br.households_per_building, 1) * COALESCE(cc.yearly_consumption, defcc.yearly_consumption, 0)
                           ELSE
                               COALESCE(br.area, 0) * COALESCE(cc.yearly_consumption_per_m2, defcc.yearly_consumption_per_m2, 0)
                       END AS yearly_demand_kwh,
                       br.height_max,
                       br.height_ground,
                       br.height_median,
                       br.floors_3dbag,
                       br.bag_id,
                       COALESCE(NULLIF(TRIM(br.energy_label), ''), ep.energy_label) AS energy_label,
                       COALESCE(br.energy_index, ep.energy_index) AS energy_index,
                       br.cbs_population,
                       br.cbs_households,
                       br.cbs_avg_household_size,
                       br.floors,
                       br.construction_year,
                       gr.country_code
                FROM buildings_result br
                CROSS JOIN poly
                LEFT JOIN grid_result gr ON gr.grid_result_id = br.grid_result_id
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
                LEFT JOIN LATERAL (
                    SELECT
                        NULLIF(TRIM(COALESCE(r.energy_label, '')), '') AS energy_label,
                        r.energy_index
                    FROM res r
                    WHERE r.osm_id = br.osm_id
                    ORDER BY
                        CASE WHEN NULLIF(TRIM(COALESCE(r.energy_label, '')), '') IS NULL THEN 1 ELSE 0 END,
                        r.energy_index NULLS LAST
                    LIMIT 1
                ) ep ON TRUE
                LEFT JOIN consumer_categories cc
                    ON LOWER(TRIM(cc.definition)) = LOWER(TRIM(COALESCE(cls.f_class, br.f_class)))
                LEFT JOIN consumer_categories defcc
                    ON defcc.definition = '_default'
                LEFT JOIN building_transformer_assignments bta
                    ON br.osm_id = bta.building_osm_id AND {assignment_filter}
                WHERE br.version_id = %s
                  AND br.vertice_id IS NOT NULL
                  AND ST_Intersects(br.geom, poly.g);
                """,
                con=dbc.conn,
                params=buildings_params,
                geom_col="geom"
            )
        else:
            # No model context - just get original buildings without any assignments
            buildings_params = [json.dumps(payload.geom), VERSION_ID]
            buildings = gpd.GeoDataFrame.from_postgis(
                f"""
                WITH poly AS (
                  SELECT ST_Transform(
                           ST_CollectionExtract(
                             ST_MakeValid(
                               ST_SetSRID(ST_GeomFromGeoJSON(%s), {input_srid})
                             ),
                             3
                           ),
                           3035
                         ) AS g
                )
                SELECT br.osm_id,
                       br.grid_result_id,
                       ST_Transform(br.geom, 4326) AS geom,
                       br.type,
                       COALESCE(cls.f_class, br.f_class) AS f_class,
                       COALESCE(NULLIF(cls.f_classes, ''), NULLIF(br.f_classes, ''), COALESCE(cls.f_class, br.f_class)) AS f_classes,
                       br.area,
                       br.peak_load_in_kw,
                       CASE
                           WHEN COALESCE(cc.load_method, defcc.load_method, 'area') = 'household' THEN
                               COALESCE(br.households_per_building, 1) * COALESCE(cc.yearly_consumption, defcc.yearly_consumption, 0)
                           ELSE
                               COALESCE(br.area, 0) * COALESCE(cc.yearly_consumption_per_m2, defcc.yearly_consumption_per_m2, 0)
                       END AS yearly_demand_kwh,
                       br.height_max,
                       br.height_ground,
                       br.height_median,
                       br.floors_3dbag,
                       br.bag_id,
                       COALESCE(NULLIF(TRIM(br.energy_label), ''), ep.energy_label) AS energy_label,
                       COALESCE(br.energy_index, ep.energy_index) AS energy_index,
                       br.cbs_population,
                       br.cbs_households,
                       br.cbs_avg_household_size,
                       br.floors,
                       br.construction_year,
                       gr.country_code
                FROM buildings_result br
                CROSS JOIN poly
                LEFT JOIN grid_result gr ON gr.grid_result_id = br.grid_result_id
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
                LEFT JOIN LATERAL (
                    SELECT
                        NULLIF(TRIM(COALESCE(r.energy_label, '')), '') AS energy_label,
                        r.energy_index
                    FROM res r
                    WHERE r.osm_id = br.osm_id
                    ORDER BY
                        CASE WHEN NULLIF(TRIM(COALESCE(r.energy_label, '')), '') IS NULL THEN 1 ELSE 0 END,
                        r.energy_index NULLS LAST
                    LIMIT 1
                ) ep ON TRUE
                LEFT JOIN consumer_categories cc
                    ON LOWER(TRIM(cc.definition)) = LOWER(TRIM(COALESCE(cls.f_class, br.f_class)))
                LEFT JOIN consumer_categories defcc
                    ON defcc.definition = '_default'
                WHERE br.version_id = %s
                  AND br.vertice_id IS NOT NULL
                  AND ST_Intersects(br.geom, poly.g);
                """,
                con=dbc.conn,
                params=buildings_params,
                geom_col="geom"
            )

        stages.mark("buildings")
        # 1b. Custom Buildings
        try:
            dbc.cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'custom_buildings');")
            if dbc.cur.fetchone()[0]:
                user_id_param = payload.user_id or ''
                include_public = payload.include_public_buildings if payload.include_public_buildings is not None else True
                include_private = payload.include_private_buildings if payload.include_private_buildings is not None else True
                excluded_ids = payload.excluded_building_ids or []

                print(f"[Custom Buildings Filter] include_public={include_public}, include_private={include_private}, user_id={user_id_param}, excluded_ids={excluded_ids}")

                visibility_conditions = []
                if include_public:
                    visibility_conditions.append("cb.is_public = TRUE")
                if include_private and user_id_param:
                    visibility_conditions.append(f"(cb.user_id = '{user_id_param}' AND (cb.is_public = FALSE OR cb.is_public IS NULL))")

                if not visibility_conditions:
                    print("[Custom Buildings] Both public and private are disabled - skipping custom buildings")
                else:
                    visibility_clause = " OR ".join(visibility_conditions)
                    exclusion_clause = ""
                    if excluded_ids:
                        exclusion_clause = f" AND cb.custom_building_id NOT IN ({','.join(map(str, excluded_ids))})"

                    print(f"[Custom Buildings] Visibility clause: {visibility_clause}")
                    print(f"[Custom Buildings] Exclusion clause: {exclusion_clause}")

                    query = f"""
                    WITH poly AS (
                      SELECT ST_Transform(
                               ST_CollectionExtract(
                                 ST_MakeValid(
                                   ST_SetSRID(ST_GeomFromGeoJSON(%s), {input_srid})
                                 ),
                                 3
                               ),
                               3035
                             ) AS g
                    ),
                    custom_in_poly AS (
                        SELECT cb.custom_building_id,
                               cb.user_id,
                               cb.title,
                               cb.f_class,
                               cb.building_type,
                               cb.area,
                               cb.peak_load_kw,
                               cb.demand_energy,
                               cb.is_public,
                               cb.icon,
                               cb.geom,
                               cb.geom_area
                        FROM custom_buildings cb, poly
                        WHERE ST_Intersects(ST_Transform(cb.geom, 3035), poly.g)
                          AND ({visibility_clause}){exclusion_clause}
                    )
                    SELECT (-c.custom_building_id) as osm_id,
                           COALESCE(
                               (SELECT tp.grid_result_id
                                FROM transformer_positions tp
                                JOIN grid_result gr ON gr.grid_result_id = tp.grid_result_id
                                WHERE tp.version_id = '1'
                                  AND COALESCE(gr.plz, '') <> 'USER'
                                ORDER BY tp.geom <-> ST_Transform(c.geom, 3035)
                                LIMIT 1),
                               -1
                           ) as grid_result_id,
                           ST_Transform(c.geom, 4326) as geom,
                           c.building_type as type,
                           c.area,
                           c.peak_load_kw as peak_load_in_kw,
                           COALESCE(c.demand_energy, 0) as yearly_demand_kwh,
                           c.f_class,
                           c.f_class as f_classes,
                           COALESCE(c.icon, 'building-2') as icon,
                           COALESCE(c.is_public, FALSE) as is_public,
                           c.title
                    FROM custom_in_poly c;
                    """

                    params = [json.dumps(payload.geom)]

                    custom_buildings = gpd.GeoDataFrame.from_postgis(
                        query,
                        con=dbc.conn,
                        params=params,
                        geom_col="geom"
                    )

                    if not custom_buildings.empty:
                        print(f"[Custom Buildings] Found {len(custom_buildings)} custom buildings")
                        print(f"[Custom Buildings] IDs: {custom_buildings['osm_id'].tolist()}")
                        buildings = pd.concat([buildings, custom_buildings], ignore_index=True)
                    else:
                        print("[Custom Buildings] No custom buildings found in polygon after filtering")
        except Exception as e:
            print(f"[Custom Buildings] Error: {e}")
            traceback.print_exc()

        try:
            print("=== Pylovo /generate-grid request ===")
            print(f"Incoming geom type: {payload.geom.get('type')}")
            print(f"Number of buildings (POIs) in area: {len(buildings)}")
        except Exception:
            pass

        if buildings.empty:
            total_elapsed = time.perf_counter() - request_started
            print(f"[Timing] /generate-grid completed in {total_elapsed:.2f}s (empty result)")
            return {
                "status": "success",
                "buildings": {"type": "FeatureCollection", "features": []},
                "transformers": {"type": "FeatureCollection", "features": []},
                "lines": {"type": "FeatureCollection", "features": []},
                "grids": [],
            }

        stages.mark("custom_buildings")
        buildings = _apply_ai_estimates_to_buildings(buildings)
        stages.mark("ai_estimates")

        buildings["group_id"] = buildings["grid_result_id"]
        grid_ids = buildings["grid_result_id"].unique().tolist()
        grids_info = [{"grid_result_id": int(gid)} for gid in grid_ids]

        # 2. Transformers (system transformers only, exclude user-placed)
        transformers = gpd.GeoDataFrame.from_postgis(
            """
            SELECT tp.osm_id,
                   tp.grid_result_id,
                   gr.transformer_rated_power AS rated_power_kva,
                   ST_Transform(tp.geom, 4326) AS geom
            FROM transformer_positions tp
            JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
            WHERE tp.version_id = %s
              AND tp.grid_result_id = ANY(%s)
              AND COALESCE(gr.plz, '') <> 'USER';
            """,
            con=dbc.conn,
            params=[VERSION_ID, grid_ids],
            geom_col="geom"
        )

        # 2b. User-placed transformers
        user_transformers = gpd.GeoDataFrame()

        if payload.model_id is not None or payload.draft_id:
            user_filter_conditions = ["gr.plz = 'USER'"]
            user_params = [json.dumps(payload.geom), VERSION_ID]

            if payload.model_id is not None:
                user_filter_conditions.append("gr.model_id = %s")
                user_params.append(payload.model_id)
            elif payload.draft_id:
                user_filter_conditions.append("gr.draft_id = %s")
                user_params.append(payload.draft_id)

            if payload.user_id:
                user_filter_conditions.append("gr.user_id = %s")
                user_params.append(payload.user_id)

            user_filter_sql = " AND ".join(user_filter_conditions)

            user_transformers = gpd.GeoDataFrame.from_postgis(
                f"""
                WITH poly AS (
                  SELECT ST_Transform(
                           ST_CollectionExtract(
                             ST_MakeValid(
                               ST_SetSRID(ST_GeomFromGeoJSON(%s), {input_srid})
                             ),
                             3
                           ),
                           3035
                         ) AS g
                )
                SELECT tp.osm_id,
                       tp.grid_result_id,
                       gr.transformer_rated_power AS rated_power_kva,
                       ST_Transform(tp.geom, 4326) AS geom
                FROM transformer_positions tp
                JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id, poly
                WHERE tp.version_id = %s
                  AND {user_filter_sql}
                  AND ST_Intersects(tp.geom, poly.g);
                """,
                con=dbc.conn,
                params=user_params,
                geom_col="geom"
            )

        if not user_transformers.empty:
            user_grid_ids = user_transformers["grid_result_id"].unique().tolist()
            for ugid in user_grid_ids:
                if ugid not in grid_ids:
                    grid_ids.append(ugid)
                    grids_info.append({"grid_result_id": int(ugid)})
            transformers = pd.concat([transformers, user_transformers], ignore_index=True).drop_duplicates(subset=['grid_result_id'])

        # 2c. Fetch user-placed transformers OUTSIDE polygon that have buildings inside polygon assigned
        if payload.user_id and (payload.model_id is not None or payload.draft_id) and not buildings.empty:
            try:
                existing_grid_ids = transformers["grid_result_id"].unique().tolist() if not transformers.empty else []
                building_osm_ids = buildings["osm_id"].astype(str).unique().tolist()

                if building_osm_ids:
                    outside_conditions = []
                    outside_scope_params = []

                    if payload.model_id is not None:
                        outside_conditions.extend(["bta.model_id = %s", "gr.model_id = %s"])
                        outside_scope_params.extend([payload.model_id, payload.model_id])
                    elif payload.draft_id:
                        outside_conditions.extend(["bta.draft_id = %s", "gr.draft_id = %s"])
                        outside_scope_params.extend([payload.draft_id, payload.draft_id])

                    outside_conditions.extend(["bta.user_id = %s", "gr.user_id = %s"])
                    outside_scope_params.extend([payload.user_id, payload.user_id])

                    outside_sql = f"""
                        SELECT DISTINCT tp.osm_id,
                               tp.grid_result_id,
                               gr.transformer_rated_power AS rated_power_kva,
                               ST_Transform(tp.geom, 4326) AS geom
                        FROM transformer_positions tp
                        JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
                        JOIN building_transformer_assignments bta ON bta.grid_result_id = tp.grid_result_id
                        WHERE tp.version_id = %s
                          AND gr.plz = 'USER'
                          AND bta.building_osm_id = ANY(%s)
                          AND tp.grid_result_id != ALL(%s)
                          AND {' AND '.join(outside_conditions)};
                    """
                    outside_params = [
                        VERSION_ID,
                        building_osm_ids,
                        existing_grid_ids if existing_grid_ids else [0],
                        *outside_scope_params,
                    ]

                    outside_transformers = gpd.GeoDataFrame.from_postgis(
                        outside_sql, con=dbc.conn, params=outside_params, geom_col="geom"
                    )

                    if not outside_transformers.empty:
                        print(f"[Transformers] Found {len(outside_transformers)} transformer(s) outside polygon with assigned buildings")
                        for ugid in outside_transformers["grid_result_id"].unique().tolist():
                            if ugid not in grid_ids:
                                grid_ids.append(ugid)
                                grids_info.append({"grid_result_id": int(ugid)})
                        transformers = pd.concat([transformers, outside_transformers], ignore_index=True).drop_duplicates(subset=['grid_result_id'])
            except Exception as e:
                print(f"[Transformers] Error fetching outside transformers: {e}")
                traceback.print_exc()

        # 2d. Reassign custom buildings that have no transformer (grid_result_id == -1)
        #     to the nearest transformer from the already-fetched set.
        if not transformers.empty and not buildings.empty:
            try:
                buildings['osm_id_num'] = pd.to_numeric(buildings['osm_id'], errors='coerce')
                orphan_mask = (buildings['osm_id_num'] < 0) & (buildings['grid_result_id'] == -1)
                orphan_count = orphan_mask.sum()
                if orphan_count > 0:
                    print(f"[Custom Buildings] Reassigning {orphan_count} orphaned custom building(s) to nearest transformer")
                    for idx in buildings[orphan_mask].index:
                        bldg_geom = buildings.at[idx, 'geom']
                        if bldg_geom is None:
                            continue
                        bldg_pt = bldg_geom.centroid if bldg_geom.geom_type != 'Point' else bldg_geom
                        min_dist = float('inf')
                        nearest_gid = None
                        for _, trafo in transformers.iterrows():
                            trafo_geom = trafo['geom']
                            if trafo_geom is None:
                                continue
                            trafo_pt = trafo_geom.centroid if trafo_geom.geom_type != 'Point' else trafo_geom
                            d = bldg_pt.distance(trafo_pt)
                            if d < min_dist:
                                min_dist = d
                                nearest_gid = trafo['grid_result_id']
                        if nearest_gid is not None:
                            buildings.at[idx, 'grid_result_id'] = nearest_gid
                            buildings.at[idx, 'group_id'] = nearest_gid
                            print(f"    Custom building {buildings.at[idx, 'osm_id']} -> grid_result_id={nearest_gid} (dist={min_dist:.6f})")
                    # Refresh grid_ids after reassignment
                    grid_ids = buildings["grid_result_id"].unique().tolist()
                    grids_info = [{"grid_result_id": int(gid)} for gid in grid_ids]
            except Exception as e:
                print(f"[Custom Buildings] Error reassigning orphaned buildings: {e}")
                traceback.print_exc()

        stages.mark("transformers")
        # 3. Lines/Cables - filter by polygon to only show cables within the polygon area
        # All lines are stored in EPSG:3035, so we transform polygon to 3035 and filter

        # First, count total lines for these grids (for debugging)
        dbc.cur.execute("SELECT COUNT(*) FROM lines_result WHERE grid_result_id = ANY(%s)", (grid_ids,))
        total_lines_count = dbc.cur.fetchone()[0]
        print(f"[Lines Query] Total lines for grids {grid_ids}: {total_lines_count}")

        # Detect input SRID from the polygon coordinates
        print(f"[Lines Query] Detected input SRID: {input_srid}")

        # Get vertex IDs of buildings that have been reassigned (to exclude their old cables)
        reassigned_vertices = []
        if payload.model_id is not None or payload.draft_id:
            try:
                reassign_where, reassign_params = _build_scope_filter(payload, alias="bta")
                if not reassign_where:
                    reassign_where = "1=1"

                # Get vertex IDs of reassigned buildings from their ORIGINAL grid
                dbc.cur.execute(f"""
                    SELECT DISTINCT br.vertice_id, br.grid_result_id as original_grid
                    FROM building_transformer_assignments bta
                    JOIN buildings_result br
                      ON br.version_id = %s
                     AND br.osm_id = bta.building_osm_id
                    WHERE ({reassign_where})
                      AND br.vertice_id IS NOT NULL
                      AND bta.grid_result_id != br.grid_result_id;
                """, [VERSION_ID] + reassign_params)
                reassigned_data = dbc.cur.fetchall()
                reassigned_vertices = [(row[0], row[1]) for row in reassigned_data if row[0] is not None]
                print(f"[Lines Query] Found {len(reassigned_vertices)} reassigned building vertices to exclude")
            except Exception as e:
                print(f"[Lines Query] Error getting reassigned vertices: {e}")
                reassigned_vertices = []

        try:
            # Build exclusion clause for reassigned buildings
            # Exclude cables from original grid that connect to reassigned building vertices
            exclusion_clause = ""
            if reassigned_vertices:
                exclusion_parts = []
                for vertex_id, original_grid in reassigned_vertices:
                    exclusion_parts.append(f"(lr.grid_result_id = {original_grid} AND (lr.from_bus = {vertex_id} OR lr.to_bus = {vertex_id}))")
                if exclusion_parts:
                    exclusion_clause = "AND NOT (" + " OR ".join(exclusion_parts) + ")"

            # Return all cables for matched grids (grid_result_id already scopes correctly)
            # Also exclude cables that connect to reassigned buildings from their original grid
            lines = gpd.GeoDataFrame.from_postgis(
                f"""
                SELECT lr.lines_result_id AS line_id,
                       lr.grid_result_id,
                       lr.line_name,
                       lr.std_type AS cable_type,
                       lr.length_km,
                       ST_Transform(lr.geom, 4326) AS geom
                FROM lines_result lr
                WHERE lr.grid_result_id = ANY(%s)
                  {exclusion_clause};
                """,
                con=dbc.conn,
                params=[grid_ids],
                geom_col="geom"
            )
            print(f"[Lines Query] Found {len(lines)} cables for {len(grid_ids)} grids (from {total_lines_count} total, excluded {len(reassigned_vertices)} reassigned)")
        except Exception as lines_err:
            print(f"[Lines Query] ERROR: {lines_err}")
            traceback.print_exc()
            # Fallback to unfiltered query
            print("[Lines Query] Falling back to unfiltered query")
            lines = gpd.GeoDataFrame.from_postgis(
                """
                SELECT lines_result_id AS line_id,
                       grid_result_id,
                       line_name,
                       std_type AS cable_type,
                       length_km,
                       ST_Transform(geom, 4326) AS geom
                FROM lines_result
                WHERE grid_result_id = ANY(%s);
                """,
                con=dbc.conn,
                params=[grid_ids],
                geom_col="geom"
            )
            print(f"[Lines Query] Fallback returned {len(lines)} cables")

        stages.mark("lines")
        # 3b. Generate synthetic LV lines for custom buildings
        try:
            buildings['osm_id_num'] = pd.to_numeric(buildings['osm_id'], errors='coerce')
            custom_bldgs = buildings[buildings['osm_id_num'] < 0]
            print(f"Custom buildings for LV lines: {len(custom_bldgs)}, Transformers: {len(transformers)}")
            if not custom_bldgs.empty and not transformers.empty:
                synthetic_lines = []
                line_id_start = lines['line_id'].max() + 1 if not lines.empty and 'line_id' in lines.columns else 100000

                for idx, bldg in custom_bldgs.iterrows():
                    bldg_grid_id = bldg.get('grid_result_id')
                    bldg_osm_id = bldg.get('osm_id_num') or bldg.get('osm_id')
                    print(f"  Processing custom building {bldg_osm_id}, grid_result_id={bldg_grid_id}")
                    if bldg_grid_id is None or bldg_grid_id == -1:
                        print(f"    Skipping: no valid grid_result_id")
                        continue

                    trafo = transformers[transformers['grid_result_id'] == bldg_grid_id]
                    if trafo.empty:
                        print(f"    Skipping: no transformer found for grid_result_id={bldg_grid_id}")
                        print(f"    Available transformer grid_ids: {transformers['grid_result_id'].unique().tolist()}")
                        continue

                    trafo_geom = trafo.iloc[0]['geom']
                    bldg_geom = bldg['geom']

                    if trafo_geom and bldg_geom:
                        line_geom, length_km = _make_direct_connection_line(trafo_geom, bldg_geom)
                        print(f"    Direct custom LV connection: {round(length_km, 3)} km")

                        synthetic_lines.append({
                            'line_id': line_id_start,
                            'grid_result_id': bldg_grid_id,
                            'line_name': f"custom_line_{abs(int(bldg_osm_id))}",
                            'cable_type': 'NAYY 4x150',
                            'length_km': round(length_km, 4),
                            'geom': line_geom
                        })
                        line_id_start += 1

                if synthetic_lines:
                    synthetic_lines_gdf = gpd.GeoDataFrame(synthetic_lines, geometry='geom', crs='EPSG:4326')
                    lines = pd.concat([lines, synthetic_lines_gdf], ignore_index=True)
                    print(f"Generated {len(synthetic_lines)} synthetic LV lines for custom buildings")

        except Exception as e:
            print(f"Error generating synthetic LV lines for custom buildings: {e}")
            traceback.print_exc()

        stages.mark("synth_custom")
        # 3c. Generate synthetic LV lines for REASSIGNED regular buildings
        # When a building is reassigned to a user-placed transformer, create a cable to the new transformer
        try:
            if payload.user_id and not transformers.empty and not buildings.empty:
                from shapely.geometry import LineString
                from shapely import wkb

                # Get buildings in our polygon that are assigned to user-placed transformers
                building_osm_ids = buildings["osm_id"].astype(str).unique().tolist()

                # Find buildings assigned to user-placed transformers (different from original grid)
                reassign_conditions = [
                    "bta.building_osm_id = ANY(%s)",
                    "gr.plz = 'USER'",
                    "bta.grid_result_id != br.grid_result_id",
                    "NOT bta.building_osm_id LIKE '-%%'",
                ]
                reassign_params = [building_osm_ids]

                if payload.model_id is not None:
                    reassign_conditions.extend(["bta.model_id = %s", "gr.model_id = %s"])
                    reassign_params.extend([payload.model_id, payload.model_id])
                elif payload.draft_id:
                    reassign_conditions.extend(["bta.draft_id = %s", "gr.draft_id = %s"])
                    reassign_params.extend([payload.draft_id, payload.draft_id])

                reassign_conditions.extend(["bta.user_id = %s", "gr.user_id = %s"])
                reassign_params.extend([payload.user_id, payload.user_id])

                reassign_sql = f"""
                    SELECT bta.building_osm_id AS osm_id,
                           bta.grid_result_id AS new_grid_id,
                           br.grid_result_id AS original_grid_id,
                           ST_Transform(br.geom, 4326) AS geom
                    FROM building_transformer_assignments bta
                    JOIN buildings_result br
                      ON br.version_id = %s
                     AND br.osm_id = bta.building_osm_id
                    JOIN grid_result gr ON bta.grid_result_id = gr.grid_result_id
                    WHERE {' AND '.join(reassign_conditions)};
                """
                dbc.cur.execute(reassign_sql, tuple([VERSION_ID] + reassign_params))
                reassign_rows = dbc.cur.fetchall()

                if reassign_rows:
                    from shapely import wkb as wkb_loader
                    reassigned_data = []
                    for row in reassign_rows:
                        geom = wkb_loader.loads(row[3], hex=True) if row[3] else None
                        reassigned_data.append({
                            'osm_id': row[0],
                            'new_grid_id': row[1],
                            'original_grid_id': row[2],
                            'geom': geom
                        })
                    reassigned_buildings = gpd.GeoDataFrame(reassigned_data, geometry='geom', crs='EPSG:4326')
                else:
                    reassigned_buildings = gpd.GeoDataFrame()

                print(f"[Synthetic Lines] Found {len(reassigned_buildings)} reassigned regular buildings")

                if not reassigned_buildings.empty:
                    synthetic_reassign_lines = []
                    line_id_start = lines['line_id'].max() + 1 if not lines.empty and 'line_id' in lines.columns else 200000

                    for idx, bldg in reassigned_buildings.iterrows():
                        new_grid_id = bldg.get('new_grid_id')
                        bldg_osm_id = bldg.get('osm_id')

                        # Find the new transformer
                        trafo = transformers[transformers['grid_result_id'] == new_grid_id]
                        if trafo.empty:
                            print(f"  Skipping reassigned building {bldg_osm_id}: no transformer for grid {new_grid_id}")
                            continue

                        trafo_geom = trafo.iloc[0]['geom']
                        bldg_geom = bldg['geom']

                        if trafo_geom and bldg_geom:
                            if trafo_geom.geom_type == 'Point':
                                t_x, t_y = trafo_geom.x, trafo_geom.y
                            else:
                                t_x, t_y = trafo_geom.centroid.x, trafo_geom.centroid.y

                            if bldg_geom.geom_type == 'Point':
                                b_x, b_y = bldg_geom.x, bldg_geom.y
                            else:
                                b_x, b_y = bldg_geom.centroid.x, bldg_geom.centroid.y

                            # Try to route along roads
                            line_geom = None
                            length_km = None
                            try:
                                dbc.cur.execute("""
                                    WITH trafo_pt AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) as geom),
                                    bldg_pt AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) as geom),
                                    nearest_road AS (
                                        SELECT w.geom, ST_ClosestPoint(w.geom, t.geom) as t_pt, ST_ClosestPoint(w.geom, b.geom) as b_pt
                                        FROM ways w, trafo_pt t, bldg_pt b
                                        ORDER BY w.geom <-> b.geom LIMIT 1
                                    ),
                                    connection AS (
                                        SELECT ST_MakeLine(ARRAY[
                                            (SELECT geom FROM trafo_pt),
                                            t_pt, b_pt,
                                            (SELECT geom FROM bldg_pt)
                                        ]) as geom FROM nearest_road
                                    )
                                    SELECT ST_Transform(geom, 4326), ST_Length(geom)/1000.0 FROM connection WHERE geom IS NOT NULL;
                                """, (t_x, t_y, b_x, b_y))
                                result = dbc.cur.fetchone()
                                if result and result[0]:
                                    line_geom = wkb.loads(result[0], hex=True)
                                    length_km = float(result[1]) if result[1] else 0.01
                            except Exception:
                                # Fallback to straight line
                                line_geom = LineString([(t_x, t_y), (b_x, b_y)])
                                length_km = line_geom.length * 111  # Approximate km

                            if line_geom:
                                synthetic_reassign_lines.append({
                                    'line_id': line_id_start,
                                    'grid_result_id': new_grid_id,
                                    'line_name': f"reassign_{bldg_osm_id}",
                                    'cable_type': 'NAYY 4x150',
                                    'length_km': round(length_km, 4),
                                    'geom': line_geom
                                })
                                line_id_start += 1

                    if synthetic_reassign_lines:
                        reassign_gdf = gpd.GeoDataFrame(synthetic_reassign_lines, geometry='geom', crs='EPSG:4326')
                        lines = pd.concat([lines, reassign_gdf], ignore_index=True)
                        print(f"Generated {len(synthetic_reassign_lines)} synthetic LV lines for reassigned buildings")

        except Exception as e:
            print(f"Error generating synthetic LV lines for reassigned buildings: {e}")
            traceback.print_exc()

        stages.mark("synth_reassigned")
        # 4. MV Lines
        try:
            mv_lines = generate_synthetic_mv_lines(transformers, db_conn=dbc.conn)
            print(f"Generated {len(mv_lines)} synthetic MV lines connecting {len(transformers)} transformers (routed via roads)")
        except Exception as mv_err:
            print(f"Synthetic MV line generation failed: {mv_err}")
            traceback.print_exc()
            mv_lines = gpd.GeoDataFrame()

        # 5. Optional state boundary (local DB bbox, no external network call)
        boundary_data = None
        try:
            if payload.include_boundary and not buildings.empty:
                # Calculate centroid of all buildings
                centroids = buildings.geometry.centroid
                avg_lon = centroids.x.mean()
                avg_lat = centroids.y.mean()
                print(f"[Boundary] Looking up local state boundary for centroid: lat={avg_lat}, lon={avg_lon}")
                boundary_data = _fetch_state_boundary_bbox_for_centroid(dbc, avg_lon, avg_lat)
                if boundary_data:
                    print(f"[Boundary] Matched local state: {boundary_data['region'].get('name')}")
                else:
                    print("[Boundary] No local state boundary match for centroid")
        except Exception as boundary_err:
            print(f"[Boundary] Failed to fetch boundary: {boundary_err}")

        response_data = {
            "status": "success",
            "buildings": json.loads(buildings.to_json()),
            "transformers": json.loads(transformers.to_json()) if not transformers.empty else {
                "type": "FeatureCollection",
                "features": [],
            },
            "lines": json.loads(lines.to_json()) if not lines.empty else {
                "type": "FeatureCollection",
                "features": [],
            },
            "mv_lines": json.loads(mv_lines.to_json()) if not mv_lines.empty else {
                "type": "FeatureCollection",
                "features": [],
            },
            "grids": grids_info,
        }
        
        # Include boundary if available
        if boundary_data:
            response_data["boundary"] = boundary_data
        stages.mark("serialize")
        total_elapsed = time.perf_counter() - request_started
        print(f"[Timing] /generate-grid completed in {total_elapsed:.2f}s")
        print(f"[Timing] stages: {stages.summary()}")
        return response_data

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if dbc is not None:
            dbc.close()


@router.post("/grid-statistics")
async def get_grid_statistics(payload: GridStatisticsRequest):
    """Get detailed statistics for specified grid result IDs"""
    dbc = None
    try:
        dbc = DatabaseClient()
        grid_ids = payload.grid_result_ids

        if not grid_ids:
            return {"status": "success", "statistics": {}}

        # Building statistics
        buildings_stats_query = """
            SELECT
                COUNT(*) as building_count,
                SUM(area) as total_area_m2,
                AVG(area) as avg_area_m2,
                SUM(peak_load_in_kw) as total_peak_load_kw,
                AVG(peak_load_in_kw) as avg_peak_load_kw,
                COUNT(DISTINCT type) as building_type_count
            FROM buildings_result
            WHERE grid_result_id = ANY(%s);
        """
        dbc.cur.execute(buildings_stats_query, (grid_ids,))
        bldg_row = dbc.cur.fetchone()

        # Building type breakdown
        building_types_query = """
            SELECT type, COUNT(*) as count, SUM(peak_load_in_kw) as total_peak_kw
            FROM buildings_result
            WHERE grid_result_id = ANY(%s)
            GROUP BY type
            ORDER BY count DESC;
        """
        dbc.cur.execute(building_types_query, (grid_ids,))
        type_rows = dbc.cur.fetchall()

        # Transformer statistics
        transformer_stats_query = """
            SELECT
                COUNT(*) as transformer_count,
                SUM(gr.transformer_rated_power) as total_capacity_kva,
                AVG(gr.transformer_rated_power) as avg_capacity_kva,
                MIN(gr.transformer_rated_power) as min_capacity_kva,
                MAX(gr.transformer_rated_power) as max_capacity_kva
            FROM transformer_positions tp
            JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
            WHERE tp.grid_result_id = ANY(%s);
        """
        dbc.cur.execute(transformer_stats_query, (grid_ids,))
        trafo_row = dbc.cur.fetchone()

        # Get transformer cost lookup
        trafo_cost_query = """
            SELECT
                s_max_kva,
                COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur) AS effective_cost_eur
            FROM equipment_data
            WHERE typ = 'Transformer' AND s_max_kva IS NOT NULL;
        """
        dbc.cur.execute(trafo_cost_query)
        trafo_cost_lookup = {int(row[0]): float(row[1]) for row in dbc.cur.fetchall()}

        # Get individual transformer sizes
        trafo_sizes_query = """
            SELECT gr.transformer_rated_power
            FROM transformer_positions tp
            JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
            WHERE tp.grid_result_id = ANY(%s);
        """
        dbc.cur.execute(trafo_sizes_query, (grid_ids,))
        trafo_sizes = [row[0] for row in dbc.cur.fetchall()]

        # Line/Cable statistics
        lines_stats_query = """
            SELECT
                COUNT(*) as line_count,
                SUM(length_km) as total_length_km,
                AVG(length_km) as avg_length_km,
                COUNT(DISTINCT std_type) as cable_type_count
            FROM lines_result
            WHERE grid_result_id = ANY(%s);
        """
        dbc.cur.execute(lines_stats_query, (grid_ids,))
        lines_row = dbc.cur.fetchone()

        # Cable type breakdown
        cable_types_query = """
            SELECT std_type, COUNT(*) as count, SUM(length_km) as total_length_km
            FROM lines_result
            WHERE grid_result_id = ANY(%s)
            GROUP BY std_type
            ORDER BY total_length_km DESC;
        """
        dbc.cur.execute(cable_types_query, (grid_ids,))
        cable_rows = dbc.cur.fetchall()

        # Calculate costs
        total_cable_cost = 0
        cable_breakdown = []
        for row in cable_rows:
            cable_type = row[0]
            length_km = float(row[2] or 0)
            cost_per_m = CABLE_COST_DICT.get(cable_type, 20)
            cost = length_km * 1000 * cost_per_m
            total_cable_cost += cost
            cable_breakdown.append({
                "type": cable_type,
                "count": row[1],
                "length_km": round(length_km, 3),
                "cost_eur": round(cost, 2)
            })

        # Calculate transformer cost
        transformer_cost = 0
        transformer_cost_breakdown = []
        for size in trafo_sizes:
            size_int = int(size) if size else 0
            if size_int in trafo_cost_lookup:
                cost = trafo_cost_lookup[size_int]
            else:
                available_sizes = sorted(trafo_cost_lookup.keys())
                nearest_size = min(available_sizes, key=lambda x: abs(x - size_int)) if available_sizes else None
                cost = trafo_cost_lookup.get(nearest_size, 0) if nearest_size else 0
            transformer_cost += cost
            transformer_cost_breakdown.append({"size_kva": size_int, "cost_eur": cost})

        # Calculate simultaneity factor adjusted load
        total_peak_load = bldg_row[3] if bldg_row[3] else 0
        building_count = bldg_row[0] if bldg_row[0] else 0
        if building_count > 0:
            sim_factor = 0.07 + (1 - 0.07) * (building_count ** (-0.75))
            simultaneous_load_kw = float(total_peak_load) * sim_factor
        else:
            simultaneous_load_kw = 0

        statistics = {
            "buildings": {
                "count": bldg_row[0] or 0,
                "total_area_m2": round(bldg_row[1] or 0, 2),
                "avg_area_m2": round(bldg_row[2] or 0, 2),
                "total_peak_load_kw": round(bldg_row[3] or 0, 2),
                "avg_peak_load_kw": round(bldg_row[4] or 0, 2),
                "simultaneous_load_kw": round(simultaneous_load_kw, 2),
                "building_types": [
                    {"type": r[0], "count": r[1], "total_peak_kw": round(r[2] or 0, 2)}
                    for r in type_rows
                ]
            },
            "transformers": {
                "count": trafo_row[0] or 0,
                "total_capacity_kva": round(trafo_row[1] or 0, 2),
                "avg_capacity_kva": round(trafo_row[2] or 0, 2),
                "min_capacity_kva": trafo_row[3],
                "max_capacity_kva": trafo_row[4],
                "utilization_percent": round((simultaneous_load_kw / float(trafo_row[1] or 1)) * 100, 1) if trafo_row[1] else 0
            },
            "cables": {
                "count": lines_row[0] or 0,
                "total_length_km": round(lines_row[1] or 0, 3),
                "avg_length_km": round(lines_row[2] or 0, 4),
                "cable_types": cable_breakdown
            },
            "costs": {
                "cable_cost_eur": round(total_cable_cost, 2),
                "transformer_cost_eur": round(transformer_cost, 2),
                "transformer_cost_breakdown": transformer_cost_breakdown,
                "total_estimated_cost_eur": round(total_cable_cost + transformer_cost, 2)
            },
            "voltage": {
                "nominal_voltage_v": VN,
                "voltage_band_low": V_BAND_LOW,
                "voltage_band_high": V_BAND_HIGH
            }
        }

        return {
            "status": "success",
            "grid_result_ids": grid_ids,
            "statistics": statistics
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if dbc is not None:
            dbc.close()
