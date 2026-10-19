"""MV (Medium Voltage) lines generation service.

Generates synthetic MV lines connecting transformers using Minimum Spanning Tree,
routed along the road network using pgRouting.
"""
import numpy as np
import geopandas as gpd
from shapely.geometry import LineString
from scipy.spatial import Delaunay
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.sparse import csr_matrix


def _chain_route_coords(route_geom, start_point) -> list:
    """Return a routed path's coordinates as one ordered sequence.

    ST_LineMerge leaves a MultiLineString when the route's edges do not merge
    into one line. Its parts are chained by nearest endpoint, starting from the
    end nearest start_point and reversing parts as needed.
    """
    if route_geom.geom_type == 'LineString':
        return list(route_geom.coords)

    parts = [list(part.coords) for part in route_geom.geoms]

    def sq_dist(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    chained = []
    cursor = start_point
    while parts:
        best = min(range(len(parts)),
                   key=lambda k: min(sq_dist(cursor, parts[k][0]), sq_dist(cursor, parts[k][-1])))
        part = parts.pop(best)
        if sq_dist(cursor, part[-1]) < sq_dist(cursor, part[0]):
            part.reverse()
        chained.extend(part if not chained or chained[-1] != part[0] else part[1:])
        cursor = chained[-1]
    return chained


def generate_synthetic_mv_lines(transformers_gdf: gpd.GeoDataFrame, db_conn=None) -> gpd.GeoDataFrame:
    """
    Generate synthetic MV lines connecting transformers using Minimum Spanning Tree,
    routed along the road network.

    Optimized version that batches database queries for better performance.

    Args:
        transformers_gdf: GeoDataFrame with transformer positions (must have 'geom' column in WGS84)
        db_conn: Database connection for pgRouting queries

    Returns:
        GeoDataFrame with synthetic MV lines routed along roads
    """
    if transformers_gdf.empty or len(transformers_gdf) < 2:
        return gpd.GeoDataFrame(columns=['mv_line_id', 'from_transformer', 'to_transformer',
                                          'length_km', 'voltage_kv', 'cable_type', 'geom'],
                                 geometry='geom', crs='EPSG:4326')

    # Extract transformer coordinates and IDs
    coords = []
    transformer_ids = []

    geom_col = transformers_gdf.geometry.name if hasattr(transformers_gdf, 'geometry') else 'geom'

    for idx, row in transformers_gdf.iterrows():
        geom = row[geom_col] if geom_col in row.index else row.get('geom') or row.get('geometry')
        if geom is not None:
            if geom.geom_type == 'Point':
                coords.append([geom.x, geom.y])
            else:
                centroid = geom.centroid
                coords.append([centroid.x, centroid.y])
            transformer_ids.append(row.get('grid_result_id', idx))

    if len(coords) < 2:
        return gpd.GeoDataFrame(columns=['mv_line_id', 'from_transformer', 'to_transformer',
                                          'length_km', 'voltage_kv', 'cable_type', 'geom'],
                                 geometry='geom', crs='EPSG:4326')

    coords = np.array(coords)
    n_points = len(coords)

    # Build distance matrix for MST using vectorized operations
    if n_points <= 100:
        # Vectorized distance calculation
        lat_rad = np.radians(coords[:, 1])
        cos_lat = np.cos(lat_rad)

        dist_matrix = np.zeros((n_points, n_points))
        for i in range(n_points):
            dx = (coords[:, 0] - coords[i, 0]) * 111 * cos_lat[i]
            dy = (coords[:, 1] - coords[i, 1]) * 111
            dist_matrix[i, :] = np.sqrt(dx**2 + dy**2)
    else:
        try:
            tri = Delaunay(coords)
            dist_matrix = np.zeros((n_points, n_points))
            for simplex in tri.simplices:
                for i in range(3):
                    for j in range(i + 1, 3):
                        p1, p2 = simplex[i], simplex[j]
                        if dist_matrix[p1, p2] == 0:
                            dx = (coords[p2, 0] - coords[p1, 0]) * 111 * np.cos(np.radians(coords[p1, 1]))
                            dy = (coords[p2, 1] - coords[p1, 1]) * 111
                            dist = np.sqrt(dx**2 + dy**2)
                            dist_matrix[p1, p2] = dist
                            dist_matrix[p2, p1] = dist
        except Exception:
            lat_rad = np.radians(coords[:, 1])
            cos_lat = np.cos(lat_rad)
            dist_matrix = np.zeros((n_points, n_points))
            for i in range(n_points):
                dx = (coords[:, 0] - coords[i, 0]) * 111 * cos_lat[i]
                dy = (coords[:, 1] - coords[i, 1]) * 111
                dist_matrix[i, :] = np.sqrt(dx**2 + dy**2)

    # Compute Minimum Spanning Tree
    sparse_matrix = csr_matrix(dist_matrix)
    mst = minimum_spanning_tree(sparse_matrix)
    mst_array = mst.toarray()

    # Get MST edges
    mst_edges = []
    for i in range(n_points):
        for j in range(i + 1, n_points):
            if mst_array[i, j] > 0 or mst_array[j, i] > 0:
                mst_edges.append((i, j, max(mst_array[i, j], mst_array[j, i])))

    # If no database connection, use straight lines
    if db_conn is None:
        mv_lines = []
        for line_id, (i, j, dist) in enumerate(mst_edges, 1):
            mv_lines.append({
                'mv_line_id': line_id,
                'from_transformer': transformer_ids[i],
                'to_transformer': transformer_ids[j],
                'length_km': round(dist, 3),
                'voltage_kv': 20.0,
                'cable_type': 'NA2XS2Y 3x1x150',
                'geom': LineString([(coords[i, 0], coords[i, 1]), (coords[j, 0], coords[j, 1])])
            })
        return gpd.GeoDataFrame(mv_lines, geometry='geom', crs='EPSG:4326')

    cur = db_conn.cursor()

    # OPTIMIZATION: Batch get all nearest road vertices in ONE query
    # Use a distance threshold of 1000m to ensure we only pick nearby roads
    # If no road is within threshold, vertex will be NULL and we'll use straight line
    cur.execute(f"""
        WITH points AS (
            SELECT row_number() OVER () - 1 AS idx, geom
            FROM (VALUES {', '.join([f"(ST_Transform(ST_SetSRID(ST_MakePoint({c[0]}, {c[1]}), 4326), 3035))" for c in coords])}) AS t(geom)
        )
        SELECT p.idx,
               (SELECT source FROM ways
                WHERE ST_DWithin(ways.geom, p.geom, 1000)
                ORDER BY ways.geom <-> p.geom LIMIT 1) AS vertex,
               (SELECT ST_Distance(ways.geom, p.geom) FROM ways
                WHERE ST_DWithin(ways.geom, p.geom, 1000)
                ORDER BY ways.geom <-> p.geom LIMIT 1) AS distance_m
        FROM points p
        ORDER BY p.idx
    """)

    vertex_map = {}
    for row in cur.fetchall():
        idx = int(row[0])
        vertex = row[1]
        distance = row[2]
        vertex_map[idx] = vertex
        if vertex is None:
            print(f"[MV Lines] WARNING: No road found within 1km of transformer {idx}")
        else:
            print(f"[MV Lines] Transformer {idx} -> vertex {vertex} (distance: {distance:.0f}m)")

    # OPTIMIZATION: Batch route all MST edges in ONE query using pgr_dijkstra with multiple pairs
    route_results = {}
    if mst_edges:
        # Build array of (source, target) pairs
        pairs = [(vertex_map.get(i), vertex_map.get(j)) for i, j, _ in mst_edges]
        valid_pairs = [(s, t) for s, t in pairs if s is not None and t is not None]
        print(f"[MV Lines] MST edges: {len(mst_edges)}, Valid pairs for routing: {len(valid_pairs)}")

        if valid_pairs:
            try:
                # Optimization: Limit graph to a "corridor" around the Euclidean MST
                # This is much better than a Bounding Box for sparse/diagonal networks
                # as it avoids loading the entire rectangular area.

                # Construct WKT of the Euclidean MST lines
                lines_wkt_list = []
                for i, j, _ in mst_edges:
                    p1 = coords[i]
                    p2 = coords[j]
                    lines_wkt_list.append(f"({p1[0]} {p1[1]}, {p2[0]} {p2[1]})")

                if lines_wkt_list:
                    mst_wkt = f"MULTILINESTRING({', '.join(lines_wkt_list)})"

                    # Select ways within 5km buffer of the MST lines
                    # We project the WKT (4326) to 3035 to match the ways table
                    ways_sql = f"""
                        SELECT way_id AS id, source, target, cost, reverse_cost
                        FROM ways
                        WHERE ST_DWithin(
                            geom,
                            ST_Transform(ST_SetSRID(ST_GeomFromText('{mst_wkt}'), 4326), 3035),
                            5000
                        )
                    """
                else:
                    # Fallback if no edges (shouldn't happen given outer check)
                    min_x, min_y = np.min(coords, axis=0)
                    max_x, max_y = np.max(coords, axis=0)
                    ways_sql = f"SELECT way_id AS id, source, target, cost, reverse_cost FROM ways WHERE ST_DWithin(geom, ST_Transform(ST_SetSRID(ST_MakeEnvelope({min_x}, {min_y}, {max_x}, {max_y}), 4326), 3035), 5000)"

                # Use pgr_dijkstra with multiple source-target pairs (one-to-one)
                # Use the corridor-based query for better performance
                pairs_array = ", ".join([f"({s}, {t})" for s, t in valid_pairs])

                # Escape the ways_sql for use in pgr_dijkstra (double single quotes)
                ways_sql_escaped = ways_sql.replace("'", "''")

                print(f"[MV Lines] Routing {len(valid_pairs)} pairs using corridor-based query...")

                cur.execute(f"""
                    WITH pairs AS (
                        SELECT * FROM (VALUES {pairs_array}) AS t(source, target)
                    ),
                    routes AS (
                        SELECT
                            p.source AS start_vid,
                            p.target AS end_vid,
                            ST_LineMerge(ST_Union(w.geom ORDER BY route.path_seq)) AS route_geom
                        FROM pairs p
                        CROSS JOIN LATERAL (
                            SELECT * FROM pgr_dijkstra(
                                '{ways_sql_escaped}',
                                p.source, p.target, directed := false
                            )
                        ) AS route
                        JOIN ways w ON route.edge = w.way_id
                        WHERE route.edge > 0
                        GROUP BY p.source, p.target
                    )
                    SELECT
                        start_vid,
                        end_vid,
                        ST_Transform(route_geom, 4326) AS geom,
                        ST_Length(route_geom) / 1000.0 AS length_km
                    FROM routes
                """)

                # Build lookup of routed paths
                route_results = {}
                for row in cur.fetchall():
                    from shapely import wkb
                    route_results[(row[0], row[1])] = {
                        'geom': wkb.loads(row[2], hex=True) if row[2] else None,
                        'length_km': float(row[3]) if row[3] else None
                    }
                print(f"[MV Lines] Corridor query routed {len(route_results)}/{len(valid_pairs)} paths")

                # If corridor query found less than 50% of routes, try with full ways table
                if len(route_results) < len(valid_pairs) * 0.5:
                    print(f"[MV Lines] Retrying with full ways table...")
                    cur.execute(f"""
                        WITH pairs AS (
                            SELECT * FROM (VALUES {pairs_array}) AS t(source, target)
                        ),
                        routes AS (
                            SELECT
                                p.source AS start_vid,
                                p.target AS end_vid,
                                ST_LineMerge(ST_Union(w.geom ORDER BY route.path_seq)) AS route_geom
                            FROM pairs p
                            CROSS JOIN LATERAL (
                                SELECT * FROM pgr_dijkstra(
                                    'SELECT way_id AS id, source, target, cost, reverse_cost FROM ways WHERE cost > 0',
                                    p.source, p.target, directed := false
                                )
                            ) AS route
                            JOIN ways w ON route.edge = w.way_id
                            WHERE route.edge > 0
                            GROUP BY p.source, p.target
                        )
                        SELECT
                            start_vid,
                            end_vid,
                            ST_Transform(route_geom, 4326) AS geom,
                            ST_Length(route_geom) / 1000.0 AS length_km
                        FROM routes
                    """)

                    for row in cur.fetchall():
                        from shapely import wkb
                        key = (row[0], row[1])
                        if key not in route_results:
                            route_results[key] = {
                                'geom': wkb.loads(row[2], hex=True) if row[2] else None,
                                'length_km': float(row[3]) if row[3] else None
                            }
                    print(f"[MV Lines] After full table retry: {len(route_results)}/{len(valid_pairs)} paths")

                print(f"[MV Lines] Successfully routed {len(route_results)} paths via pgRouting")
            except Exception as routing_err:
                print(f"[MV Lines] pgRouting failed, will use straight lines: {routing_err}")
                import traceback
                traceback.print_exc()

    # Build MV lines from results
    mv_lines = []
    routed_count = 0
    road_segment_count = 0
    straight_count = 0

    for line_id, (i, j, straight_dist) in enumerate(mst_edges, 1):
        from_vertex = vertex_map.get(i)
        to_vertex = vertex_map.get(j)
        from_id = transformer_ids[i]
        to_id = transformer_ids[j]

        # Get transformer coordinates
        trafo1_x, trafo1_y = coords[i, 0], coords[i, 1]
        trafo2_x, trafo2_y = coords[j, 0], coords[j, 1]

        # Check if we have a routed path
        route = route_results.get((from_vertex, to_vertex)) or route_results.get((to_vertex, from_vertex))

        if route and route['geom']:
            # Add "hooks" connecting transformer points to the road path
            route_coords = _chain_route_coords(route['geom'], (trafo1_x, trafo1_y))

            # Determine which end of the route is closer to which transformer
            route_start = route_coords[0]
            route_end = route_coords[-1]

            dist_t1_start = (trafo1_x - route_start[0])**2 + (trafo1_y - route_start[1])**2
            dist_t1_end = (trafo1_x - route_end[0])**2 + (trafo1_y - route_end[1])**2

            if dist_t1_start <= dist_t1_end:
                # Route goes from near trafo1 to near trafo2
                # Add hook: trafo1 -> route_start ... route_end -> trafo2
                final_coords = [(trafo1_x, trafo1_y)] + route_coords + [(trafo2_x, trafo2_y)]
            else:
                # Route is reversed, goes from near trafo2 to near trafo1
                # Add hook: trafo1 -> route_end ... route_start -> trafo2
                final_coords = [(trafo1_x, trafo1_y)] + list(reversed(route_coords)) + [(trafo2_x, trafo2_y)]

            final_geom = LineString(final_coords)

            # Recalculate length with hooks included (approximate in km)
            hook1_dist = np.sqrt((trafo1_x - final_coords[1][0])**2 + (trafo1_y - final_coords[1][1])**2) * 111 * np.cos(np.radians(trafo1_y))
            hook2_dist = np.sqrt((trafo2_x - final_coords[-2][0])**2 + (trafo2_y - final_coords[-2][1])**2) * 111 * np.cos(np.radians(trafo2_y))
            total_length = route['length_km'] + hook1_dist + hook2_dist

            mv_lines.append({
                'mv_line_id': line_id,
                'from_transformer': from_id,
                'to_transformer': to_id,
                'length_km': round(total_length, 3),
                'voltage_kv': 20.0,
                'cable_type': 'NA2XS2Y 3x1x150',
                'geom': final_geom
            })
            routed_count += 1
        else:
            # Try to create a road-following connection between transformers
            # This handles: same vertex, disconnected components, or routing failures
            try:
                p1_x, p1_y = coords[i, 0], coords[i, 1]
                p2_x, p2_y = coords[j, 0], coords[j, 1]

                # Query to create a road-following path between two points
                cur.execute("""
                    WITH trafo1 AS (
                        SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) as geom
                    ),
                    trafo2 AS (
                        SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3035) as geom
                    ),
                    -- Find the nearest road segment for trafo1
                    nearest_road1 AS (
                        SELECT w.geom, w.way_id,
                               ST_ClosestPoint(w.geom, t.geom) as closest_pt,
                               ST_Distance(w.geom, t.geom) as dist
                        FROM ways w, trafo1 t
                        WHERE ST_DWithin(w.geom, t.geom, 500)
                        ORDER BY w.geom <-> t.geom
                        LIMIT 1
                    ),
                    -- Find the nearest road segment for trafo2
                    nearest_road2 AS (
                        SELECT w.geom, w.way_id,
                               ST_ClosestPoint(w.geom, t.geom) as closest_pt,
                               ST_Distance(w.geom, t.geom) as dist
                        FROM ways w, trafo2 t
                        WHERE ST_DWithin(w.geom, t.geom, 500)
                        ORDER BY w.geom <-> t.geom
                        LIMIT 1
                    ),
                    -- Build the connection: trafo1 -> road1_point -> road segment(s) -> road2_point -> trafo2
                    connection AS (
                        SELECT ST_LineMerge(ST_Union(ARRAY[
                            -- Trafo1 to its closest road point
                            ST_MakeLine((SELECT geom FROM trafo1), nr1.closest_pt),
                            -- Road segment between the two closest points (approximation: straight line along road direction)
                            ST_MakeLine(nr1.closest_pt, nr2.closest_pt),
                            -- Closest road point to Trafo2
                            ST_MakeLine(nr2.closest_pt, (SELECT geom FROM trafo2))
                        ])) as geom
                        FROM nearest_road1 nr1, nearest_road2 nr2
                    )
                    SELECT ST_Transform(geom, 4326) as geom,
                           ST_Length(geom) / 1000.0 as length_km
                    FROM connection
                    WHERE geom IS NOT NULL;
                """, (p1_x, p1_y, p2_x, p2_y))

                result = cur.fetchone()
                if result and result[0]:
                    from shapely import wkb
                    road_geom = wkb.loads(result[0], hex=True)
                    road_length = float(result[1]) if result[1] else straight_dist

                    mv_lines.append({
                        'mv_line_id': line_id,
                        'from_transformer': from_id,
                        'to_transformer': to_id,
                        'length_km': round(road_length, 3),
                        'voltage_kv': 20.0,
                        'cable_type': 'NA2XS2Y 3x1x150',
                        'geom': road_geom
                    })
                    road_segment_count += 1
                    print(f"[MV Lines] Edge {i}->{j}: Created road-segment connection ({road_length:.3f} km)")
                    continue
            except Exception as road_err:
                print(f"[MV Lines] Edge {i}->{j}: Road-segment fallback failed: {road_err}")

            # Final fallback to straight line
            if from_vertex is None or to_vertex is None:
                print(f"[MV Lines] Edge {i}->{j}: Using straight line (no road vertex found)")
            elif from_vertex == to_vertex:
                print(f"[MV Lines] Edge {i}->{j}: Using straight line (same vertex {from_vertex})")
            else:
                print(f"[MV Lines] Edge {i}->{j}: Using straight line (no route between vertices {from_vertex}->{to_vertex})")
            mv_lines.append({
                'mv_line_id': line_id,
                'from_transformer': from_id,
                'to_transformer': to_id,
                'length_km': round(straight_dist, 3),
                'voltage_kv': 20.0,
                'cable_type': 'NA2XS2Y 3x1x150',
                'geom': LineString([(coords[i, 0], coords[i, 1]), (coords[j, 0], coords[j, 1])])
            })
            straight_count += 1

    print(f"[MV Lines] Final result: {routed_count} pgRouting, {road_segment_count} road-segment, {straight_count} straight lines")

    if not mv_lines:
        return gpd.GeoDataFrame(columns=['mv_line_id', 'from_transformer', 'to_transformer',
                                          'length_km', 'voltage_kv', 'cable_type', 'geom'],
                                 geometry='geom', crs='EPSG:4326')

    return gpd.GeoDataFrame(mv_lines, geometry='geom', crs='EPSG:4326')
