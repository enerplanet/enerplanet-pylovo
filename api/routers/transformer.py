"""Transformer management endpoints."""
import json
import math
import time
import traceback

import geopandas as gpd
from fastapi import APIRouter, HTTPException

from models import AddTransformerRequest, FinalizeTransformersRequest
from src.database.database_client import DatabaseClient
from src.database.connection_pool import get_connection

router = APIRouter(tags=["transformer"])

VERSION_ID = "1"
FULL_WAYS_SQL = (
    "SELECT way_id AS id, source, target, cost, reverse_cost FROM ways WHERE cost > 0"
)


def _corridor_ways_sql(x1: float, y1: float, x2: float, y2: float,
                       min_buffer_m: float = 2000.0) -> str:
    """Edge SQL for pgr_dijkstra limited to ways near the (x1,y1)-(x2,y2) line."""
    buffer_m = max(min_buffer_m, math.hypot(x2 - x1, y2 - y1))
    return (
        f"{FULL_WAYS_SQL} AND ST_DWithin(geom, ST_SetSRID(ST_MakeLine("
        f"ST_MakePoint({x1}, {y1}), ST_MakePoint({x2}, {y2})), 3035), {buffer_m})"
    )


def _routing_graphs(x1: float, y1: float, x2: float, y2: float) -> list:
    """Corridor graph first, full graph as fallback when no route is found."""
    return [_corridor_ways_sql(x1, y1, x2, y2), FULL_WAYS_SQL]


def _require_transformer_schema(cur) -> None:
    """Verify the transformer scope schema."""
    cur.execute(
        """
        SELECT
            (
                SELECT COUNT(DISTINCT column_name) = 3
                FROM information_schema.columns
                WHERE table_schema = ANY(current_schemas(FALSE))
                  AND table_name = 'grid_result'
                  AND column_name = ANY(ARRAY['user_id', 'model_id', 'draft_id'])
            )
            AND to_regclass('building_transformer_assignments') IS NOT NULL;
        """
    )
    if not cur.fetchone()[0]:
        raise HTTPException(
            status_code=503,
            detail="Transformer database schema is not ready",
        )


@router.post("/add-transformer")
async def add_transformer(payload: AddTransformerRequest):
    """
    Add a new transformer at a specified location and reassign nearby buildings.

    This creates a new grid with:
    1. A new transformer at the specified coordinates
    2. Buildings within reassign_radius_m that are closer to this transformer than their current one
    3. New cable connections from reassigned buildings to the new transformer

    Returns updated grid data including the new transformer and reassigned buildings.
    """
    dbc = None
    try:
        if len(payload.coordinates) != 2:
            raise HTTPException(status_code=400, detail="Coordinates must be [longitude, latitude]")
        if not payload.user_id:
            raise HTTPException(status_code=400, detail="user_id is required")

        lon, lat = payload.coordinates
        new_grid_id = None
        reassigned_buildings = []

        with get_connection() as conn:
            with conn.cursor() as cur:
                # DDL must never run in a request. Even IF NOT EXISTS requires
                # an AccessExclusiveLock and can queue the whole API behind a
                # long-running read transaction.
                cur.execute("SET LOCAL lock_timeout = '5s';")
                _require_transformer_schema(cur)

                # Generate unique OSM ID for the new transformer
                new_osm_id = f"user/{int(time.time() * 1000)}"

                # 1. Insert into transformers table first
                cur.execute("""
                    INSERT INTO transformers (osm_id, country_code, type, transformer_rated_power, geom_type, within_shopping, geom)
                    VALUES (%s, 'DE', 'UserPlaced', %s, 'user_placed', FALSE,
                            ST_Multi(ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035)))
                    RETURNING osm_id;
                """, (new_osm_id, int(payload.kva), lon, lat))
                new_osm_id = cur.fetchone()[0]

                # 2. Check if we need a postcode_result entry for USER plz
                cur.execute("""
                    SELECT 1 FROM postcode_result WHERE version_id = %s AND postcode_result_plz = 'USER' AND country_code = 'DE';
                """, (VERSION_ID,))

                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO postcode (plz, country_code, note)
                        VALUES ('USER', 'DE', 'User-placed transformers')
                        ON CONFLICT (plz, country_code) DO NOTHING;
                    """)
                    cur.execute("""
                        INSERT INTO postcode_result (version_id, postcode_result_plz, country_code)
                        VALUES (%s, 'USER', 'DE')
                        ON CONFLICT (version_id, postcode_result_plz, country_code) DO NOTHING;
                    """, (VERSION_ID,))

                # 3. Get the next available bcid for USER plz
                cur.execute("""
                    SELECT COALESCE(MAX(bcid), 0) + 1 FROM grid_result
                    WHERE version_id = %s AND plz = 'USER' AND country_code = 'DE';
                """, (VERSION_ID,))
                new_bcid = cur.fetchone()[0]

                # 4. Create a new grid_result entry
                cur.execute("""
                    INSERT INTO grid_result (version_id, plz, country_code, kcid, bcid, transformer_rated_power, user_id, model_id, draft_id)
                    VALUES (%s, 'USER', 'DE', 0, %s, %s, %s, %s, %s)
                    RETURNING grid_result_id;
                """, (VERSION_ID, new_bcid, int(payload.kva), payload.user_id, payload.model_id, payload.draft_id))
                new_grid_id = cur.fetchone()[0]

                # 5. Create the transformer_positions entry
                cur.execute("""
                    INSERT INTO transformer_positions (grid_result_id, version_id, osm_id, geom, comment)
                    VALUES (%s, %s, %s, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035), 'UserPlaced');
                """, (new_grid_id, VERSION_ID, new_osm_id, lon, lat))

                # 6. Find and reassign buildings within radius
                if payload.grid_result_ids and payload.reassign_radius_m > 0:
                    cur.execute("""
                        WITH new_trafo AS (
                            SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) AS geom
                        ),
                        buildings_in_range AS (
                            SELECT br.osm_id,
                                   br.grid_result_id AS old_grid_id,
                                   ST_Distance(br.geom, nt.geom) AS dist_to_new,
                                   (SELECT ST_Distance(br.geom, tp.geom)
                                    FROM transformer_positions tp
                                    WHERE tp.grid_result_id = br.grid_result_id) AS dist_to_current
                            FROM buildings_result br, new_trafo nt
                            WHERE br.version_id = %s
                              AND br.grid_result_id = ANY(%s)
                              AND ST_DWithin(br.geom, nt.geom, %s)
                        )
                        SELECT osm_id, old_grid_id, dist_to_new, dist_to_current
                        FROM buildings_in_range
                        WHERE dist_to_new < COALESCE(dist_to_current, dist_to_new + 1);
                    """, (lon, lat, VERSION_ID, payload.grid_result_ids, payload.reassign_radius_m))

                    buildings_to_reassign = cur.fetchall()

                    if buildings_to_reassign:
                        osm_ids_to_reassign = [row[0] for row in buildings_to_reassign]

                        # Insert assignments into the new table (model-specific, not global)
                        for osm_id in osm_ids_to_reassign:
                            cur.execute("""
                                INSERT INTO building_transformer_assignments 
                                    (building_osm_id, grid_result_id, user_id, model_id, draft_id, version_id)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT DO NOTHING;
                            """, (osm_id, new_grid_id, payload.user_id, payload.model_id, payload.draft_id, VERSION_ID))

                        reassigned_buildings = osm_ids_to_reassign

                        # Create cable lines for reassigned buildings
                        cur.execute("""
                            SELECT br.osm_id,
                                   ST_X(ST_Centroid(br.geom)) as bldg_x,
                                   ST_Y(ST_Centroid(br.geom)) as bldg_y,
                                   br.peak_load_in_kw
                            FROM buildings_result br
                            WHERE br.version_id = %s AND br.osm_id = ANY(%s);
                        """, (VERSION_ID, osm_ids_to_reassign))
                        building_coords = cur.fetchall()

                        cur.execute("""
                            SELECT ST_X(geom) as trafo_x, ST_Y(geom) as trafo_y
                            FROM transformer_positions
                            WHERE grid_result_id = %s;
                        """, (new_grid_id,))
                        trafo_pos = cur.fetchone()
                        trafo_x, trafo_y = trafo_pos[0], trafo_pos[1]

                        bus_counter = 1
                        for bldg_osm_id, bldg_x, bldg_y, peak_load_kw in building_coords:
                            cur.execute("""
                                SELECT
                                    (SELECT source FROM ways ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 3035) LIMIT 1) as start_v,
                                    (SELECT source FROM ways ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 3035) LIMIT 1) as end_v;
                            """, (trafo_x, trafo_y, bldg_x, bldg_y))
                            vertices = cur.fetchone()
                            start_v, end_v = vertices[0], vertices[1] if vertices else (None, None)

                            line_geom_wkt = None
                            length_km = 0.01

                            if start_v and end_v:
                                if start_v == end_v:
                                    cur.execute("""
                                        WITH trafo_pt AS (
                                            SELECT ST_SetSRID(ST_MakePoint(%s, %s), 3035) as geom
                                        ),
                                        bldg_pt AS (
                                            SELECT ST_SetSRID(ST_MakePoint(%s, %s), 3035) as geom
                                        ),
                                        nearest_way AS (
                                            SELECT w.geom, w.way_id
                                            FROM ways w, bldg_pt b
                                            ORDER BY w.geom <-> b.geom
                                            LIMIT 1
                                        ),
                                        connection_points AS (
                                            SELECT
                                                t.geom as trafo_geom,
                                                b.geom as bldg_geom,
                                                ST_ClosestPoint(nw.geom, t.geom) as trafo_on_road,
                                                ST_ClosestPoint(nw.geom, b.geom) as bldg_on_road,
                                                nw.geom as road_geom,
                                                ST_LineLocatePoint(nw.geom, t.geom) as trafo_frac,
                                                ST_LineLocatePoint(nw.geom, b.geom) as bldg_frac
                                            FROM nearest_way nw, trafo_pt t, bldg_pt b
                                        ),
                                        full_line AS (
                                            SELECT ST_LineMerge(ST_Union(ARRAY[
                                                ST_MakeLine(trafo_geom, trafo_on_road),
                                                ST_LineSubstring(
                                                    road_geom,
                                                    LEAST(trafo_frac, bldg_frac),
                                                    GREATEST(trafo_frac, bldg_frac)
                                                ),
                                                ST_MakeLine(bldg_on_road, bldg_geom)
                                            ])) as geom
                                            FROM connection_points
                                        )
                                        SELECT ST_AsText(geom), ST_Length(geom) / 1000.0 as length_km
                                        FROM full_line
                                        WHERE geom IS NOT NULL;
                                    """, (trafo_x, trafo_y, bldg_x, bldg_y))
                                    result = cur.fetchone()
                                    if result and result[0]:
                                        line_geom_wkt = result[0]
                                        length_km = float(result[1]) if result[1] else 0.01
                                else:
                                    result = None
                                    for ways_sql in _routing_graphs(trafo_x, trafo_y, bldg_x, bldg_y):
                                        cur.execute(f"""
                                            WITH route AS (
                                                SELECT ST_LineMerge(ST_Union(w.geom ORDER BY r.path_seq)) AS route_geom
                                                FROM pgr_dijkstra(
                                                    '{ways_sql.replace("'", "''")}',
                                                    %s, %s, directed := false
                                                ) r
                                                JOIN ways w ON r.edge = w.way_id
                                                WHERE r.edge > 0
                                            ),
                                            trafo_pt AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 3035) as geom),
                                            bldg_pt AS (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 3035) as geom),
                                            full_connection AS (
                                                SELECT ST_LineMerge(ST_Union(ARRAY[
                                                    ST_MakeLine(t.geom, ST_StartPoint(r.route_geom)),
                                                    r.route_geom,
                                                    ST_MakeLine(ST_EndPoint(r.route_geom), b.geom)
                                                ])) as geom
                                                FROM route r, trafo_pt t, bldg_pt b
                                                WHERE r.route_geom IS NOT NULL
                                            )
                                            SELECT ST_AsText(geom), ST_Length(geom) / 1000.0 as length_km
                                            FROM full_connection
                                            WHERE geom IS NOT NULL;
                                        """, (start_v, end_v, trafo_x, trafo_y, bldg_x, bldg_y))
                                        result = cur.fetchone()
                                        if result and result[0]:
                                            break
                                    if result and result[0]:
                                        line_geom_wkt = result[0]
                                        length_km = float(result[1]) if result[1] else 0.01

                            if not line_geom_wkt:
                                line_geom_wkt = f"LINESTRING({trafo_x} {trafo_y}, {bldg_x} {bldg_y})"
                                import math as m
                                length_km = max(m.sqrt((bldg_x - trafo_x)**2 + (bldg_y - trafo_y)**2) / 1000.0, 0.001)

                            cur.execute("""
                                INSERT INTO lines_result (grid_result_id, line_name, std_type,
                                                         from_bus, to_bus, length_km, geom)
                                SELECT %s, %s, %s, %s, %s, %s,
                                       (ST_Dump(ST_GeomFromText(%s, 3035))).geom
                                ON CONFLICT DO NOTHING;
                            """, (new_grid_id, f"L_user_{bldg_osm_id}"[:50],
                                  "NAYY 4x150 SE", 0, bus_counter, length_km, line_geom_wkt))
                            bus_counter += 1

                conn.commit()

        # Fetch the updated data
        dbc = DatabaseClient()

        new_transformer = gpd.GeoDataFrame.from_postgis(
            """
            SELECT tp.osm_id,
                   tp.grid_result_id,
                   gr.transformer_rated_power AS rated_power_kva,
                   ST_Transform(tp.geom, 4326) AS geom
            FROM transformer_positions tp
            JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
            WHERE tp.version_id = %s AND tp.grid_result_id = %s;
            """,
            con=dbc.conn,
            params=[VERSION_ID, new_grid_id],
            geom_col="geom"
        )

        reassigned_buildings_gdf = gpd.GeoDataFrame()
        if reassigned_buildings:
            reassigned_buildings_gdf = gpd.GeoDataFrame.from_postgis(
                """
                SELECT br.osm_id,
                       br.grid_result_id,
                       ST_Transform(br.geom, 4326) AS geom,
                       br.type,
                       br.f_class,
                       br.area,
                       br.peak_load_in_kw
                FROM buildings_result br
                WHERE br.version_id = %s AND br.osm_id = ANY(%s);
                """,
                con=dbc.conn,
                params=[VERSION_ID, reassigned_buildings],
                geom_col="geom"
            )

        new_lines_gdf = gpd.GeoDataFrame()
        if new_grid_id:
            new_lines_gdf = gpd.GeoDataFrame.from_postgis(
                """
                SELECT lr.lines_result_id as line_id,
                       lr.grid_result_id,
                       lr.line_name,
                       lr.std_type as cable_type,
                       lr.length_km,
                       ST_Transform(lr.geom, 4326) AS geom
                FROM lines_result lr
                WHERE lr.grid_result_id = %s;
                """,
                con=dbc.conn,
                params=[new_grid_id],
                geom_col="geom"
            )

        transformer_geojson = json.loads(new_transformer.to_json()) if not new_transformer.empty else {"type": "FeatureCollection", "features": []}
        buildings_geojson = json.loads(reassigned_buildings_gdf.to_json()) if not reassigned_buildings_gdf.empty else {"type": "FeatureCollection", "features": []}
        lines_geojson = json.loads(new_lines_gdf.to_json()) if not new_lines_gdf.empty else {"type": "FeatureCollection", "features": []}

        return {
            "status": "success",
            "new_grid_id": new_grid_id,
            "transformer": transformer_geojson,
            "reassigned_buildings": buildings_geojson,
            "lines": lines_geojson,
            "reassigned_count": len(reassigned_buildings),
            "message": f"Created transformer at [{lon:.6f}, {lat:.6f}] with {payload.kva} kVA. Reassigned {len(reassigned_buildings)} buildings with {len(new_lines_gdf)} cable connections."
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if dbc is not None:
            dbc.close()


@router.post("/assign-building")
async def assign_building_to_transformer(payload: dict):
    """
    Assign a building to a specific transformer (grid_result_id).
    Creates a cable line from the building to the transformer.
    Uses building_transformer_assignments table for model-specific assignments.
    """
    try:
        building_osm_id = payload.get("building_osm_id")
        target_grid_id = payload.get("target_grid_id")
        user_id = payload.get("user_id")
        model_id = payload.get("model_id")
        draft_id = payload.get("draft_id")

        if not building_osm_id:
            raise HTTPException(status_code=400, detail="building_osm_id is required")
        if not target_grid_id:
            raise HTTPException(status_code=400, detail="target_grid_id is required")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")
        if model_id is None and not draft_id:
            raise HTTPException(status_code=400, detail="Either model_id or draft_id is required")

        with get_connection() as conn:
            with conn.cursor() as cur:
                _require_transformer_schema(cur)

                cur.execute("""
                    SELECT br.grid_result_id,
                           ST_X(ST_Centroid(br.geom)) as bldg_x,
                           ST_Y(ST_Centroid(br.geom)) as bldg_y,
                           br.peak_load_in_kw
                    FROM buildings_result br
                    WHERE br.version_id = %s AND br.osm_id = %s;
                """, (VERSION_ID, str(building_osm_id)))

                building = cur.fetchone()
                if not building:
                    raise HTTPException(status_code=404, detail="Building not found")

                old_grid_id, bldg_x, bldg_y, peak_load_kw = building

                cur.execute("""
                    SELECT ST_X(tp.geom) as trafo_x,
                           ST_Y(tp.geom) as trafo_y,
                           gr.plz,
                           gr.user_id,
                           gr.model_id,
                           gr.draft_id
                    FROM transformer_positions tp
                    JOIN grid_result gr ON gr.grid_result_id = tp.grid_result_id
                    WHERE tp.grid_result_id = %s AND tp.version_id = %s;
                """, (target_grid_id, VERSION_ID))

                trafo = cur.fetchone()
                if not trafo:
                    raise HTTPException(status_code=404, detail="Target transformer not found")

                trafo_x, trafo_y, target_plz, target_user_id, target_model_id, target_draft_id = trafo

                # Only allow assigning to in-scope user transformers.
                if target_plz == "USER":
                    if target_user_id and target_user_id != user_id:
                        raise HTTPException(status_code=403, detail="Target transformer belongs to another user")
                    if model_id is not None and target_model_id != model_id:
                        raise HTTPException(status_code=403, detail="Target transformer is not in the current model scope")
                    if model_id is None and draft_id and target_draft_id != draft_id:
                        raise HTTPException(status_code=403, detail="Target transformer is not in the current draft scope")

                # Delete any existing assignment for this building in this model/draft context
                delete_conditions = ["building_osm_id = %s"]
                delete_params = [str(building_osm_id)]
                if model_id is not None:
                    delete_conditions.append("model_id = %s")
                    delete_params.append(model_id)
                elif draft_id:
                    delete_conditions.append("draft_id = %s")
                    delete_params.append(draft_id)
                delete_conditions.append("user_id = %s")
                delete_params.append(user_id)
                cur.execute(f"""
                    DELETE FROM building_transformer_assignments
                    WHERE {' AND '.join(delete_conditions)};
                """, delete_params)

                # Insert new assignment (model-specific, not modifying buildings_result)
                cur.execute("""
                    INSERT INTO building_transformer_assignments 
                        (building_osm_id, grid_result_id, user_id, model_id, draft_id, version_id)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (str(building_osm_id), target_grid_id, user_id, model_id, draft_id, VERSION_ID))

                # Delete old lines for this building in the target grid
                cur.execute("""
                    DELETE FROM lines_result
                    WHERE grid_result_id = %s AND line_name LIKE %s;
                """, (target_grid_id, f"%{building_osm_id}%"))

                # Also delete old lines from the previous grid (where building was before)
                if old_grid_id and old_grid_id != target_grid_id:
                    cur.execute("""
                        DELETE FROM lines_result
                        WHERE grid_result_id = %s AND line_name LIKE %s;
                    """, (old_grid_id, f"%{building_osm_id}%"))

                print(f"[Assign Building] Building coords: ({bldg_x}, {bldg_y}), Transformer coords: ({trafo_x}, {trafo_y})")

                # Find nearest road vertices for building and transformer
                # Also get the closest point on the road for proper connection
                cur.execute("""
                    SELECT source, 
                           ST_X(ST_ClosestPoint(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035))) AS closest_x,
                           ST_Y(ST_ClosestPoint(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035))) AS closest_y
                    FROM ways
                    WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035), 1000)
                    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                    LIMIT 1;
                """, (bldg_x, bldg_y, bldg_x, bldg_y, bldg_x, bldg_y, bldg_x, bldg_y))
                bldg_road = cur.fetchone()
                print(f"[Assign Building] Building nearest road: {bldg_road}")

                cur.execute("""
                    SELECT source,
                           ST_X(ST_ClosestPoint(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035))) AS closest_x,
                           ST_Y(ST_ClosestPoint(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035))) AS closest_y
                    FROM ways
                    WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 3035), 1000)
                    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                    LIMIT 1;
                """, (trafo_x, trafo_y, trafo_x, trafo_y, trafo_x, trafo_y, trafo_x, trafo_y))
                trafo_road = cur.fetchone()
                print(f"[Assign Building] Transformer nearest road: {trafo_road}")

                line_wkt = None
                length_km = None

                # Use pgRouting Dijkstra if both vertices found
                if bldg_road and trafo_road:
                    bldg_node_id = bldg_road[0]
                    bldg_road_x, bldg_road_y = bldg_road[1], bldg_road[2]
                    trafo_node_id = trafo_road[0]
                    trafo_road_x, trafo_road_y = trafo_road[1], trafo_road[2]

                    print(f"[Assign Building] Routing from vertex {bldg_node_id} to {trafo_node_id}")

                    # If same vertex, create direct path via road points (no routing needed)
                    if bldg_node_id == trafo_node_id:
                        print(f"[Assign Building] Same vertex - using direct path via road points")
                        # Create line: building -> road point (building) -> road point (transformer) -> transformer
                        cur.execute("""
                            SELECT ST_AsText(ST_MakeLine(ARRAY[
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                            ])) AS line_wkt,
                            ST_Length(ST_MakeLine(ARRAY[
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                            ])) / 1000.0 AS length_km;
                        """, (bldg_x, bldg_y, bldg_road_x, bldg_road_y, trafo_road_x, trafo_road_y, trafo_x, trafo_y,
                              bldg_x, bldg_y, bldg_road_x, bldg_road_y, trafo_road_x, trafo_road_y, trafo_x, trafo_y))
                        direct_result = cur.fetchone()
                        if direct_result and direct_result[0]:
                            line_wkt = direct_result[0]
                            length_km = direct_result[1] or 0.01
                            print(f"[Assign Building] Direct path via road, length: {length_km:.3f} km")
                    else:
                        # Get routed path geometry using pgr_dijkstra
                        route_result = None
                        for ways_sql in _routing_graphs(bldg_x, bldg_y, trafo_x, trafo_y):
                            cur.execute(f"""
                                WITH route AS (
                                    SELECT edge, path_seq
                                    FROM pgr_dijkstra(
                                        '{ways_sql.replace("'", "''")}',
                                        %s, %s, directed := false
                                    )
                                    WHERE edge != -1
                                )
                                SELECT 
                                    ST_AsText(ST_LineMerge(ST_Union(w.geom ORDER BY r.path_seq))) AS route_wkt,
                                    ST_Length(ST_LineMerge(ST_Union(w.geom ORDER BY r.path_seq))) / 1000.0 AS length_km
                                FROM route r
                                JOIN ways w ON r.edge = w.way_id;
                            """, (bldg_node_id, trafo_node_id))
                            route_result = cur.fetchone()
                            if route_result and route_result[0]:
                                break
                        print(f"[Assign Building] Route result: {route_result}")
                        
                        if route_result and route_result[0]:
                            # Build complete path: building -> road connection -> routed path -> road connection -> transformer
                            route_line = route_result[0]
                            
                            # Create complete line including building and transformer connections
                            # Use ST_LineMerge + ST_Collect to properly combine point-to-point lines with the routed path
                            cur.execute("""
                                WITH route_geom AS (
                                    SELECT ST_GeomFromText(%s, 3035) AS geom
                                ),
                                bldg_to_road AS (
                                    SELECT ST_MakeLine(
                                        ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                        ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                                    ) AS geom
                                ),
                                road_to_trafo AS (
                                    SELECT ST_MakeLine(
                                        ST_SetSRID(ST_MakePoint(%s, %s), 3035),
                                        ST_SetSRID(ST_MakePoint(%s, %s), 3035)
                                    ) AS geom
                                ),
                                complete_path AS (
                                    SELECT ST_LineMerge(ST_Collect(ARRAY[
                                        (SELECT geom FROM bldg_to_road),
                                        (SELECT geom FROM route_geom),
                                        (SELECT geom FROM road_to_trafo)
                                    ])) AS geom
                                ),
                                -- Force to single LINESTRING: if LineMerge returned MultiLineString, use ST_MakeLine with all points
                                final_geom AS (
                                    SELECT CASE 
                                        WHEN ST_GeometryType(geom) = 'ST_MultiLineString' 
                                        THEN (SELECT ST_MakeLine(pts.geom ORDER BY pts.path[1], pts.path[2])
                                              FROM ST_DumpPoints(geom) pts)
                                        ELSE geom 
                                    END AS geom
                                    FROM complete_path
                                )
                                SELECT ST_AsText(geom), ST_Length(geom) / 1000.0
                                FROM final_geom;
                            """, (route_line, bldg_x, bldg_y, bldg_road_x, bldg_road_y, 
                                  trafo_road_x, trafo_road_y, trafo_x, trafo_y))
                        
                        complete_result = cur.fetchone()
                        if complete_result and complete_result[0]:
                            line_wkt = complete_result[0]
                            length_km = complete_result[1] or 0.01
                            print(f"[Assign Building] Complete routed path, length: {length_km:.3f} km")
                        else:
                            # Fallback to just the route
                            line_wkt = route_line
                            length_km = route_result[1] or 0.01
                            print(f"[Assign Building] Using road route only, length: {length_km:.3f} km")

                # Fallback to straight line if routing failed
                if not line_wkt:
                    print(f"[Assign Building] Fallback to straight line")
                    line_wkt = f"LINESTRING({bldg_x} {bldg_y}, {trafo_x} {trafo_y})"
                    cur.execute("""
                        SELECT ST_Length(ST_GeomFromText(%s, 3035)) / 1000.0;
                    """, (line_wkt,))
                    length_km = cur.fetchone()[0] or 0.01

                load_kw = peak_load_kw or 5
                if load_kw < 10:
                    cable_type = "NYY_4_16"
                elif load_kw < 30:
                    cable_type = "NYY_4_35"
                elif load_kw < 60:
                    cable_type = "NYY_4_70"
                else:
                    cable_type = "NYY_4_120"

                line_name = f"L_assign_{building_osm_id}"[:50]

                cur.execute("""
                    INSERT INTO lines_result (grid_result_id, line_name, std_type,
                                              from_bus, to_bus, length_km, geom)
                    VALUES (%s, %s, %s, 0, 1, %s, ST_GeomFromText(%s, 3035));
                """, (target_grid_id, line_name, cable_type, length_km, line_wkt))

                conn.commit()

        return {
            "status": "success",
            "building_osm_id": building_osm_id,
            "old_grid_id": old_grid_id,
            "new_grid_id": target_grid_id,
            "message": f"Assigned building {building_osm_id} to transformer {target_grid_id}"
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete-transformer")
async def delete_transformer(payload: dict):
    """
    Delete a user-placed transformer in the current user/model scope.
    Building assignments are removed from building_transformer_assignments so buildings
    fall back to their original grid_result mapping.
    """
    try:
        grid_result_id = payload.get("grid_result_id")
        user_id = payload.get("user_id")
        model_id = payload.get("model_id")
        draft_id = payload.get("draft_id")

        if not grid_result_id:
            raise HTTPException(status_code=400, detail="grid_result_id is required")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")

        with get_connection() as conn:
            with conn.cursor() as cur:
                scope_conditions = ["gr.plz = 'USER'", "gr.user_id = %s"]
                scope_params = [user_id]
                if model_id is not None:
                    scope_conditions.append("gr.model_id = %s")
                    scope_params.append(model_id)
                elif draft_id:
                    scope_conditions.append("gr.draft_id = %s")
                    scope_params.append(draft_id)

                cur.execute("""
                    SELECT gr.plz, tp.osm_id FROM grid_result gr
                    JOIN transformer_positions tp ON gr.grid_result_id = tp.grid_result_id
                    WHERE gr.grid_result_id = %s AND gr.version_id = %s
                      AND """ + " AND ".join(scope_conditions) + ";",
                    [grid_result_id, VERSION_ID, *scope_params],
                )

                result = cur.fetchone()
                if not result:
                    raise HTTPException(status_code=404, detail="Transformer not found in current scope")

                plz, osm_id = result

                buildings_to_reassign = []
                cur.execute("SELECT to_regclass('public.building_transformer_assignments');")
                if cur.fetchone()[0] is not None:
                    assignment_conditions = ["grid_result_id = %s", "user_id = %s"]
                    assignment_params = [grid_result_id, user_id]
                    if model_id is not None:
                        assignment_conditions.append("model_id = %s")
                        assignment_params.append(model_id)
                    elif draft_id:
                        assignment_conditions.append("draft_id = %s")
                        assignment_params.append(draft_id)

                    cur.execute("""
                        SELECT building_osm_id
                        FROM building_transformer_assignments
                        WHERE """ + " AND ".join(assignment_conditions) + ";",
                        assignment_params,
                    )
                    buildings_to_reassign = [row[0] for row in cur.fetchall()]

                    cur.execute("""
                        DELETE FROM building_transformer_assignments
                        WHERE """ + " AND ".join(assignment_conditions) + ";",
                        assignment_params,
                    )

                cur.execute("DELETE FROM lines_result WHERE grid_result_id = %s;", (grid_result_id,))
                cur.execute("DELETE FROM transformer_positions WHERE grid_result_id = %s;", (grid_result_id,))
                cur.execute("""
                    DELETE FROM grid_result
                    WHERE grid_result_id = %s
                      AND version_id = %s
                      AND """ + " AND ".join(scope_conditions) + ";",
                    [grid_result_id, VERSION_ID, *scope_params],
                )

                if osm_id and osm_id.startswith("user/"):
                    cur.execute("DELETE FROM transformers WHERE osm_id = %s;", (osm_id,))

                conn.commit()

        return {
            "status": "success",
            "deleted_grid_id": grid_result_id,
            "reassigned_buildings_count": len(buildings_to_reassign),
            "message": f"Deleted transformer {grid_result_id} and reassigned {len(buildings_to_reassign)} buildings."
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/finalize-transformers")
async def finalize_transformers(payload: FinalizeTransformersRequest):
    """
    Finalize transformers after saving a model.
    Updates all transformers with the given draft_id to have the real model_id.
    Also updates building_transformer_assignments to use model_id instead of draft_id.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                where_conditions = ["draft_id = %s", "plz = 'USER'"]
                where_params = [payload.draft_id]
                if payload.user_id:
                    where_conditions.append("user_id = %s")
                    where_params.append(payload.user_id)

                # Update grid_result table
                cur.execute(
                    """
                    UPDATE grid_result
                    SET model_id = %s, draft_id = NULL
                    WHERE """
                    + " AND ".join(where_conditions)
                    + """
                    RETURNING grid_result_id;
                    """,
                    [payload.model_id, *where_params],
                )

                updated_ids = [row[0] for row in cur.fetchall()]

                # Also update building_transformer_assignments table
                bta_where_conditions = ["draft_id = %s"]
                bta_where_params = [payload.draft_id]
                if payload.user_id:
                    bta_where_conditions.append("user_id = %s")
                    bta_where_params.append(payload.user_id)

                cur.execute("SELECT to_regclass('public.building_transformer_assignments');")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        """
                        UPDATE building_transformer_assignments
                        SET model_id = %s, draft_id = NULL
                        WHERE """
                        + " AND ".join(bta_where_conditions)
                        + ";",
                        [payload.model_id, *bta_where_params],
                    )

                conn.commit()

                return {
                    "status": "success",
                    "updated_count": len(updated_ids),
                    "updated_grid_ids": updated_ids,
                    "message": f"Finalized {len(updated_ids)} transformers for model {payload.model_id}"
                }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/move-transformer")
async def move_transformer(payload: dict):
    """
    Move a user-placed transformer to a new location.
    Updates the transformer_positions table and regenerates lines for connected buildings.
    """
    try:
        grid_result_id = payload.get("grid_result_id")
        new_coordinates = payload.get("coordinates")
        user_id = payload.get("user_id")
        model_id = payload.get("model_id")
        draft_id = payload.get("draft_id")

        if not grid_result_id:
            raise HTTPException(status_code=400, detail="grid_result_id is required")
        if not new_coordinates or len(new_coordinates) != 2:
            raise HTTPException(status_code=400, detail="coordinates [lon, lat] is required")
        if not user_id:
            raise HTTPException(status_code=400, detail="user_id is required")

        lon, lat = new_coordinates

        with get_connection() as conn:
            with conn.cursor() as cur:
                scope_conditions = ["gr.plz = 'USER'", "gr.user_id = %s"]
                scope_params = [user_id]
                if model_id is not None:
                    scope_conditions.append("gr.model_id = %s")
                    scope_params.append(model_id)
                elif draft_id:
                    scope_conditions.append("gr.draft_id = %s")
                    scope_params.append(draft_id)

                cur.execute(
                    """
                    SELECT gr.plz, tp.osm_id FROM grid_result gr
                    JOIN transformer_positions tp ON gr.grid_result_id = tp.grid_result_id
                    WHERE gr.grid_result_id = %s AND gr.version_id = %s
                      AND """
                    + " AND ".join(scope_conditions)
                    + ";",
                    [grid_result_id, VERSION_ID, *scope_params],
                )

                result = cur.fetchone()
                if not result:
                    raise HTTPException(status_code=404, detail="Transformer not found in current scope")

                plz, osm_id = result

                if not osm_id or not osm_id.startswith("user/"):
                    raise HTTPException(status_code=403, detail="Only user-placed transformers can be moved")

                cur.execute("""
                    UPDATE transformer_positions
                    SET geom = ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035)
                    WHERE grid_result_id = %s AND version_id = %s;
                """, (lon, lat, grid_result_id, VERSION_ID))

                cur.execute("""
                    UPDATE transformers
                    SET geom = ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035)
                    WHERE osm_id = %s;
                """, (lon, lat, osm_id))

                cur.execute("DELETE FROM lines_result WHERE grid_result_id = %s;", (grid_result_id,))

                buildings = []
                cur.execute("SELECT to_regclass('public.building_transformer_assignments');")
                if cur.fetchone()[0] is not None:
                    assignment_conditions = ["bta.grid_result_id = %s", "bta.user_id = %s"]
                    assignment_params = [grid_result_id, user_id]
                    if model_id is not None:
                        assignment_conditions.append("bta.model_id = %s")
                        assignment_params.append(model_id)
                    elif draft_id:
                        assignment_conditions.append("bta.draft_id = %s")
                        assignment_params.append(draft_id)

                    cur.execute(
                        """
                        SELECT bta.building_osm_id,
                               ST_X(ST_Centroid(br.geom)) as bldg_x,
                               ST_Y(ST_Centroid(br.geom)) as bldg_y,
                               br.peak_load_in_kw
                        FROM building_transformer_assignments bta
                        JOIN buildings_result br ON br.osm_id = bta.building_osm_id
                        WHERE br.version_id = %s
                          AND """
                        + " AND ".join(assignment_conditions)
                        + ";",
                        [VERSION_ID, *assignment_params],
                    )
                    buildings = cur.fetchall()

                cur.execute("""
                    SELECT ST_X(geom) as trafo_x, ST_Y(geom) as trafo_y
                    FROM transformer_positions
                    WHERE grid_result_id = %s AND version_id = %s;
                """, (grid_result_id, VERSION_ID))
                trafo_pos = cur.fetchone()
                trafo_x, trafo_y = trafo_pos[0], trafo_pos[1]

                bus_counter = 1
                for bldg_osm_id, bldg_x, bldg_y, peak_load_kw in buildings:
                    line_wkt = f"LINESTRING({trafo_x} {trafo_y}, {bldg_x} {bldg_y})"

                    cur.execute("""
                        SELECT ST_Length(ST_GeomFromText(%s, 3035)) / 1000.0;
                    """, (line_wkt,))
                    length_km = cur.fetchone()[0] or 0.01

                    load_kw = peak_load_kw or 5
                    if load_kw < 10:
                        cable_type = "NYY_4_16"
                    elif load_kw < 30:
                        cable_type = "NYY_4_35"
                    elif load_kw < 60:
                        cable_type = "NYY_4_70"
                    else:
                        cable_type = "NYY_4_120"

                    line_name = f"L{grid_result_id}_{bus_counter}"

                    cur.execute("""
                        INSERT INTO lines_result (grid_result_id, line_name, std_type,
                                                  from_bus, to_bus, length_km, geom)
                        VALUES (%s, %s, %s, %s, %s, %s, ST_GeomFromText(%s, 3035));
                    """, (grid_result_id, line_name, cable_type, 0, bus_counter, length_km, line_wkt))

                    bus_counter += 1

                conn.commit()

                cur.execute("""
                    SELECT json_build_object(
                        'type', 'Feature',
                        'geometry', ST_AsGeoJSON(ST_Transform(tp.geom, 4326))::json,
                        'properties', json_build_object(
                            'osm_id', tp.osm_id,
                            'grid_result_id', tp.grid_result_id,
                            'rated_power_kva', gr.transformer_rated_power
                        )
                    )
                    FROM transformer_positions tp
                    JOIN grid_result gr ON tp.grid_result_id = gr.grid_result_id
                    WHERE tp.grid_result_id = %s;
                """, (grid_result_id,))
                transformer_feature = cur.fetchone()[0]

                cur.execute("""
                    SELECT json_build_object(
                        'type', 'FeatureCollection',
                        'features', COALESCE(json_agg(
                            json_build_object(
                                'type', 'Feature',
                                'geometry', ST_AsGeoJSON(ST_Transform(geom, 4326))::json,
                                'properties', json_build_object(
                                    'line_id', lines_result_id,
                                    'grid_result_id', grid_result_id,
                                    'line_name', line_name,
                                    'cable_type', std_type,
                                    'length_km', length_km
                                )
                            )
                        ), '[]'::json)
                    )
                    FROM lines_result
                    WHERE grid_result_id = %s;
                """, (grid_result_id,))
                lines_collection = cur.fetchone()[0]

        return {
            "status": "success",
            "grid_result_id": grid_result_id,
            "transformer": transformer_feature,
            "lines": lines_collection,
            "buildings_count": len(buildings),
            "message": f"Moved transformer {grid_result_id} to new location and regenerated {len(buildings)} lines."
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
