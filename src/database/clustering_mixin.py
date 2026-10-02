import math
import warnings
import time
from abc import ABC
from decimal import *
from typing import *

import numpy as np
from scipy.cluster.hierarchy import cut_tree

from src import utils
from src.config_loader import *
from src.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class ClusteringMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

    def _canonicalize_distance_matrix(self, dist_matrix: np.ndarray) -> np.ndarray:
        """Return a strictly symmetric distance matrix for scipy.squareform.

        pgRouting cost matrix output can contain directional/missing pair artifacts.
        We normalize it as follows:
        - if both directions exist, keep the smaller cost (undirected cable routing intent)
        - if only one direction exists, keep that non-zero value
        - diagonal is forced to 0
        """
        if dist_matrix.size == 0:
            return dist_matrix

        # Work in float64 for numerical stability in downstream clustering.
        mat = dist_matrix.astype(np.float64, copy=False)
        np.fill_diagonal(mat, 0.0)

        mat_t = mat.T
        both_present = (mat > 0) & (mat_t > 0)
        one_sided = (mat > 0) ^ (mat_t > 0)

        # Symmetric merge:
        # - both present -> min(distance_ij, distance_ji)
        # - one-sided or both missing -> max(...) keeps non-zero when available
        sym = np.where(both_present, np.minimum(mat, mat_t), np.maximum(mat, mat_t))
        np.fill_diagonal(sym, 0.0)

        if np.any(one_sided):
            pair_count = int(np.count_nonzero(np.triu(one_sided, k=1)))
            self.logger.warning(
                "Distance matrix had %s one-sided routing pairs; canonicalized to symmetric values.",
                pair_count,
            )

        return sym

    def get_connected_component(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Reads from ways_tem
        :return:
        """
        component_query = """SELECT component, node
                             FROM pgr_connectedComponents(
                                     'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem');"""
        self.cur.execute(component_query)
        data = self.cur.fetchall()
        component = np.asarray([i[0] for i in data])
        node = np.asarray([i[1] for i in data])

        return component, node

    def count_no_kmean_buildings(self):
        """
        Counts relative buildings in buildings_tem, which could not be clustered via k-means
        :return: count
        """
        query = """SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE peak_load_in_kw != 0
                     AND kcid ISNULL;"""
        self.cur.execute(query)
        count = self.cur.fetchone()[0]

        return count

    def get_kcid_quality_stats(self) -> dict[str, int]:
        """Return aggregate QC metrics for consumer kcids after clustering."""
        self.cur.execute(
            """
            WITH consumer_kcids AS (
                SELECT kcid, COUNT(*) AS cnt
                FROM buildings_tem
                WHERE type != 'Transformer'
                  AND peak_load_in_kw > 0
                  AND kcid IS NOT NULL
                GROUP BY kcid
            )
            SELECT COALESCE(SUM(cnt), 0) AS total_consumers,
                   COUNT(*) AS total_kcids,
                   COALESCE(SUM(CASE WHEN cnt = 1 THEN 1 ELSE 0 END), 0) AS singleton_kcids,
                   COALESCE(SUM(CASE WHEN cnt = 1 THEN cnt ELSE 0 END), 0) AS singleton_consumers
            FROM consumer_kcids;
            """
        )
        total_consumers, total_kcids, singleton_kcids, singleton_consumers = self.cur.fetchone()

        self.cur.execute(
            """
            SELECT COUNT(*)
            FROM buildings_tem
            WHERE type != 'Transformer'
              AND peak_load_in_kw > 0
              AND kcid IS NULL;
            """
        )
        unassigned_consumers = self.cur.fetchone()[0]

        return {
            "total_consumers": int(total_consumers or 0),
            "total_kcids": int(total_kcids or 0),
            "singleton_kcids": int(singleton_kcids or 0),
            "singleton_consumers": int(singleton_consumers or 0),
            "unassigned_consumers": int(unassigned_consumers or 0),
        }

    def count_connected_buildings(self, vertices: Union[list, tuple]) -> int:
        """
        Get count from buildings_tem where type is not transformer
        :param vertices: np.array
        :return: count of buildings with given vertice_id s from buildings_tem
        """
        query = """SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE vertice_id IN %(v)s
                     AND type != 'Transformer';"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        count = self.cur.fetchone()[0]

        return count

    def delete_ways(self, vertices: list) -> None:
        """
        Deletes selected ways from ways_tem and ways_tem_vertices_pgr
        :param vertices:
        :return:
        """
        query = """DELETE
                   FROM ways_tem
                   WHERE target IN %(v)s;
        DELETE
        FROM ways_tem_vertices_pgr
        WHERE id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_connected_component_geometries(self, vertices: Union[list, tuple]) -> tuple[np.ndarray, np.ndarray]:
        """
        Gets the vertice IDs and coordinates of all buildings within a connected component
        :param vertices: vertice IDs of the connected component
        :return: (selected_vertices, coordinates) - vertice IDs and coordinates of the buildings within the connected component as tuple of two np.arrays
        """
        query = """
                SELECT vertice_id, ST_X(center) AS x, ST_Y(center) AS y
                FROM buildings_tem
                WHERE vertice_id IN %(v)s
                """
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        data = self.cur.fetchall()
        selected_vertices = np.array([x[0] for x in data])
        coordinates = np.array([(x[1], x[2]) for x in data], dtype=np.float64)

        return selected_vertices, coordinates

    def update_kmeans_cluster_multiple(self, vertices: np.ndarray, kcids: np.ndarray) -> None:
        """
        Assigns the given kcids to the buildings with the given vertice IDs.
        Both inputs should have the same length and corresponding order.
        :param vertices: np.array containing the vertice IDs of the buildings
        :param kcids: np.array containing the kcids
        :return:
        """
        query = """
                UPDATE buildings_tem
                SET kcid = %(k)s
                WHERE vertice_id IN %(v)s;
                """
        for kcid in np.unique(kcids):
            self.cur.execute(query, {"k": int(kcid), "v": tuple(map(int, vertices[kcids == kcid]))})

    def update_kmeans_cluster(self, vertices: list) -> None:
        """
        Groups connected components into a k-means id withouth applying clustering
        :param vertices:
        :return:
        """
        query = """
                WITH maxk AS (SELECT MAX(kcid) AS max_k FROM buildings_tem)
                UPDATE buildings_tem
                SET kcid = (CASE
                                WHEN m.max_k ISNULL THEN 1
                                ELSE m.max_k + 1
                    END)
                FROM maxk AS m
                WHERE vertice_id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_distance_matrix_from_kcid(self, kcid: int) -> tuple[dict, np.ndarray, dict]:
        """
        Creates a distance matrix from the buildings in the kcid
        Args:
            kcid: k-means cluster id
        Returns: The distance matrix of the buildings in the k-means cluster as np.array and the mapping between vertice_id and local ID as dict
        """

        costmatrix_query = """SELECT * \
                              FROM pgr_dijkstraCostMatrix( \
                                      'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem', \
                                      (SELECT array_agg(DISTINCT b.connection_point) \
                                       FROM (SELECT * \
                                             FROM buildings_tem \
                                             WHERE kcid = %(k)s \
                                               AND bcid ISNULL \
                                             ORDER BY connection_point) AS b), \
                                      false);"""
        params = {"k": kcid}
        localid2vid, dist_mat, _ = self.calculate_cost_arr_dist_matrix(costmatrix_query, params)

        return localid2vid, dist_mat, _

    def calculate_cost_arr_dist_matrix(self, costmatrix_query: str, params: dict) -> tuple[dict, np.ndarray, dict]:
        """
        Helper function for calculating cost array and distance matrix from given parameters
        """
        st = time.time()
        cost_df = pd.read_sql_query(
            costmatrix_query,
            con=self.conn,
            params=params,
            # agg_cost can exceed int32 on large routing matrices; keep as float.
            dtype={"start_vid": np.int32, "end_vid": np.int32, "agg_cost": np.float64},
        )
        cost_arr = cost_df.to_numpy()
        et = time.time()
        self.logger.debug(f"Elapsed time for SQL to cost_arr: {et - st}")
        localid2vid = dict(enumerate(cost_df["start_vid"].unique()))
        vid2localid = {y: x for x, y in localid2vid.items()}

        # Square distance matrix — vectorized assignment
        dist_matrix = np.zeros([len(localid2vid), len(localid2vid)], dtype=np.float64)
        st = time.time()
        row_indices = cost_df["start_vid"].map(vid2localid).to_numpy(dtype=np.intp)
        col_indices = cost_df["end_vid"].map(vid2localid).to_numpy(dtype=np.intp)
        dist_matrix[row_indices, col_indices] = cost_arr[:, 2]
        dist_matrix = self._canonicalize_distance_matrix(dist_matrix)
        et = time.time()
        self.logger.debug(f"Elapsed time for dist_matrix creation: {et - st}")
        return localid2vid, dist_matrix, vid2localid


    def generate_load_vector(self, kcid: int, bcid: int) -> np.ndarray:
        query = """SELECT SUM(peak_load_in_kw)::float
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND bcid = %(b)s
                   GROUP BY connection_point
                   ORDER BY connection_point;"""
        self.cur.execute(query, {"k": kcid, "b": bcid})
        load = np.asarray([i[0] for i in self.cur.fetchall()])

        return load

    def load_constrained_hierarchical_clustering(self, Z: np.ndarray, cluster_amount: int, localid2vid: dict, buildings: pd.DataFrame,
            consumer_cat_df: pd.DataFrame, transformer_capacities: np.ndarray, double_trans: np.ndarray, ) -> tuple[
        dict, dict, int]:
        """
        Attempts to cluster buildings based on hierarchical clustering linkage matrix Z and assigns transformers.

        This function cuts the hierarchical tree to form `cluster_amount` clusters. For each cluster, it calculates
        the simultaneous peak load. It then attempts to assign an optimal transformer (single or double) based on
        the load and available capacities. If a cluster's load exceeds the maximum single transformer capacity
        and has enough buildings, it is marked as invalid (too big).

        Args:
            Z (np.ndarray): The linkage matrix from hierarchical clustering (scipy.cluster.hierarchy.linkage).
            cluster_amount (int): The number of clusters to form.
            localid2vid (dict): Mapping from local clustering indices to building vertice IDs.
            buildings (pd.DataFrame): DataFrame containing building information (loads, types, etc.).
            consumer_cat_df (pd.DataFrame): DataFrame containing consumer category definitions (simultaneity factors).
            transformer_capacities (np.ndarray): Array of available single transformer capacities (sorted).
            double_trans (np.ndarray): Array of available double transformer capacities (sorted).

        Returns:
            tuple[dict, dict, int]:
                - invalid_cluster_dict (dict): Clusters that are too big (load > max single capacity & >= 5 buildings).
                  Key: cluster_id, Value: list of vertice IDs.
                - cluster_dict (dict): Valid clusters with assigned transformers.
                  Key: cluster_id, Value: tuple(list of vertice IDs, assigned transformer capacity).
                - cluster_count (int): The actual number of clusters formed.
        """
        flat_groups = cut_tree(Z, n_clusters=cluster_amount)
        cluster_ids = np.unique(flat_groups)
        cluster_count = len(cluster_ids)
        # Check if simultaneous load can be satisfied with possible transformers
        cluster_dict = {}
        invalid_cluster_dict = {}
        for cluster_id in range(cluster_count):
            vid_list = [localid2vid[lid[0]] for lid in np.argwhere(flat_groups == cluster_id)]
            total_sim_load = float(utils.simultaneousPeakLoad(buildings, consumer_cat_df, vid_list))
            if math.isnan(total_sim_load):
                total_sim_load = 0.0
            required_kva = float(
                utils.required_apparent_power_kva(
                    total_sim_load,
                    DEFAULT_POWER_FACTOR,
                    TRANSFORMER_LOADING_MARGIN,
                )
            )
            if (required_kva >= max(transformer_capacities) and len(vid_list) > 1):  # the cluster is too big
                invalid_cluster_dict[cluster_id] = vid_list
            elif required_kva < max(transformer_capacities):
                # find the smallest transformer, that satisfies the load
                opt_transformer = transformer_capacities[transformer_capacities >= required_kva][0]
                double_candidates = double_trans[double_trans >= required_kva * 1.15]
                opt_double_transformer = double_candidates[0] if len(double_candidates) > 0 else None
                if opt_double_transformer is None or (opt_double_transformer - required_kva) > (opt_transformer - required_kva):
                    cluster_dict[cluster_id] = (vid_list, opt_transformer)
                else:
                    cluster_dict[cluster_id] = (vid_list, opt_double_transformer)
            else:
                opt_transformer = math.ceil(required_kva)
                cluster_dict[cluster_id] = (vid_list, opt_transformer)
        return invalid_cluster_dict, cluster_dict, cluster_count

    def merge_small_kcids_into_nearest(
        self,
        max_buildings: int = 1,
        max_bridge_distance_m: float = 120.0,
        bridge_clazz: int = -99,
    ) -> dict[str, int]:
        """Merge tiny consumer kcids into a nearby larger kcid in a routing-safe way.

        For every small kcid (consumer count <= ``max_buildings``):
        - skip if it already contains an existing transformer (brownfield cluster)
        - evaluate candidate target kcids by nearest consumer-point distance
        - accept the first candidate whose connection-point gap <= ``max_bridge_distance_m``
        - add one synthetic bridge edge between connection points
        - move only non-transformer rows to the target kcid

        Returns merge statistics for logging and monitoring.
        """
        stats = {
            "small_kcids": 0,
            "merged_kcids": 0,
            "merged_buildings": 0,
            "bridges_added": 0,
            "skipped_too_far": 0,
            "skipped_with_transformer": 0,
            "skipped_no_points": 0,
        }
        if max_buildings < 1 or max_bridge_distance_m <= 0:
            return stats

        # Read all non-transformer consumer points once; this keeps merges deterministic
        # while avoiding repeated heavy SQL inside loops.
        self.cur.execute(
            """
            SELECT kcid, connection_point, ST_X(center) AS cx, ST_Y(center) AS cy
            FROM buildings_tem
            WHERE kcid IS NOT NULL
              AND type != 'Transformer'
              AND connection_point IS NOT NULL
              AND center IS NOT NULL;
            """
        )
        rows = self.cur.fetchall()
        if not rows:
            return stats

        points_by_kcid: dict[int, list[tuple[int, float, float]]] = {}
        for kcid, connection_point, cx, cy in rows:
            points_by_kcid.setdefault(int(kcid), []).append((int(connection_point), float(cx), float(cy)))

        if not points_by_kcid:
            return stats

        # Brownfield kcids must keep their own transformer association.
        self.cur.execute(
            """
            SELECT DISTINCT kcid
            FROM buildings_tem
            WHERE kcid IS NOT NULL
              AND type = 'Transformer';
            """
        )
        kcids_with_transformers = {int(r[0]) for r in self.cur.fetchall()}

        small_kcids = [k for k, pts in points_by_kcid.items() if len(pts) <= max_buildings]
        large_kcids = [k for k, pts in points_by_kcid.items() if len(pts) > max_buildings]
        stats["small_kcids"] = len(small_kcids)
        if not small_kcids or not large_kcids:
            return stats

        all_connection_points = sorted(
            {cp for points in points_by_kcid.values() for cp, _, _ in points}
        )
        if not all_connection_points:
            return stats

        self.cur.execute(
            """
            SELECT id, ST_X(geom) AS vx, ST_Y(geom) AS vy
            FROM ways_tem_vertices_pgr
            WHERE id = ANY(%(ids)s);
            """,
            {"ids": all_connection_points},
        )
        vertex_xy_by_cp = {
            int(cp_id): (float(vx), float(vy))
            for cp_id, vx, vy in self.cur.fetchall()
        }

        target_cache: dict[int, tuple[list[tuple[int, float, float]], np.ndarray]] = {}
        for target_kcid in large_kcids:
            target_points = points_by_kcid.get(target_kcid, [])
            if not target_points:
                continue
            target_arr = np.array([(x, y) for _, x, y in target_points], dtype=np.float64)
            target_cache[target_kcid] = (target_points, target_arr)
        if not target_cache:
            return stats

        for source_kcid in small_kcids:
            if source_kcid in kcids_with_transformers:
                stats["skipped_with_transformer"] += 1
                continue

            source_points = points_by_kcid.get(source_kcid, [])
            if not source_points:
                stats["skipped_no_points"] += 1
                continue

            source_arr = np.array([(x, y) for _, x, y in source_points], dtype=np.float64)
            candidate_pairs: list[tuple[float, int, int, int]] = []
            for candidate_kcid, (target_points, target_arr) in target_cache.items():
                if target_arr.size == 0:
                    continue
                pair_dist_sq = np.sum((source_arr[:, None, :] - target_arr[None, :, :]) ** 2, axis=2)
                source_idx, target_local_idx = np.unravel_index(int(np.argmin(pair_dist_sq)), pair_dist_sq.shape)
                candidate_pairs.append(
                    (
                        float(pair_dist_sq[source_idx, target_local_idx]),
                        int(candidate_kcid),
                        int(source_idx),
                        int(target_local_idx),
                    )
                )

            if not candidate_pairs:
                stats["skipped_no_points"] += 1
                continue

            candidate_pairs.sort(key=lambda item: item[0])
            target_kcid = None
            source_cp = None
            target_cp = None
            has_vertex_distance = False

            for _, candidate_kcid, source_idx, target_local_idx in candidate_pairs:
                target_points = target_cache[candidate_kcid][0]
                candidate_source_cp = int(source_points[source_idx][0])
                candidate_target_cp = int(target_points[target_local_idx][0])
                source_xy = vertex_xy_by_cp.get(candidate_source_cp)
                target_xy = vertex_xy_by_cp.get(candidate_target_cp)
                if source_xy is None or target_xy is None:
                    continue

                has_vertex_distance = True
                nearest_dist = float(
                    np.hypot(source_xy[0] - target_xy[0], source_xy[1] - target_xy[1])
                )
                if nearest_dist <= max_bridge_distance_m:
                    target_kcid = int(candidate_kcid)
                    source_cp = candidate_source_cp
                    target_cp = candidate_target_cp
                    break

            if target_kcid is None or source_cp is None or target_cp is None:
                if has_vertex_distance:
                    stats["skipped_too_far"] += 1
                else:
                    stats["skipped_no_points"] += 1
                continue

            if source_cp != target_cp:
                # Add a synthetic undirected edge to preserve routing connectivity
                # after kcid reassignment. Avoid duplicate links.
                self.cur.execute(
                    """
                    SELECT 1
                    FROM ways_tem
                    WHERE (source = %(s)s AND target = %(t)s)
                       OR (source = %(t)s AND target = %(s)s)
                    LIMIT 1;
                    """,
                    {"s": source_cp, "t": target_cp},
                )
                if not self.cur.fetchone():
                    self.cur.execute(
                        """
                        INSERT INTO ways_tem (clazz, source, target, cost, reverse_cost, geom, way_id, country_code)
                        SELECT %(clazz)s,
                               %(s)s,
                               %(t)s,
                               ST_Distance(vs.geom, vt.geom),
                               ST_Distance(vs.geom, vt.geom),
                               ST_MakeLine(vs.geom, vt.geom),
                               (SELECT COALESCE(MAX(way_id), 0) + 1 FROM ways_tem),
                               COALESCE((SELECT country_code FROM ways_tem LIMIT 1), 'DE')
                        FROM ways_tem_vertices_pgr vs
                        JOIN ways_tem_vertices_pgr vt ON vt.id = %(t)s
                        WHERE vs.id = %(s)s;
                        """,
                        {"clazz": int(bridge_clazz), "s": source_cp, "t": target_cp},
                    )
                    if self.cur.rowcount > 0:
                        stats["bridges_added"] += 1

            self.cur.execute(
                """
                UPDATE buildings_tem
                SET kcid = %(target)s
                WHERE kcid = %(source)s
                  AND type != 'Transformer';
                """,
                {"target": target_kcid, "source": source_kcid},
            )
            moved = int(self.cur.rowcount)
            if moved > 0:
                stats["merged_kcids"] += 1
                stats["merged_buildings"] += moved

        return stats

    def get_kcid_length(self) -> int:
        query = """SELECT COUNT(DISTINCT kcid)
                   FROM buildings_tem
                   WHERE kcid IS NOT NULL; """
        self.cur.execute(query)
        kcid_length = self.cur.fetchone()[0]
        return kcid_length

    def get_next_unfinished_kcid(self, plz: int, country_code: str = "DE", exclude: list[int] | None = None) -> int | None:
        """
        :return: one unmodeled k mean cluster ID - plz
        """
        query = """SELECT kcid
                   FROM buildings_tem
                   WHERE kcid NOT IN (SELECT DISTINCT kcid
                                      FROM grid_result
                                      WHERE version_id = %(v)s
                                        AND grid_result.plz = %(plz)s
                                        AND grid_result.country_code = %(cc)s)
                     AND kcid IS NOT NULL
                     AND NOT (kcid = ANY(%(exclude)s))
                   ORDER BY kcid
                   LIMIT 1;"""
        self.cur.execute(query, {"v": VERSION_ID, "plz": plz, "cc": country_code, "exclude": list(exclude or [])})
        row = self.cur.fetchone()
        return row[0] if row else None

    def get_included_transformers(self, kcid: int) -> list:
        """
        Reads the vertice ids of transformers from a given kcid
        :param kcid:
        :return: list
        """
        query = """SELECT vertice_id
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND type = 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        transformers_list = ([t[0] for t in data] if (data := self.cur.fetchall()) else [])
        return transformers_list

    def get_used_ont_vertices(self, plz: int, country_code: str = "DE") -> set[int]:
        """Return ONT vertex IDs already used by existing clusters in the same PLZ/country/version."""
        query = """SELECT DISTINCT ont_vertice_id
                   FROM grid_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                     AND country_code = %(cc)s
                     AND ont_vertice_id IS NOT NULL;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz, "cc": country_code})
        return {int(row[0]) for row in self.cur.fetchall()}

    def clear_grid_result_in_kmean_cluster(
        self,
        plz: int,
        kcid: int,
        only_greenfield: bool = True,
        country_code: str = "DE",
    ):
        # Remove old clustering at same postcode cluster
        clear_query = """DELETE
                         FROM grid_result
                         WHERE version_id = %(v)s
                           AND plz = %(pc)s
                           AND country_code = %(cc)s
                           AND kcid = %(kc)s """
        if only_greenfield:
            clear_query += " AND bcid >= 0;"
        else:
            clear_query += ";"

        params = {"v": VERSION_ID, "pc": plz, "kc": kcid, "cc": country_code}
        self.cur.execute(clear_query, params)
        self.logger.debug(f"Building clusters with plz = {plz}, k_mean cluster = {kcid} area cleared.")

    def upsert_bcid(self, plz: int, kcid: int, bcid: int, vertices: list, transformer_rated_power: int, country_code: str = "DE"):
        """
        Assign buildings in buildings_tem the bcid and stores the cluster in grid_result
        Args:
            plz: postcode cluster ID - plz
            kcid: kmeans cluster ID
            bcid: building cluster ID
            vertices: List of vertice_id of selected buildings
            transformer_rated_power: Apparent power of the selected transformer
            country_code: Country code (DE, NL, etc.)
        """
        # Insert references to building elements in which cluster they are.
        building_query = """UPDATE buildings_tem
                            SET bcid = %(bc)s
                            WHERE plz = %(pc)s
                              AND kcid = %(kc)s
                              AND bcid ISNULL
                              AND connection_point IN %(vid)s
                              AND type != 'Transformer'; """

        params = {"v": VERSION_ID, "pc": plz, "bc": bcid, "kc": kcid, "vid": tuple(map(int, vertices)), }
        self.cur.execute(building_query, params)

        # Insert new clustering
        cluster_query = """INSERT INTO grid_result (version_id, plz, country_code, kcid, bcid, transformer_rated_power)
                           VALUES (%(v)s, %(pc)s, %(cc)s, %(kc)s, %(bc)s, %(s)s); """

        params = {"v": VERSION_ID, "pc": plz, "cc": country_code, "bc": bcid, "kc": kcid, "s": int(transformer_rated_power)}
        self.cur.execute(cluster_query, params)

    def get_consumer_to_transformer_df(self, kcid: int, transformer_list: list) -> pd.DataFrame:
        consumer_query = """SELECT DISTINCT connection_point
                            FROM buildings_tem
                            WHERE kcid = %(k)s
                              AND type != 'Transformer';"""
        self.cur.execute(consumer_query, {"k": kcid})
        consumer_list = [t[0] for t in self.cur.fetchall()]

        cost_query = """SELECT *
                        FROM pgr_dijkstraCost(
                                'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem',
                                %(cl)s, %(tl)s,
                                false);"""
        cost_df = pd.read_sql_query(
            cost_query,
            con=self.conn,
            params={"cl": consumer_list, "tl": transformer_list},
            # Vertex IDs and path costs can exceed int16 bounds on real-world datasets.
            dtype={"start_vid": np.int32, "end_vid": np.int32, "agg_cost": np.float64},
        )

        return cost_df

    def count_kmean_cluster_consumers(self, kcid: int) -> int:
        query = """SELECT COUNT(DISTINCT vertice_id)
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND type != 'Transformer'
                     AND bcid ISNULL;"""
        self.cur.execute(query, {"k": kcid})
        count = self.cur.fetchone()[0]

        return count

    def delete_isolated_building(self, plz: int, kcid):
        query = """DELETE
                   FROM buildings_tem
                   WHERE plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid ISNULL;"""
        self.cur.execute(query, {"p": plz, "k": kcid})

    def assign_isolated_building_to_bcid(self, plz: int, kcid: int, country_code: str = "DE") -> int:
        """Assign remaining unassigned buildings in a kcid to bcid=0 and ensure a grid_result row exists."""
        query = """UPDATE buildings_tem
                   SET bcid = 0
                   WHERE plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid IS NULL
                     AND type != 'Transformer';"""
        self.cur.execute(query, {"p": plz, "k": kcid})
        assigned_rows = self.cur.rowcount

        if assigned_rows <= 0:
            return 0

        # Size singleton cluster with the same transformer mapping used elsewhere.
        settlement_type = self.get_settlement_type_from_plz(plz, country_code)
        transformer_capacities, _ = self.get_transformer_data(settlement_type)
        transformer_rated_power = 630
        if len(transformer_capacities) > 0:
            transformer_rated_power = int(transformer_capacities[0])

            self.cur.execute(
                """SELECT DISTINCT connection_point
                   FROM buildings_tem
                   WHERE plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = 0
                     AND type != 'Transformer'
                     AND connection_point IS NOT NULL;""",
                {"p": plz, "k": kcid},
            )
            conn_points = [row[0] for row in self.cur.fetchall()]
            if conn_points:
                try:
                    sim_load_kw = float(self.calculate_sim_load(conn_points))
                except (InvalidOperation, ValueError, TypeError):
                    sim_load_kw = 0.0
                required_kva = float(
                    utils.required_apparent_power_kva(
                        sim_load_kw,
                        DEFAULT_POWER_FACTOR,
                        TRANSFORMER_LOADING_MARGIN,
                    )
                )
                candidates = transformer_capacities[transformer_capacities >= required_kva]
                if len(candidates) > 0:
                    transformer_rated_power = int(candidates[0].item())
                else:
                    transformer_rated_power = int(transformer_capacities[-1].item())

        self.cur.execute(
            """INSERT INTO grid_result (version_id, plz, country_code, kcid, bcid, transformer_rated_power)
               VALUES (%(v)s, %(pc)s, %(cc)s, %(kc)s, 0, %(s)s)
               ON CONFLICT (version_id, kcid, bcid, plz, country_code)
               DO UPDATE SET transformer_rated_power = EXCLUDED.transformer_rated_power;""",
            {
                "v": VERSION_ID,
                "pc": plz,
                "cc": country_code,
                "kc": kcid,
                "s": transformer_rated_power,
            },
        )
        return assigned_rows

    def get_greenfield_bcids(self, plz: int, kcid: int, country_code: str = "DE") -> list:
        """
        Args:
            plz: loadarea cluster ID
            kcid: kmeans cluster ID
        Returns: A list of greenfield building clusters for a given plz
        """
        query = """SELECT DISTINCT bcid
                   FROM grid_result
                   WHERE version_id = %(v)s
                     AND kcid = %(kc)s
                     AND plz = %(pc)s
                     AND country_code = %(cc)s
                     AND model_status ISNULL
                   ORDER BY bcid; """
        params = {"v": VERSION_ID, "pc": plz, "kc": kcid, "cc": country_code}
        self.cur.execute(query, params)
        bcid_list = [t[0] for t in data] if (data := self.cur.fetchall()) else []
        return bcid_list

    def get_buildings_from_kcid(self, kcid: int, ) -> pd.DataFrame:
        """
        Args:
            kcid: kmeans_cluster ID
        Returns: A dataframe with all building information
        """
        buildings_query = """SELECT *
                             FROM buildings_tem
                             WHERE connection_point IS NOT NULL
                               AND kcid = %(k)s
                               AND bcid ISNULL;"""
        params = {"k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)

        self.logger.debug(f"Building data fetched. {len(buildings_df)} buildings from kc={kcid} ...")

        return buildings_df

    def get_buildings_from_bcid(self, plz: int, kcid: int, bcid: int) -> pd.DataFrame:

        buildings_query = """SELECT *
                             FROM buildings_tem
                             WHERE type != 'Transformer'
                               AND plz = %(p)s
                               AND bcid = %(b)s
                               AND kcid = %(k)s;"""
        params = {"p": plz, "b": bcid, "k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)
        # dropping duplicate indices
        # buildings_df = buildings_df[~buildings_df.index.duplicated(keep='first')]

        self.logger.debug(f"{len(buildings_df)} building data fetched.")

        return buildings_df

    def get_existing_transformer_capacity_trafo_ui(self, plz: int, kcid: int, bcid: int) -> Optional[int]:
        """
        Check if there's an existing transformer with a specific capacity for the given cluster.
        
        Args:
            plz (int): The postal code
            kcid (int): K-means cluster ID
            bcid (int): Building cluster ID
            
        Returns:
            Optional[int]: Transformer capacity if found, None otherwise
        """
        # Get the geometry of the cluster area as text format for proper psycopg2 serialization
        cluster_geom_query = """
            SELECT ST_AsText(ST_Collect(geom)) as cluster_geom_wkt
            FROM buildings_tem
            WHERE kcid = %(kcid)s AND bcid = %(bcid)s
        """
        self.cur.execute(cluster_geom_query, {"kcid": kcid, "bcid": bcid})
        result = self.cur.fetchone()
        
        if not result or not result[0]:
            return None
            
        cluster_geom_wkt = result[0]
        
        # Check if there's a transformer with a specific capacity in this area
        # Use a more robust approach to handle GEOS topology issues
        transformer_query = """
            SELECT transformer_rated_power
            FROM transformers t
            WHERE t.transformer_rated_power IS NOT NULL
            AND ST_Intersects(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, 3035)), 0)))
            LIMIT 1
        """
        
        try:
            self.cur.execute(transformer_query, {"cluster_geom_wkt": cluster_geom_wkt})
            result = self.cur.fetchone()
            
            if result:
                return int(result[0])
        except Exception as e:
            # If ST_Intersects fails due to topology issues, try with a small buffer
            try:
                fallback_query = """
                    SELECT transformer_rated_power
                    FROM transformers t
                    WHERE t.transformer_rated_power IS NOT NULL
                    AND ST_DWithin(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, 3035)), 0)), 1.0)
                    LIMIT 1
                """
                self.cur.execute(fallback_query, {"cluster_geom_wkt": cluster_geom_wkt})
                result = self.cur.fetchone()
                
                if result:
                    return int(result[0])
            except Exception as fallback_error:
                # Log the error but don't fail the entire process
                self.logger.warning(f"Could not check transformer intersection for plz={plz}, kcid={kcid}, bcid={bcid}: {fallback_error}")
        
        return None

    def update_transformer_rated_power(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        note: int,
        country_code: str = "DE",
    ):
        """
        Update the field transformer_rated_power in grid_result for a given building cluster (bcid).

        Process:
        1) Determine settlement type from postcode (plz) and fetch the allowed standard transformer capacities
           (ascending array transformer_capacities).
        2) Read the currently stored transformer_rated_power for the (plz, kcid, bcid) tuple.

        Behaviour controlled by note:
        - note == 0 (single standard transformer mode):
          Upgrade to the smallest standard capacity strictly greater than the current value.
          (Precondition: such a larger capacity must exist; otherwise an IndexError would occur.)
        - note != 0 (multi / grouped mode):
          a) Build an extended list by appending doubled capacities of selected mid–range sizes (transformer_capacities[2:4] * 2).
          b) If the current capacity already matches any allowed (standard or doubled) value: no change.
          c) Else round up to the next multiple of 630 kVA (ceil(current / 630) * 630) to emulate a grouped / parallel transformer arrangement.

        Parameters:
        plz  : Postcode cluster ID.
        kcid : K‑means cluster ID.
        bcid : Building cluster ID within the k‑means cluster.
        note : Control flag for update strategy (0 = standard single transformer upgrade, !=0 = multi / grouping logic).

        Returns:
        None. Performs an in‑place database update.
        """
        # First check if there's an existing transformer with a specific capacity
        existing_capacity = self.get_existing_transformer_capacity_trafo_ui(plz, kcid, bcid)
        if existing_capacity is not None:
            # Use the existing transformer capacity
            update_query = """UPDATE grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND country_code = %(cc)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "cc": country_code, "k": kcid, "b": bcid, "n": existing_capacity})
            self.logger.debug(f"Using existing transformer capacity {existing_capacity} kVA for plz={plz}, kcid={kcid}, bcid={bcid}")
            return
        
        sdl = self.get_settlement_type_from_plz(plz, country_code)
        transformer_capacities, _ = self.get_transformer_data(sdl)

        if note == 0:
            old_query = """SELECT transformer_rated_power
                           FROM grid_result
                           WHERE version_id = %(v)s
                             AND plz = %(p)s
                             AND country_code = %(cc)s
                             AND kcid = %(k)s
                             AND bcid = %(b)s;"""
            self.cur.execute(old_query, {"v": VERSION_ID, "p": plz, "cc": country_code, "k": kcid, "b": bcid})
            transformer_rated_power = self.cur.fetchone()[0]

            new_transformer_rated_power = transformer_capacities[transformer_capacities > transformer_rated_power][
                0].item()
            update_query = """UPDATE grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND country_code = %(cc)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "cc": country_code, "k": kcid, "b": bcid, "n": new_transformer_rated_power}, )
        else:
            double_trans = np.multiply(transformer_capacities[2:4], 2)
            combined = np.concatenate((transformer_capacities, double_trans), axis=None)
            combined = np.sort(combined, axis=None)
            old_query = """SELECT transformer_rated_power
                           FROM grid_result
                           WHERE version_id = %(v)s
                             AND plz = %(p)s
                             AND country_code = %(cc)s
                             AND kcid = %(k)s
                             AND bcid = %(b)s;"""
            self.cur.execute(old_query, {"v": VERSION_ID, "p": plz, "cc": country_code, "k": kcid, "b": bcid})
            transformer_rated_power = self.cur.fetchone()[0]
            if transformer_rated_power in combined.tolist():
                return None
            new_transformer_rated_power = np.ceil(transformer_rated_power / 630) * 630
            update_query = """UPDATE grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND country_code = %(cc)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "cc": country_code, "k": kcid, "b": bcid, "n": new_transformer_rated_power}, )
            self.logger.info(
                f"Updated transformer_rated_power (multi/group mode): plz={plz}, kcid={kcid}, bcid={bcid}, "
                f"old={transformer_rated_power} kVA -> new={new_transformer_rated_power} kVA)"
            )

    def get_transformer_data(self, settlement_type: int = None) -> tuple[np.array, dict]:
        """
        Args:
            Settlement type: 1=Rural, 2=Semi-urban, 3=Urban
        Returns: Typical transformer capacities and costs depending on the settlement type
        """
        if settlement_type not in TRANSFORMER_MAPPING:
            self.logger.warning(f"Unknown settlement_type={settlement_type}, defaulting to 2 (semi-urban).")
            settlement_type = 2

        allowed_capacities = tuple(TRANSFORMER_MAPPING[settlement_type])

        query = """SELECT equipment_data.s_max_kva,
                          COALESCE(installed_cost_eur, cost_eur, equipment_only_cost_eur) AS effective_cost_eur
                   FROM equipment_data
                   WHERE typ = 'Transformer' \
                     AND s_max_kva IN %(capacities)s
                   ORDER BY s_max_kva;"""

        self.cur.execute(query, {"capacities": allowed_capacities})
        data = self.cur.fetchall()
        capacities = [i[0] for i in data]
        transformer2cost = {i[0]: i[1] for i in data}

        self.logger.debug("Transformer data fetched.")
        return np.array(capacities), transformer2cost

    def update_building_cluster(self, transformer_id: int, conn_id_list: Union[list, tuple], count: int, kcid: int,
            plz: int, transformer_rated_power: int, country_code: str = "DE") -> None:
        """
        Update building cluster information by performing multiple operations:
          - Update the 'bcid' in 'buildings_tem' where 'vertice_id' matches the transformer_id.
          - Update the 'bcid' in 'buildings_tem' for rows where 'connection_point' is in the provided list and type is not 'Transformer'.
          - Insert a new record into 'grid_result'.
          - Insert a new record into 'transformer_positions' using subqueries for geometry and OGC ID.
        Args:
            transformer_id (int): The ID of the transformer.
            conn_id_list (Union[list, tuple]): A list or tuple of connection point IDs.
            count (int): The new building cluster identifier.
            kcid (int): The KCID value.
            plz (int): The postcode value.
            transformer_rated_power (int): The selected transformer size for the building cluster.
            country_code (str): The country code (e.g., 'DE', 'NL').
        """
        params = {
            "v": VERSION_ID,
            "count": count,
            "c": tuple(conn_id_list),
            "t": transformer_id,
            "k": kcid,
            "pc": plz,
            "l": transformer_rated_power,
            "cc": country_code,
        }

        # Keep updates scoped to the current PLZ/KCID cluster.
        self.cur.execute(
            """
            UPDATE buildings_tem
            SET bcid = %(count)s
            WHERE vertice_id = %(t)s
              AND type = 'Transformer'
              AND plz = %(pc)s
              AND kcid = %(k)s;
            """,
            params,
        )

        self.cur.execute(
            """
            UPDATE buildings_tem
            SET bcid = %(count)s
            WHERE connection_point IN %(c)s
              AND type != 'Transformer'
              AND plz = %(pc)s
              AND kcid = %(k)s
              AND bcid IS NULL;
            """,
            params,
        )

        # Insert cluster row and use RETURNING to avoid subquery cardinality issues.
        self.cur.execute(
            """
            INSERT INTO grid_result (version_id, plz, country_code, kcid, bcid, ont_vertice_id, transformer_rated_power)
            VALUES (%(v)s, %(pc)s, %(cc)s, %(k)s, %(count)s, %(t)s, %(l)s)
            RETURNING grid_result_id;
            """,
            params,
        )
        grid_result_id = self.cur.fetchone()[0]

        # Select a deterministic transformer row for this vertex.
        self.cur.execute(
            """
            SELECT center, osm_id
            FROM buildings_tem
            WHERE vertice_id = %(t)s
              AND type = 'Transformer'
              AND plz = %(pc)s
              AND kcid = %(k)s
            ORDER BY osm_id
            LIMIT 1;
            """,
            params,
        )
        transformer_row = self.cur.fetchone()
        if not transformer_row:
            # Fallback: still prefer transformer type if cluster filter misses due upstream inconsistencies.
            self.cur.execute(
                """
                SELECT center, osm_id
                FROM buildings_tem
                WHERE vertice_id = %(t)s
                ORDER BY CASE WHEN type = 'Transformer' THEN 0 ELSE 1 END, osm_id
                LIMIT 1;
                """,
                params,
            )
            transformer_row = self.cur.fetchone()

        if not transformer_row:
            raise ValueError(
                f"Transformer vertex {transformer_id} not found in buildings_tem (plz={plz}, kcid={kcid})."
            )

        self.cur.execute(
            """
            INSERT INTO transformer_positions (version_id, grid_result_id, geom, osm_id, comment)
            VALUES (%s, %s, %s, %s, 'Normal');
            """,
            (VERSION_ID, grid_result_id, transformer_row[0], transformer_row[1]),
        )

    def calculate_sim_load(self, conn_list: Union[tuple, list]) -> Decimal:
        if not conn_list:
            return Decimal(0)

        query = """SELECT
                       COALESCE(c.parent_category, '_default') AS category,
                       SUM(b.peak_load_in_kw), SUM(b.households_per_building),
                       COALESCE(c.sim_factor, d.sim_factor) AS sim_factor
                   FROM buildings_tem AS b
                   LEFT JOIN consumer_categories AS c ON b.f_class = c.definition
                   LEFT JOIN consumer_categories AS d ON d.definition = '_default'
                   WHERE b.connection_point IN %(c)s
                     AND b.type != 'Transformer'
                   GROUP BY COALESCE(c.parent_category, '_default'), COALESCE(c.sim_factor, d.sim_factor);"""
        self.cur.execute(query, {"c": tuple(conn_list)})
        rows = self.cur.fetchall()

        def _safe_decimal(value, default: Decimal = Decimal(0)) -> Decimal:
            if value is None:
                return default
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return default
            if not parsed.is_finite():
                return default
            return parsed

        total_sim_load = Decimal(0)
        for row in rows:
            load = _safe_decimal(row[1])
            count = _safe_decimal(row[2])
            factor = _safe_decimal(row[3], Decimal("0.07"))

            if load <= 0 or count <= 0:
                continue
            # Simultaneity factors are bounded by definition.
            factor = max(Decimal(0), min(Decimal(1), factor))
            try:
                sim = load * (factor + (Decimal(1) - factor) * (count ** Decimal("-0.75")))
            except InvalidOperation:
                continue
            if sim.is_finite() and sim > 0:
                total_sim_load += sim

        return total_sim_load

    def prefetch_building_loads_for_kcid(self, kcid: int) -> dict:
        """Pre-fetch all building load data for a kcid, keyed by connection_point.

        Returns a dict mapping connection_point -> list of (parent_category, peak_load, households, sim_factor).
        This avoids per-assignment SQL queries in position_brownfield_transformers.
        """
        query = """SELECT b.connection_point,
                       COALESCE(c.parent_category, '_default') AS category,
                       b.peak_load_in_kw, b.households_per_building,
                       COALESCE(c.sim_factor, d.sim_factor) AS sim_factor
                   FROM buildings_tem AS b
                   LEFT JOIN consumer_categories AS c ON b.f_class = c.definition
                   LEFT JOIN consumer_categories AS d ON d.definition = '_default'
                   WHERE b.kcid = %(k)s
                     AND b.type != 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        rows = self.cur.fetchall()
        building_loads = {}
        for cp, category, peak_load, households, sim_factor in rows:
            building_loads.setdefault(cp, []).append((category, peak_load, households, sim_factor))
        return building_loads

    @staticmethod
    def calculate_sim_load_from_cache(conn_list: Union[tuple, list], building_loads: dict) -> Decimal:
        """Calculate simultaneous load from pre-fetched building data without SQL queries."""
        if not conn_list:
            return Decimal(0)

        def _safe_decimal(value, default: Decimal = Decimal(0)) -> Decimal:
            if value is None:
                return default
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return default
            if not parsed.is_finite():
                return default
            return parsed

        # Aggregate by (category, sim_factor) across all connection points
        aggregated = {}
        for cp in conn_list:
            for category, peak_load, households, sim_factor in building_loads.get(cp, []):
                key = (category, sim_factor)
                if key not in aggregated:
                    aggregated[key] = [Decimal(0), Decimal(0)]
                aggregated[key][0] += _safe_decimal(peak_load)
                aggregated[key][1] += _safe_decimal(households)

        total_sim_load = Decimal(0)
        for (category, sim_factor), (load, count) in aggregated.items():
            if load <= 0 or count <= 0:
                continue
            factor = _safe_decimal(sim_factor, Decimal("0.07"))
            factor = max(Decimal(0), min(Decimal(1), factor))
            try:
                sim = load * (factor + (Decimal(1) - factor) * (count ** Decimal("-0.75")))
            except InvalidOperation:
                continue
            if sim.is_finite() and sim > 0:
                total_sim_load += sim
        return total_sim_load

    def get_building_connection_points_from_bc(self, kcid: int, bcid: int) -> list:
        """
        Args:
            kcid: kmeans_cluster ID
            bcid: building cluster ID
        Returns: A dataframe with all building information
        """
        count_query = """SELECT DISTINCT connection_point
                         FROM buildings_tem
                         WHERE vertice_id IS NOT NULL
                           AND bcid = %(b)s
                           AND kcid = %(k)s;"""
        params = {"b": bcid, "k": kcid}
        self.cur.execute(count_query, params)
        try:
            cp = [t[0] for t in self.cur.fetchall()]
        except:
            cp = []

        return cp

    def upsert_transformer_selection(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        connection_id: int,
        country_code: str = "DE",
    ):
        """Writes the vertice_id of chosen building as ONT location in the grid_result table"""

        query = """UPDATE grid_result
                   SET ont_vertice_id = %(c)s
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                     AND country_code = %(cc)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;

        UPDATE grid_result
        SET model_status = 1
        WHERE version_id = %(v)s
          AND plz = %(p)s
          AND country_code = %(cc)s
          AND kcid = %(k)s
          AND bcid = %(b)s;

        INSERT INTO transformer_positions (version_id, grid_result_id, geom, comment)
        VALUES(
                %(v)s,
                (SELECT grid_result_id
                 FROM grid_result
                 WHERE version_id = %(v)s \
                   AND plz = %(p)s \
                   AND country_code = %(cc)s \
                   AND kcid = %(k)s \
                   AND bcid = %(b)s),
                (SELECT geom FROM ways_tem_vertices_pgr WHERE id = %(c)s),
                'on_way');"""
        params = {
            "v": VERSION_ID,
            "c": connection_id,
            "b": bcid,
            "k": kcid,
            "p": plz,
            "cc": country_code,
        }

        self.cur.execute(query, params)

    def get_distance_matrix_from_bcid(self, kcid: int, bcid: int) -> tuple[dict, np.ndarray, dict]:
        """
        Args:
            kcid: k mean cluster ID
            bcid: building cluster ID
        Returns: The distance matrix of the buildings in the building cluster as np.array and the mapping between vertice_id and local ID as dict
        """

        costmatrix_query = """SELECT *
                              FROM pgr_dijkstraCostMatrix(
                                      'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem',
                                      (SELECT array_agg(DISTINCT b.connection_point)
                                       FROM (SELECT *
                                             FROM buildings_tem
                                             WHERE kcid = %(k)s
                                               AND bcid = %(b)s
                                             ORDER BY connection_point) AS b),
                                      false);"""
        params = {"b": bcid, "k": kcid}
        localid2vid, dist_mat, _ = self.calculate_cost_arr_dist_matrix(costmatrix_query, params)

        return localid2vid, dist_mat, _

    def get_settlement_type_from_plz(self, plz, country_code: str = "DE") -> int:
        """
        Args:
            plz:
        Returns: Settlement type: 1=Rural, 2=Semi-urban, 3=Urban
        """
        settlement_query = """SELECT settlement_type
                              FROM postcode_result
                              WHERE postcode_result_plz = %(p)s
                                AND country_code = %(cc)s
                              LIMIT 1; """
        self.cur.execute(settlement_query, {"p": plz, "cc": country_code})
        settlement_type = self.cur.fetchone()[0]

        return settlement_type
