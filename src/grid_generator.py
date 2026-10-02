import logging
import os
import math
import time
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd  # type: ignore
import pandapower as pp
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans
from concurrent.futures import ProcessPoolExecutor, as_completed  # lightweight parallel execution

import src.database.database_client as dbc
from src.infdb.infdb_client import InfdbClient
from src.analysis.parameter_calculation import ParameterCalculator
from src import utils
from src.config_loader import *

# Import electrical backend components
from src.electrical_backend import IElectricalBackend, create_backend
from src.cable_installer import CableInstaller

class ResultExistsError(Exception):
    "Raised when the PLZ has already been created."
    pass


class GridGenerator:
    """
    Generates the grid for the given plz area
    """

    def __init__(self, plz=999999, country_code: str = "DE", **kwargs):
        self.plz = plz
        self.country_code = country_code
        self.dbc = dbc.DatabaseClient()
        self.dbc.insert_version_if_not_exists()
        self.logger = utils.create_logger(
            name="GridGenerator", log_file=kwargs.get("log_file", "log.txt"), log_level=LOG_LEVEL
        )
        self.inf_dbc = None
        if USE_INFDB:
            self.inf_dbc = InfdbClient()
        self._consumer_categories_cache = None
        self._settlement_type_cache_by_plz = {}
        self._transformer_data_cache_by_plz = {}
        self.current_stage = "init"
        self.current_stage_started_at = time.time()

    def __del__(self):
        self.dbc.__del__()

    def _run_stage(self, stage_name: str, stage_fn) -> None:
        """Run a generation stage with explicit timing markers for diagnostics."""
        self.current_stage = stage_name
        self.current_stage_started_at = time.time()
        # Ensure temp views still exist (guards against implicit rollback / connection hiccups)
        self.dbc.ensure_temp_views(self.plz)
        self.logger.info(f"[PLZ {self.plz}] stage={stage_name} START")
        stage_fn()
        elapsed = time.time() - self.current_stage_started_at
        self.logger.info(f"[PLZ {self.plz}] stage={stage_name} DONE in {elapsed:.2f}s")

    def _get_consumer_categories_cached(self) -> pd.DataFrame:
        if self._consumer_categories_cache is None:
            self._consumer_categories_cache = self.dbc.get_consumer_categories()
        return self._consumer_categories_cache

    def _get_settlement_type_cached(self, plz: int) -> int:
        if plz not in self._settlement_type_cache_by_plz:
            self._settlement_type_cache_by_plz[plz] = self.dbc.get_settlement_type_from_plz(
                plz, self.country_code
            )
        return self._settlement_type_cache_by_plz[plz]

    def _get_transformer_data_cached(self, plz: int):
        if plz not in self._transformer_data_cache_by_plz:
            settlement_type = self._get_settlement_type_cached(plz)
            self._transformer_data_cache_by_plz[plz] = self.dbc.get_transformer_data(settlement_type)
        return self._transformer_data_cache_by_plz[plz]

    def generate_grid_for_single_plz(
        self,
        plz: str,
        analyze_grids: bool = False,
        refresh_mv: bool = True,
        raise_on_error: bool = True,
    ) -> bool:
        """Generates the grid for a single PLZ.

        :param plz: Postal code for which the grid should be generated.
        :type plz: str
        :param analyze_grids: Option to analyze the results after grid generation, defaults to False.
        :type analyze_grids: bool
        :param refresh_mv: Refresh materialized views after processing, defaults to True.
        :type refresh_mv: bool
        """
        self.plz = plz

        # Suppress verbose per-PLZ logging by default.
        # Set PYLOVO_TRACE_GRID=1 to keep stage-level logs for debugging.
        trace_grid = os.getenv("PYLOVO_TRACE_GRID", "0").strip().lower() not in {"0", "false", "no"}
        prev_level = self.logger.level
        prev_dbc_level = self.dbc.logger.level
        if not trace_grid:
            self.logger.setLevel(logging.CRITICAL)
            self.dbc.logger.setLevel(logging.CRITICAL)
        else:
            self.logger.setLevel(logging.INFO)
            self.dbc.logger.setLevel(logging.INFO)
            self.logger.info(f"[PLZ {self.plz}] trace mode enabled (PYLOVO_TRACE_GRID=1)")

        self.current_stage = "create_temp_tables"
        self.current_stage_started_at = time.time()
        self.dbc.create_temp_tables(plz)  # create PLZ-suffixed temp tables
        # self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work

        error = None
        cleanup_error = None
        try:
            self.current_stage = "generate_grid"
            self.current_stage_started_at = time.time()
            self.generate_grid()
            self.current_stage = "save_tables"
            self.current_stage_started_at = time.time()
            enable_nearest_fallback = os.getenv("ENABLE_NEAREST_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
            self.dbc.save_tables(
                plz=self.plz,
                country_code=self.country_code,
                enable_nearest_fallback=enable_nearest_fallback,
            )  # Save data from temporary tables to result tables
            self.dbc.commit_changes()
            if analyze_grids:
                self.current_stage = "analyze_grids"
                self.current_stage_started_at = time.time()
                pc = ParameterCalculator(country_code=self.country_code)
                pc.dbc.logger.setLevel(logging.CRITICAL)
                pc.calc_parameters_per_plz(plz)
                self.dbc.commit_changes()  # commit the changes to the database
        except ResultExistsError:
            self.dbc.logger.info(f"Grid for the postcode area {plz} has already been generated.")
        except Exception as e:
            self.logger.error(
                f"Error during grid generation for PLZ {self.plz} at stage '{self.current_stage}': {e}"
            )
            self.logger.info(f"Skipped PLZ {self.plz} due to generation error.")
            try:
                self.dbc.ensure_connection(clear_transaction=True)
            except Exception as conn_err:
                self.logger.warning(
                    f"[PLZ {self.plz}] failed to restore DB connection after stage error: {conn_err}"
                )
            try:
                # Best-effort cleanup of sample_set marker after a failed run.
                self.dbc.delete_plz_from_sample_set_table(str(CLASSIFICATION_VERSION), self.plz)
            except Exception as delete_err:
                self.logger.warning(
                    f"[PLZ {self.plz}] failed to delete sample_set entry after generation error: {delete_err}"
                )
            error = e
            if not raise_on_error:
                traceback.print_exc()
        finally:
            # Always clean up temporary tables, even if there was an error
            self.current_stage = "drop_temp_tables"
            self.current_stage_started_at = time.time()
            try:
                self.dbc.ensure_connection(clear_transaction=True)
                self.dbc.drop_temp_tables(plz)  # drop PLZ-suffixed temp tables
            except Exception as drop_err:
                cleanup_error = drop_err
                self.logger.warning(f"[PLZ {self.plz}] failed to drop temp tables: {drop_err}")
            finally:
                # Restore log levels
                self.logger.setLevel(prev_level)
                self.dbc.logger.setLevel(prev_dbc_level)

        if error is not None:
            if raise_on_error:
                raise error
            return False
        if cleanup_error is not None:
            if raise_on_error:
                raise cleanup_error
            return False

        if refresh_mv:
            # update the materialized views to reflect changes in their base tables
            self.current_stage = "refresh_materialized_views"
            self.current_stage_started_at = time.time()
            self.dbc.refresh_materialized_views()
        self.current_stage = "commit"
        self.current_stage_started_at = time.time()
        self.dbc.commit_changes()  # commit the changes to the database
        self.current_stage = "done"
        self.current_stage_started_at = time.time()
        return True

    def generate_grid_for_multiple_plz(
        self, df_plz: pd.DataFrame, analyze_grids: bool = False, parallel: bool = True
    ) -> None:
        """Generate grids for all PLZ entries. Materialized views are refreshed once all grids have been processed.
        :param df_plz: table that contains PLZ for grid generation
        :param analyze_grids: option to analyse the results after grid generation, defaults to False
        :param parallel: optionally use parallel workers, defaults to True
        """
        plz_list = [str(row["plz"]) for _, row in df_plz.iterrows()]

        # Add f_classes columns to shared tables ONCE before workers spawn.
        # ALTER TABLE requires ACCESS EXCLUSIVE lock — running from workers deadlocks.
        self.dbc.ensure_shared_table_columns()

        # Use parallel processing if:
        # 1. parallel=True AND
        # 2. We have multiple PLZ to process AND
        # 3. We have more than 1 CPU core available (can't parallelize with 1 core)
        should_use_parallel = parallel and len(plz_list) > 1 and N_JOBS > 1
        
        print(f"Processing {len(plz_list)} PLZ areas (parallel={should_use_parallel}, workers={min(N_JOBS, len(plz_list)) if should_use_parallel else 1})")
        
        if should_use_parallel:
            # Use parallel processing for multiple PLZ
            # Use up to N_JOBS workers, but not more than the number of PLZ
            max_workers = min(N_JOBS, len(plz_list))
            
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                # Create a dictionary that maps futures to their corresponding PLZ.
                futures = {
                    executor.submit(GridGenerator._worker, plz, analyze_grids): plz
                    for plz in plz_list
                }
                
                completed_count = 0
                total_count = len(plz_list)
                
                import sys
                
                try:
                    worker_timeout_minutes = CONFIG_GENERATION.get("WORKER_TIMEOUT_MINUTES", 30)
                    worker_timeout = worker_timeout_minutes * 60  # Convert to seconds
                    failed_plz = []
                    for future in as_completed(futures, timeout=worker_timeout):
                        plz = futures[future]
                        completed_count += 1
                        remaining = total_count - completed_count
                        
                        try:
                            future.result()
                            status = "OK"
                        except Exception as exc:
                            status = "FAIL"
                            failed_plz.append(plz)
                            
                            try:
                                future.cancel()
                            except Exception:
                                pass
                        
                        # Single updating progress line
                        sys.stdout.write(f"\r[{completed_count}/{total_count}] Last: PLZ {plz} ({status}) | {remaining} remaining   ")
                        sys.stdout.flush()
                    
                    # Final newline + summary
                    print()
                    if failed_plz:
                        print(f"Completed: {total_count - len(failed_plz)}/{total_count} OK, {len(failed_plz)} failed: {failed_plz}")
                    else:
                        print(f"All {total_count} PLZ completed successfully.")
                            
                except KeyboardInterrupt:
                    print(f"\n[WARN]  KeyboardInterrupt received. Shutting down gracefully...")
                    print(f"   Completed: {completed_count}/{total_count} PLZ")
                    
                    # Cancel all pending futures
                    for future in futures:
                        future.cancel()
                    
                    # Wait a bit for ongoing processes to finish gracefully
                    print("   Waiting for ongoing processes to finish...")
                    try:
                        # Give processes time to finish gracefully based on config
                        shutdown_timeout = CONFIG_GENERATION.get("GRACEFUL_SHUTDOWN_TIMEOUT", 5)
                        for future in as_completed(futures, timeout=shutdown_timeout):
                            if not future.cancelled():
                                plz = futures[future]
                                try:
                                    future.result()
                                    print(f"[OK] Gracefully completed PLZ {plz}")
                                except Exception as exc:
                                    print(f"[FAIL] PLZ {plz} failed during graceful shutdown: {exc}")
                    except Exception:
                        # Timeout or other exception during graceful shutdown
                        pass
                    
                    print("   Shutdown complete.")
                    raise KeyboardInterrupt("Grid generation interrupted by user")
                    
                except Exception as e:
                    print(f"\n[ERROR] Error during parallel processing: {e}")
                    print(f"   Completed: {completed_count}/{total_count} PLZ")
                    
                    # Cancel all pending futures on any error
                    for future in futures:
                        future.cancel()
                    
                    raise
        else:
            for plz in plz_list:
                # defer materialized view refresh until all PLZ are processed
                self.generate_grid_for_single_plz(
                    plz=plz, analyze_grids=analyze_grids, refresh_mv=False
                )

        # refresh materialized views once after all grids have been generated
        try:
            self.dbc.refresh_materialized_views()
            self.dbc.commit_changes()
        except Exception as e:
            self.logger.error(f"Error refreshing materialized views: {e}")
            # Don't re-raise here as individual PLZ processing might have succeeded

    @staticmethod
    def _worker(plz: str, analyze_grids: bool) -> None:
        """Worker process to generate a grid for a single PLZ."""
        log_file = Path("log") / f"log_{plz}.txt"
        if log_file.exists():
            log_file.unlink()  # Overwrite log file if it exists
        
        # Create a dedicated GridGenerator instance for this worker
        # This ensures each worker has its own database connection and logger
        gg = None
        try:
            gg = GridGenerator(log_file=log_file)  # dedicated logger per PLZ
            
            # Generate grid with proper error handling
            gg.generate_grid_for_single_plz(
                plz=plz, analyze_grids=analyze_grids, refresh_mv=False
            )
            
        except Exception as e:
            print(f"[FAIL] Worker failed for PLZ {plz}: {e}")
            import traceback
            traceback.print_exc()
            
            # Ensure proper cleanup even on failure
            if gg and hasattr(gg, 'dbc') and gg.dbc:
                try:
                    gg.dbc.conn.rollback()
                except Exception as rollback_error:
                    print(f"Rollback error for PLZ {plz}: {rollback_error}")
            raise
        finally:
            # Ensure proper cleanup of database connections
            if gg and hasattr(gg, 'dbc') and gg.dbc:
                try:
                    # Use the close method which handles all connection types
                    gg.dbc.close()
                except Exception as cleanup_error:
                    print(f"Cleanup error for PLZ {plz}: {cleanup_error}")
            
            # Also ensure GridGenerator cleanup
            if gg:
                try:
                    gg.__del__()
                except Exception as del_error:
                    pass
            
            pass

    def generate_grid(self):
        if self.dbc.is_grid_generated(self.plz, self.country_code):
            raise ResultExistsError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {VERSION_ID}."
            )
        self._run_stage("prepare_data_from_config", self.prepare_data_from_config)
        self._run_stage("prepare_postcodes", self.prepare_postcodes)
        self._run_stage("prepare_buildings", self.prepare_buildings)
        self._run_stage("prepare_transformers", self.prepare_transformers)
        self._run_stage("prepare_ways", self.prepare_ways)
        self._run_stage("apply_kmeans_clustering", self.apply_kmeans_clustering)
        self._run_stage("position_all_transformers", self.position_all_transformers)
        self._run_stage("install_cables", self.install_cables)

    def prepare_data_from_config(self):
        """
        Load data from config.
        """
        self.dbc.insert_equipment_data_from_config(equipment_data=EQUIPMENT_DATA)
        self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work
        self.dbc.insert_consumer_categories_from_config(consumer_categories=CONSUMER_CATEGORIES)

    def prepare_postcodes(self):
        """
        Caches postcode from raw data tables and stores in temporary tables.
        FROM: postcode
        INTO: postcode_result
        """
        self.dbc.copy_postcode_result_table(self.plz, self.country_code)
        self.logger.info(f"Working on plz {self.plz}")

    def prepare_buildings(self):
        """
        Caches buildings from raw data tables and stores in temporary tables.
        FROM: res, oth
        INTO: buildings_tem
        """
        if USE_INFDB:
            if TESTING:
                allocated_plz = self.dbc.get_plz_for_testing(self.plz)
                buildings_data = self.inf_dbc.fetch_buildings_from_infdb(allocated_plz)
                self.dbc.set_buildings_table_with_geometry_filter(buildings_data, allocated_plz)
            else:
                buildings_data = self.inf_dbc.fetch_buildings_from_infdb(self.plz)
                self.dbc.set_buildings_table(buildings_data, self.plz)
        else:
            self.dbc.set_residential_buildings_table(self.plz, self.country_code)
            self.dbc.set_other_buildings_table(self.plz, self.country_code)
        # Create indexes after bulk INSERT to avoid index maintenance overhead while loading.
        self.dbc.create_buildings_tem_indexes(self.plz)
        # self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work
        self.logger.info("Buildings_tem table prepared")
        self.dbc.remove_duplicate_buildings()
        self.logger.info("Duplicate buildings removed from buildings_tem")

        try:
            avg_hh = self.dbc.calculate_avg_households_per_building(self.plz, self.country_code)
            house_dist = self.dbc.calculate_house_distance_metric(self.plz, self.country_code)
            settlement_type = self.dbc.set_settlement_type_per_plz(
                self.plz,
                self.country_code,
                settlement_type_thresholds={
                    "rural_max_households": RURAL_MAX_HOUSEHOLDS,
                    "urban_min_households": URBAN_MIN_HOUSEHOLDS,
                    "rural_min_distance": RURAL_MIN_BUILDING_DISTANCE,
                    "urban_max_distance": URBAN_MAX_BUILDING_DISTANCE,
                    "score_deadband": SETTLEMENT_TYPE_SCORE_DEADBAND,
                },
            )
            self.logger.info(
                f"Settlement type determined (avg_households_per_building={avg_hh:.2f}, house_distance={house_dist:.1f} m, settlement_type={settlement_type})"
            )
        except Exception as e:
            self.logger.warning(f"Settlement type classification failed: {e}")

        unloadcount = self.dbc.set_building_peak_load()
        self.logger.info(
            f"Building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large_consumers = self.dbc.update_too_large_consumers_to_zero()
        self.logger.debug(f"{too_large_consumers} too large consumers removed from buildings_tem")

        self.dbc.assign_close_buildings()
        self.logger.debug("All close buildings assigned and removed from buildings_tem")

    def prepare_transformers(self):
        """
        Cache transformers from raw data tables and stores in temporary tables.
        FROM: transformers
        INTO: buildings_tem
        """
        self.dbc.insert_transformers(self.plz, self.country_code)
        self.logger.info("Transformers inserted into buildings_tem table")
        self.dbc.count_indoor_transformers()
        self.dbc.drop_indoor_transformers()
        self.logger.info("Indoor transformers removed from buildings_tem table")

    def prepare_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        """
        if USE_INFDB:
            if TESTING:
                allocated_plz = self.dbc.get_plz_for_testing(self.plz)
                ways_rows = self.inf_dbc.fetch_ways_from_infdb(allocated_plz)
                ways_count = self.dbc.set_ways_tem_table_with_geometry_filter(ways_rows, allocated_plz)
            else:
                ways_rows = self.inf_dbc.fetch_ways_from_infdb(self.plz)
                ways_count = self.dbc.set_ways_tem_table_infdb(ways_rows, self.plz)
        else:
            ways_count = self.dbc.set_ways_tem_table(self.plz, self.country_code)
        self.logger.info(f"The ways_tem table filled with {ways_count} ways")
        # Create ways indexes after bulk INSERT for faster downstream topology/routing steps.
        self.dbc.create_ways_tem_indexes(self.plz)

        # Update planner statistics after bulk data loading
        self.dbc.analyze_temp_tables(self.plz)

        # Run preprocessing functions that segment roads and connect buildings
        self.dbc.preprocess_ways()
        self.logger.info(f"Ways preprocessing completed in ways_tem.")

        # Build pgRouting topology on the processed network
        self.dbc.build_pgr_network_topology(self.plz)
        self.logger.info(f"pgRouting network topology created from ways_tem.")

        self.dbc.update_ways_cost()
        unconn = self.dbc.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")

    def apply_kmeans_clustering(self):
        """
        Find connected components (subgraphs) of an undirected street graph using Depth-First Search algorithm over
        edges and vertices from ways_tem and, if necessary due to their size, apply k-means clustering to these
        street network components.

        FROM: ways_tem, buildings_tem
        INTO: ways_tem, vertices_pgr, buildings_tem
        """

        # Get connected components from the street network
        component, vertices = self.dbc.get_connected_component()
        component_ids, component_counts = np.unique(component, return_counts=True)
        
        self.logger.info(f"Street network analysis: found {len(component_ids)} connected components")
        for cid, count in zip(component_ids, component_counts):
            self.logger.debug(f"  - Component {cid}: {count} vertices")

        if len(component_ids) > 0:
            # Handle components based on number
            if len(component_ids) > 1:
                # Process multiple connected components
                for i, component_id in enumerate(component_ids):
                    related_vertices = vertices[np.argwhere(component == component_id)]
                    self._process_component_to_kcid(related_vertices, i)
            else:
                # Process single connected component
                self._process_component_to_kcid(vertices)
        else:
            # No components found - issue warning
            warnings.warn("No connected components found in ways_tem table")

        if SMALL_COMPONENT_MERGE_ENABLED:
            merge_stats = self.dbc.merge_small_kcids_into_nearest(
                max_buildings=SMALL_COMPONENT_MERGE_MAX_BUILDINGS,
                max_bridge_distance_m=SMALL_COMPONENT_MERGE_MAX_DISTANCE_M,
                bridge_clazz=SMALL_COMPONENT_MERGE_BRIDGE_CLAZZ,
            )
            if merge_stats["small_kcids"] > 0:
                self.logger.info(
                    "Small-kcid merge: merged_kcids=%s merged_buildings=%s bridges_added=%s "
                    "skipped_too_far=%s skipped_with_transformer=%s skipped_no_points=%s",
                    merge_stats["merged_kcids"],
                    merge_stats["merged_buildings"],
                    merge_stats["bridges_added"],
                    merge_stats["skipped_too_far"],
                    merge_stats["skipped_with_transformer"],
                    merge_stats["skipped_no_points"],
                )

        # Verify clustering was successful for all buildings
        no_kmean_count = self.dbc.count_no_kmean_buildings()
        if no_kmean_count not in [0, None]:
            warnings.warn(f"K-means clustering issue: {no_kmean_count} buildings not assigned to clusters")

        if KCID_QA_GATES_ENABLED:
            qa_stats = self.dbc.get_kcid_quality_stats()
            total_kcids = qa_stats["total_kcids"]
            singleton_ratio = (
                qa_stats["singleton_kcids"] / total_kcids if total_kcids > 0 else 0.0
            )
            self.logger.info(
                "KCID QA: total_kcids=%s singleton_kcids=%s singleton_ratio=%.3f "
                "unassigned_consumers=%s total_consumers=%s",
                qa_stats["total_kcids"],
                qa_stats["singleton_kcids"],
                singleton_ratio,
                qa_stats["unassigned_consumers"],
                qa_stats["total_consumers"],
            )

            qa_failures = []
            if qa_stats["unassigned_consumers"] > KCID_QA_MAX_UNASSIGNED_CONSUMERS:
                qa_failures.append(
                    f"unassigned_consumers={qa_stats['unassigned_consumers']} "
                    f"> max={KCID_QA_MAX_UNASSIGNED_CONSUMERS}"
                )
            if singleton_ratio > KCID_QA_MAX_SINGLETON_RATIO:
                qa_failures.append(
                    f"singleton_kcid_ratio={singleton_ratio:.3f} > max={KCID_QA_MAX_SINGLETON_RATIO:.3f}"
                )

            if qa_failures:
                qa_msg = "KCID QA gate failed: " + "; ".join(qa_failures)
                if KCID_QA_RAISE_ON_FAILURE:
                    raise ValueError(qa_msg)
                warnings.warn(qa_msg)

    def _process_component_to_kcid(self, vertices, component_index=None):
        """Helper method to process components to kcid groups"""
        conn_building_count = self.dbc.count_connected_buildings(vertices)

        if conn_building_count is None or conn_building_count == 0:
            # Truly empty component — remove ways and transformers
            self.dbc.delete_ways(vertices)
            self.dbc.delete_transformers_from_buildings_tem(vertices)
            self.logger.debug("Empty component removed. Ways and transformers deleted from temporary tables.")
        elif conn_building_count == 1:
            # Single building — assign kcid so it's not orphaned
            self.dbc.update_kmeans_cluster(vertices)
            self.logger.debug("Single-building component assigned kcid.")
        elif conn_building_count >= LARGE_COMPONENT_LOWER_BOUND:
            # KMeans applied to large component for precise clustering
            cluster_count = max(2, math.ceil(conn_building_count / LARGE_COMPONENT_DIVIDER))
            k_means = KMeans(
                n_clusters=cluster_count, random_state=K_MEANS_SEED,
                n_init=10
            )
            (selected_vertices, coordinates) = self.dbc.get_connected_component_geometries(vertices)
            kcids = k_means.fit_predict(coordinates) + self.dbc.get_kcid_length() + 1
            self.dbc.update_kmeans_cluster_multiple(selected_vertices, kcids)
            log_msg = f"Large component {component_index} clustered into {cluster_count} groups" if component_index is not None else f"Large component clustered into {cluster_count} groups"
            self.logger.debug(log_msg)
        else:
            # Allocate cluster id for connected component smaller than the building threshold
            self.dbc.update_kmeans_cluster(vertices)

    def position_all_transformers(self):
        """
        Positions all transformers for each bcid cluster (brownfield with existing transformers and greenfield)
        FROM: buildings_tem, grid_result
        INTO: buildings_tem, grid_result
        """
        kcid_length = self.dbc.get_kcid_length()
        # A kcid can finish without a grid_result row; excluding attempted kcids
        # stops it from being picked again and starving the kcids after it.
        attempted_kcids = []

        for _ in range(kcid_length):
            kcid = self.dbc.get_next_unfinished_kcid(self.plz, self.country_code, exclude=attempted_kcids)
            if kcid is None:
                self.logger.debug("No unfinished kcids remain for plz %s.", self.plz)
                break
            attempted_kcids.append(kcid)
            self.current_stage = f"position_all_transformers:kcid={kcid}"
            self.current_stage_started_at = time.time()
            self.logger.debug(f"working on kcid {kcid}")
            
            # Clear existing data for this kcid to avoid PK/Unique violations on re-run
            self.dbc.clear_grid_result_in_kmean_cluster(
                self.plz, kcid, only_greenfield=False, country_code=self.country_code
            )
            
            # Building clustering
            # 0. Check for existing transformers from OSM
            transformers = self.dbc.get_included_transformers(kcid)

            # Case 1: No transformers present
            if not transformers:
                self.logger.debug(f"kcid{kcid} has no included transformer")
                remaining = self.dbc.count_kmean_cluster_consumers(kcid)
                if remaining > 1:
                    # Create greenfield building clusters
                    self.dimension_bcid_for_kcid(self.plz, kcid)
                    self.logger.debug(f"kcid{kcid} building clusters finished")
                elif remaining > 0:
                    assigned = self.dbc.assign_isolated_building_to_bcid(
                        self.plz, kcid, self.country_code
                    )
                    self.logger.debug(
                        "kcid%s singleton handling finished (%s consumer vertex assigned).",
                        kcid,
                        assigned,
                    )
                else:
                    self.logger.debug(f"kcid{kcid} has no remaining consumers after preprocessing.")

            # Case 2: Transformers present
            else:
                self.logger.debug(f"kcid{kcid} has {len(transformers)} transformers")
                # Create brownfield building clusters with existing transformers
                self.position_brownfield_transformers(self.plz, kcid, transformers)

                # Check buildings and manage clusters
                remaining = self.dbc.count_kmean_cluster_consumers(kcid)
                if remaining > 1:
                    self.dimension_bcid_for_kcid(self.plz, kcid)
                elif remaining > 0:
                    # Assign isolated building(s) to bcid=0 and create a grid_result entry.
                    self.dbc.assign_isolated_building_to_bcid(self.plz, kcid, self.country_code)
                self.logger.debug("Remaining building clustering finished")

            # Process unfinished clusters
            for bcid in self.dbc.get_greenfield_bcids(self.plz, kcid, self.country_code):
                # Transformer positioning for greenfield clusters
                if bcid >= 0:
                    self.position_greenfield_transformers(self.plz, kcid, bcid)
                    self.logger.debug(f"Transformer positioning for kcid{kcid}, bcid{bcid} finished")
                    self.dbc.update_transformer_rated_power(self.plz, kcid, bcid, 1, self.country_code)
                    self.logger.debug("Transformer_rated_power in grid_result updated.")

    def dimension_bcid_for_kcid(self, plz: int, kcid: int) -> None:
        """
        Create building clusters (bcids) with average linkage method for a given kcid.
        :param plz: Postal code
        :param kcid: K-means cluster ID
        :return: None
        """
        self.current_stage = f"dimension_bcid_for_kcid:kcid={kcid}"
        self.current_stage_started_at = time.time()

        # Get data needed for clustering
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self._get_consumer_categories_cached()
        transformer_capacities, _ = self._get_transformer_data_cached(plz)
        # Use the two largest available transformers
        double_trans = np.multiply(transformer_capacities[-2:], 2)

        # Get distance matrix and prepare for hierarchical clustering
        localid2vid, dist_mat, vid2localid = self.dbc.get_distance_matrix_from_kcid(kcid)

        if dist_mat.ndim != 2 or dist_mat.shape[0] != dist_mat.shape[1]:
            self.logger.error(
                "Invalid distance matrix shape for kcid=%s, plz=%s: shape=%s",
                kcid,
                plz,
                getattr(dist_mat, "shape", None),
            )
            raise ValueError(f"Distance matrix has invalid shape for kcid={kcid}, plz={plz}")

        if dist_mat.size > 0:
            if not np.all(np.isfinite(dist_mat)):
                invalid_count = int(np.size(dist_mat) - np.count_nonzero(np.isfinite(dist_mat)))
                self.logger.error(
                    "Non-finite entries in distance matrix for kcid=%s, plz=%s: invalid_count=%s",
                    kcid,
                    plz,
                    invalid_count,
                )
                raise ValueError(f"Distance matrix contains non-finite values for kcid={kcid}, plz={plz}")

            asym = np.abs(dist_mat - dist_mat.T)
            asym_max = float(np.max(asym))
            if asym_max > 1e-9:
                max_idx = np.unravel_index(np.argmax(asym), asym.shape)
                i, j = int(max_idx[0]), int(max_idx[1])
                vi = int(localid2vid.get(i, i))
                vj = int(localid2vid.get(j, j))
                self.logger.error(
                    "Asymmetric distance matrix for kcid=%s, plz=%s: max_delta=%.6f at local=(%s,%s) vertex=(%s,%s), d_ij=%.6f, d_ji=%.6f",
                    kcid,
                    plz,
                    asym_max,
                    i,
                    j,
                    vi,
                    vj,
                    float(dist_mat[i, j]),
                    float(dist_mat[j, i]),
                )
                raise ValueError(
                    f"Distance matrix is not symmetric for kcid={kcid}, plz={plz}, "
                    f"max_delta={asym_max:.6f} at vertices ({vi},{vj})"
                )

        dist_vector = squareform(dist_mat)

        if len(dist_vector) == 0:
            # Consumers sharing one connection point give no pair to cluster; they
            # become one bcid. Ported from tum-ens/pylovo@2503a3e.
            vertices = sorted(buildings["connection_point"].dropna().astype(int).unique().tolist())
            if len(vertices) == 1:
                total_sim_load = utils.simultaneousPeakLoad(buildings, consumer_cat_df, vertices)
                required_kva = utils.required_apparent_power_kva(
                    total_sim_load, DEFAULT_POWER_FACTOR, TRANSFORMER_LOADING_MARGIN
                )
                feasible = transformer_capacities[transformer_capacities >= required_kva]
                transformer_size = int(feasible[0]) if len(feasible) else int(math.ceil(required_kva))
                self.dbc.clear_grid_result_in_kmean_cluster(
                    plz, kcid, only_greenfield=False, country_code=self.country_code
                )
                self.dbc.upsert_bcid(
                    plz, kcid, 1, vertices=vertices,
                    transformer_rated_power=transformer_size, country_code=self.country_code,
                )
                self.logger.info(
                    "Single connection point in PLZ %s KCID %s assigned to BCID 1 with %s kVA",
                    plz, kcid, transformer_size,
                )
                return
            self.logger.warning(
                "Skipped BCID dimensioning for PLZ %s KCID %s: no routable pair among %s connection points",
                plz, kcid, len(vertices),
            )
            return

        # Initialize hierarchical clustering
        Z = linkage(dist_vector, method="average")
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = 1
        new_localid2vid = localid2vid

        # Iterative clustering process
        while True:
            # Try clustering with current parameters
            invalid_cluster_dict, cluster_dict, _ = self.dbc.load_constrained_hierarchical_clustering(Z, cluster_amount, new_localid2vid, buildings,
                                                                                                      consumer_cat_df, transformer_capacities,
                                                                                                      double_trans)
            if cluster_dict:
                cluster_dict, invalid_capacity_dict = self._validate_cluster_transformer_assignments(
                    buildings,
                    consumer_cat_df,
                    cluster_dict,
                )
                if invalid_capacity_dict:
                    invalid_cluster_dict.update(invalid_capacity_dict)

            # Process valid clusters
            if cluster_dict:
                current_valid_amount = len(valid_cluster_dict)
                valid_cluster_dict.update({x + current_valid_amount: y for x, y in cluster_dict.items()})
                valid_cluster_dict = dict(enumerate(valid_cluster_dict.values()))  # reindexing the dict with enumerate

            # Process invalid clusters
            if invalid_cluster_dict:
                current_invalid_amount = len(invalid_trans_cluster_dict)
                invalid_trans_cluster_dict.update(
                    {x + current_invalid_amount: y for x, y in invalid_cluster_dict.items()})
                invalid_trans_cluster_dict = dict(enumerate(invalid_trans_cluster_dict.values()))

            # Check if clustering is complete
            if not invalid_trans_cluster_dict:
                self.logger.info(
                    f"Found {len(valid_cluster_dict)} single transformer clusters for KCID: {kcid} (postcode: {plz})"
                )
                break
            else:
                # Process too large clusters by re-clustering them
                self.logger.info(
                    f"Found {len(invalid_trans_cluster_dict)} too_large clusters for PLZ: {plz}, KCID: {kcid}"
                )

                # Get buildings from the first too-large cluster for re-clustering
                invalid_vertice_ids = list(invalid_trans_cluster_dict[0])
                invalid_local_ids = [vid2localid[v] for v in invalid_vertice_ids]

                # Create new mappings and distance matrix for the subclustering
                new_localid2vid = {k: v for k, v in localid2vid.items() if k in invalid_local_ids}
                new_localid2vid = dict(enumerate(new_localid2vid.values()))
                new_dist_mat = dist_mat[invalid_local_ids][:, invalid_local_ids]
                new_dist_vector = squareform(new_dist_mat)

                # Prepare for next iteration
                Z = linkage(new_dist_vector, method="average")
                cluster_amount = 2
                del invalid_trans_cluster_dict[0]
                invalid_trans_cluster_dict = dict(enumerate(invalid_trans_cluster_dict.values()))

        # At this point, a valid clustering solution (minimum number of transformers) was found.
        # Each cluster:
        #   1. Contains buildings that can be supplied by a single transformer
        #   2. Has an appropriately sized transformer assigned
        # The valid_cluster_dict maps building cluster IDs to tuples of (building_vertices_list, optimal_transformer_size)
        # We could calculate the total transformer cost by summing the costs of all selected transformers:
        # total_transformer_cost = sum([transformer2cost[v[1]] for v in valid_cluster_dict.values()])

        # Reorder bcids for consistency
        valid_cluster_dict = self._order_clusters_by_min_vertice(valid_cluster_dict)

        # Save results to database
        self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid, country_code=self.country_code)
        for bcid, cluster_data in valid_cluster_dict.items():
            self.dbc.upsert_bcid(plz, kcid, bcid, vertices=cluster_data[0],
                                         transformer_rated_power=cluster_data[1], country_code=self.country_code)

        self.logger.debug(f"bcids for plz {plz} kcid {kcid} found...")

    def _order_clusters_by_min_vertice(self, cluster_dict: dict) -> dict:
        """
        Helper to reassign bcids based on smallest vertex ID of each cluster
        for consistent ordering across equivalent partitions.
        Helper function to reassign bcids of the given building clusters ordered by the smallest vertice IDs of the clusters.
        Returns the same result for cluster distributions that are equivalent up to renaming.
        :param cluster_dict: input clusters
        :return: reordered clusters
        """
        ordered_vertices = sorted(cluster_dict.items(), key = lambda cluster: min(cluster[1][0]))
        return {new_bcid: vertices for new_bcid, (_, vertices) in enumerate(ordered_vertices, start=1)}

    def position_brownfield_transformers(self, plz: int, kcid: int, transformer_list: list) -> None:
        """
        Assign buildings to the existing transformers and store them as bcid in buildings_tem.
        Args:
            plz: Postal code
            kcid: K-means cluster ID
            transformer_list: List of transformer IDs
        """
        self.logger.info(f"{len(transformer_list)} Transformers found for kcid {kcid}")

        # Get cost dataframe between consumers and transformers
        cost_df = self.dbc.get_consumer_to_transformer_df(kcid, transformer_list)

        # Filter out connections with distance >= 800
        cost_df = cost_df[cost_df["agg_cost"] < MAX_BROWNFIELD_TRAFO_DISTANCE].sort_values(by=["agg_cost"])

        # Get available transformer capacities from database
        possible_transformers, _ = self._get_transformer_data_cached(plz)

        # Pre-fetch all building loads for this kcid to avoid per-assignment SQL queries
        building_loads = self.dbc.prefetch_building_loads_for_kcid(kcid)

        # Initialize tracking variables
        pre_result_dict = {transformer_id: [] for transformer_id in transformer_list}
        full_transformer_list = []
        assigned_consumer_list = []

        # Assign consumers to closest transformer
        for _, row in cost_df.iterrows():
            start_consumer_id = row["start_vid"]
            end_transformer_id = row["end_vid"]

            # Skip if consumer already assigned or transformer full or transformer not in our list
            if start_consumer_id in assigned_consumer_list or end_transformer_id in full_transformer_list:
                continue
            if end_transformer_id not in pre_result_dict:
                continue

            # Try to assign consumer to transformer
            pre_result_dict[end_transformer_id].append(int(start_consumer_id))
            sim_load_kw = float(self.dbc.calculate_sim_load_from_cache(pre_result_dict[end_transformer_id], building_loads))
            required_kva = self._required_transformer_capacity_kva(sim_load_kw)

            if required_kva > max(possible_transformers):
                # Remove consumer and mark transformer as full
                pre_result_dict[end_transformer_id].pop()
                full_transformer_list.append(end_transformer_id)

                # Exit if all transformers are full
                if len(full_transformer_list) == len(transformer_list):
                    self.logger.debug("All transformers full")
                    break
            else:
                # Mark consumer as assigned
                assigned_consumer_list.append(start_consumer_id)

        self.logger.info("Transformer selection finished")

        # Create building clusters for each transformer
        building_cluster_count = 0

        for transformer_id in transformer_list:
            # Skip empty transformers
            if not pre_result_dict[transformer_id]:
                self.logger.debug(f"Transformer {transformer_id} has no assigned consumer, deleted")
                self.dbc.delete_transformers_from_buildings_tem([transformer_id])
                continue

            # Create building cluster with sequential negative ID
            building_cluster_count -= 1

            # Calculate the simulated load for all loads assigned to this transformer
            sim_load_kw = float(self.dbc.calculate_sim_load_from_cache(pre_result_dict[transformer_id], building_loads))
            required_kva = self._required_transformer_capacity_kva(sim_load_kw)

            # Select the smallest transformer that is larger than the simulated load
            candidates = possible_transformers[possible_transformers >= required_kva]
            if len(candidates) > 0:
                transformer_rated_power = candidates[0].item()
            else:
                transformer_rated_power = possible_transformers[-1].item()  # use largest available

            # Update database with new building cluster
            self.dbc.update_building_cluster(transformer_id, pre_result_dict[transformer_id], building_cluster_count, kcid,
                plz, transformer_rated_power, self.country_code)

        self.logger.info("Brownfield clusters completed")


    def position_greenfield_transformers(self, plz, kcid, bcid):
        """
        Positions a transformer at the optimal location for a greenfield building cluster.

        The optimal location minimizes the sum of distance*load from each vertex to others.

        Args:
            plz: Postcode
            kcid: Kmeans cluster ID
            bcid: Building cluster ID
        """
        # Get all connection points in the building cluster
        connection_points = self.dbc.get_building_connection_points_from_bc(kcid, bcid)
        used_ont_vertices = self.dbc.get_used_ont_vertices(plz, self.country_code)

        # If there's only one connection point, use it
        if len(connection_points) == 1:
            ont_connection_id = int(connection_points[0])
            if ont_connection_id in used_ont_vertices:
                self.logger.warning(
                    "Greenfield ONT reuse unavoidable for PLZ=%s KCID=%s BCID=%s at vertex %s (single candidate).",
                    plz,
                    kcid,
                    bcid,
                    ont_connection_id,
                )
            self.dbc.upsert_transformer_selection(
                plz, kcid, bcid, ont_connection_id, self.country_code
            )
            return

        # Get distance matrix between all connection points
        localid2vid, dist_mat, _ = self.dbc.get_distance_matrix_from_bcid(kcid, bcid)

        # Get load vector for each connection point
        loads = self.dbc.generate_load_vector(kcid, bcid)

        # Calculate weighted distance (distance * load) for each potential location
        total_load_per_vertice = dist_mat.dot(loads)

        # Select the best available point. Prefer unused ONT vertices to avoid stacked transformers.
        sorted_localids = np.argsort(total_load_per_vertice)
        ont_connection_id = None
        for local_id in sorted_localids:
            candidate_vid = int(localid2vid[int(local_id)])
            if candidate_vid not in used_ont_vertices:
                ont_connection_id = candidate_vid
                break

        if ont_connection_id is None:
            # All candidates already used by other clusters in this PLZ; reuse is unavoidable.
            best_localid = int(sorted_localids[0])
            ont_connection_id = int(localid2vid[best_localid])
            self.logger.warning(
                "Greenfield ONT reuse unavoidable for PLZ=%s KCID=%s BCID=%s; all %s candidates already used.",
                plz,
                kcid,
                bcid,
                len(sorted_localids),
            )
        else:
            best_localid = int(sorted_localids[0])
            best_vid = int(localid2vid[best_localid])
            if ont_connection_id != best_vid:
                self.logger.debug(
                    "Selected alternate ONT vertex for PLZ=%s KCID=%s BCID=%s to avoid reuse: best=%s, chosen=%s.",
                    plz,
                    kcid,
                    bcid,
                    best_vid,
                    ont_connection_id,
                )

        # Update the database with the selected transformer position
        self.dbc.upsert_transformer_selection(
            plz, kcid, bcid, ont_connection_id, self.country_code
        )

        self.logger.info("Greenfield clusters completed")

    def prepare_vertices_list(self, plz: int, kcid: int, bcid: int) -> tuple[
        dict, int, list, pd.DataFrame, pd.DataFrame, list, list]:
        vertices_dict, ont_vertice = self.dbc.get_vertices_from_bcid(plz, kcid, bcid, self.country_code)
        vertices_list = list(vertices_dict.keys())

        buildings_df = self.dbc.get_buildings_from_bcid(plz, kcid, bcid)
        consumer_df = self._get_consumer_categories_cached()
        consumer_list = buildings_df.vertice_id.to_list()
        consumer_list = list(dict.fromkeys(consumer_list))  # removing duplicates

        connection_nodes = [i for i in vertices_list if i not in consumer_list]

        return (vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df, consumer_list, connection_nodes,)

    def _required_transformer_capacity_kva(self, diversified_real_kw: float) -> float:
        return float(
            utils.required_apparent_power_kva(
                diversified_real_kw,
                DEFAULT_POWER_FACTOR,
                TRANSFORMER_LOADING_MARGIN,
            )
        )

    def _required_line_current_ka(self, diversified_real_kw: float) -> float:
        return float(
            utils.required_line_current_ka(
                diversified_real_kw,
                VN,
                DEFAULT_POWER_FACTOR,
                V_BAND_LOW,
            )
        )

    def _validate_cluster_transformer_assignments(
        self,
        buildings_df: pd.DataFrame,
        consumer_df: pd.DataFrame,
        cluster_dict: dict,
    ) -> tuple[dict, dict]:
        """Recheck final cluster sizing before persisting BCIDs."""
        valid_clusters = {}
        invalid_clusters = {}
        for cluster_id, cluster_data in cluster_dict.items():
            vertices, transformer_rated_power = cluster_data
            diversified_kw = float(utils.simultaneousPeakLoad(buildings_df, consumer_df, vertices))
            required_kva = self._required_transformer_capacity_kva(diversified_kw)
            if float(transformer_rated_power) + 1e-9 < required_kva:
                invalid_clusters[cluster_id] = vertices
                self.logger.warning(
                    "Rejecting oversized BCID candidate for PLZ=%s: cluster=%s buildings=%s "
                    "diversified_kw=%.2f required_kva=%.2f assigned_kva=%s",
                    self.plz,
                    cluster_id,
                    len(vertices),
                    diversified_kw,
                    required_kva,
                    transformer_rated_power,
                )
            else:
                valid_clusters[cluster_id] = cluster_data
        return valid_clusters, invalid_clusters

    def get_building_simultaneous_load_dict(self, consumer_list: list, buildings_df: pd.DataFrame,
            consumer_df: pd.DataFrame) -> tuple[
        dict, dict, dict]:
        sim_load_per_building = {consumer: 0 for consumer in consumer_list}  # dict of all vertices in bc, 0 as default
        load_units = {consumer: 0 for consumer in consumer_list}
        load_type = {consumer: "yes" for consumer in consumer_list}

        for row in buildings_df.itertuples():
            load_units[row.vertice_id] = row.households_per_building
            load_type[row.vertice_id] = row.f_class

        diversified_kw_by_consumer, _ = utils.diversifiedLoadPerConsumer(
            buildings_df,
            consumer_df,
            consumer_ids=consumer_list,
            consumer_id_col="vertice_id",
        )
        for consumer, diversified_kw in diversified_kw_by_consumer.items():
            sim_load_per_building[consumer] = float(diversified_kw) * 1e-3

        return sim_load_per_building, load_units, load_type

    def _get_unreachable_consumers(self, consumer_list: list, backend, vertices_dict: dict) -> list:
        """Find consumers that have buses but no cables connected.
        
        These are buildings that couldn't be reached via the road network.
        
        Args:
            consumer_list: List of all consumer vertex IDs
            backend: Electrical backend instance
            vertices_dict: Dict of vertex -> cost (may include unreachable buildings)
            
        Returns:
            List of unreachable consumer vertex IDs
        """
        unreachable = []
        for consumer in consumer_list:
            bus_name = f"Consumer Nodebus {consumer}"
            # Check if this bus has any lines connected to it
            if backend.get_connected_lines_count(bus_name) == 0:
                unreachable.append(consumer)
        return unreachable


    def find_furthest_node_path_list(self, connection_node_list: list, vertices_dict: dict, ont_vertice: int) -> list:
        connection_node_dict = {n: vertices_dict[n] for n in connection_node_list}
        furthest_node = max(connection_node_dict, key=connection_node_dict.get)
        # Use batch routing to get path from furthest node to transformer
        all_paths = self.dbc.get_paths_to_bus([furthest_node], ont_vertice)
        furthest_node_path_list = all_paths.get(furthest_node, [])
        if not furthest_node_path_list:
            furthest_node_path_list = self.dbc.get_path_to_bus(furthest_node, ont_vertice)
        furthest_node_path = [p for p in furthest_node_path_list if p in connection_node_list]

        return furthest_node_path


    def determine_maximum_load_branch(self, furthest_node_path_list: list, buildings_df: pd.DataFrame,
            consumer_df: pd.DataFrame, node_loads: dict = None, sim_factors: dict = None) -> tuple[list, float]:
        """
        Determine the longest feasible branch (in order from transformer to furthest node)
        limited by maximum allowable current.
        
        This method implements the primary constraint for cable dimensioning: current capacity.
        It builds branches by adding nodes one by one until the current limit is reached.
        
        Args:
            furthest_node_path_list: List of nodes from transformer to furthest node
            buildings_df: DataFrame with building load information
            consumer_df: DataFrame with consumer category information
            node_loads: Precomputed per-node loads from utils.precompute_node_loads()
            sim_factors: Precomputed sim factors from utils._get_sim_factors()
            
        Returns:
            tuple: (branch_node_list, Imax) - List of nodes in the branch and maximum current
        """
        branch_node_list = []
        # Use incremental accumulation instead of recalculating from scratch each iteration
        cumulative_loads = {}  # {parent_cat: [total_power, total_count]}

        for node in furthest_node_path_list:
            branch_node_list.append(node)

            # Incrementally add this node's loads
            if node_loads and node in node_loads:
                for cat, (power, count) in node_loads[node].items():
                    if cat in cumulative_loads:
                        cumulative_loads[cat] = (cumulative_loads[cat][0] + power, cumulative_loads[cat][1] + count)
                    else:
                        cumulative_loads[cat] = (power, count)

            if node_loads is not None and sim_factors is not None:
                sim_load = utils.incrementalSimLoad(cumulative_loads, sim_factors)
            else:
                # Fallback for callers that do not provide precomputed caches
                sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)
            Imax = self._required_line_current_ka(sim_load)

            if Imax >= MAX_CABLE_CURRENT_KA and len(branch_node_list) > 1:
                # Remove the last node and undo its contribution
                branch_node_list.remove(node)
                if node_loads and node in node_loads:
                    for cat, (power, count) in node_loads[node].items():
                        if cat in cumulative_loads:
                            cumulative_loads[cat] = (cumulative_loads[cat][0] - power, cumulative_loads[cat][1] - count)
                break
            elif Imax >= MAX_CABLE_CURRENT_KA and len(branch_node_list) == 1:
                break

        # Calculate final current for the selected branch
        if node_loads is not None and sim_factors is not None:
            sim_load = utils.incrementalSimLoad(cumulative_loads, sim_factors)
        else:
            sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)
        Imax = self._required_line_current_ka(sim_load)

        return branch_node_list, Imax

    def install_cables(self):
        """
        Installs electrical cables using the electrical backend pattern.

        The algorithm works as follows:
        1. Retrieves all clusters (kcid, bcid) for the postal code area
        2. For each cluster:
           a. Prepares building and connection data
           b. Creates an electrical network via backend
           c. Adds buses, transformers, and loads using ComponentSpecs
           d. Installs cables using the same branch-by-branch greedy algorithm
        3. Tracks progress and saves the network configurations

        Returns:
            None
        """
        # Get all clusters for the postal code area
        cluster_list = self.dbc.get_list_from_plz(self.plz, self.country_code)
        if TESTING:
            cluster_list = cluster_list[:5]  # Limit to first 5 clusters for testing
        ci_count = 0
        ci_process = 0

        for id in cluster_list:
            kcid, bcid = id
            self.current_stage = f"install_cables:kcid={kcid},bcid={bcid}"
            self.current_stage_started_at = time.time()
            self.logger.info(f"Start cable installation for PLZ {self.plz} kcid {kcid} bcid {bcid}")

            # Get data for this cluster
            vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df, consumer_list, connection_nodes = (
                self.prepare_vertices_list(self.plz, kcid, bcid)
            )
            # Strict mode: keep only network-reachable consumers for cable layout.
            reachable_vertices = set(vertices_list)
            reachable_consumers = [c for c in consumer_list if c in reachable_vertices]
            dropped_consumers = len(consumer_list) - len(reachable_consumers)
            if dropped_consumers > 0:
                self.logger.warning(
                    "Skipping %s unreachable consumers in cluster kcid=%s, bcid=%s (no routing path).",
                    dropped_consumers,
                    kcid,
                    bcid,
                )
                buildings_df = buildings_df[buildings_df["vertice_id"].isin(reachable_consumers)].copy()
            consumer_list = reachable_consumers
            connection_nodes = [i for i in vertices_list if i not in consumer_list]

            if not consumer_list:
                self.logger.warning(
                    "No reachable consumers for cluster kcid=%s, bcid=%s; skipping cable installation.",
                    kcid,
                    bcid,
                )
                continue

            sim_load_per_building, load_units, load_type = self.get_building_simultaneous_load_dict(
                consumer_list,
                buildings_df,
                consumer_df,
            )

            # Precompute per-node loads once for O(n) incremental branch calculation
            node_loads = utils.precompute_node_loads(buildings_df, consumer_df)
            sim_factors = utils._get_sim_factors(consumer_df)

            # Initialize backend using configuration
            backend = create_backend(ELECTRICAL_BACKEND, logger=self.logger)
            circuit_name = f"PLZ{self.plz}_kcid{kcid}_bcid{bcid}"
            backend.initialize_circuit(name=circuit_name, source_bus="MVbus 1", primary_kv=20.0)
            # Fetch cables once from database (single source of truth)
            cables = self.dbc.fetch_cables()

            # Register cable types from equipment data
            backend.register_cable_types(cables)

            # Get available cable
            all_available_cables = backend.get_cable_types()
            if not all_available_cables:
                all_available_cables = [cable[0] for cable in cables]

            local_length_dict = {c: 0 for c in all_available_cables}

            # Create cable installer
            installer = CableInstaller(backend, self.dbc, self.logger, cables)
            
            # Create network components
            installer.create_lvmv_bus(self.plz, kcid, bcid, self.country_code)
            installer.create_transformer(self.plz, kcid, bcid, self.country_code)
            installer.create_connection_bus(connection_nodes)
            installer.create_consumer_bus_and_load(consumer_list, sim_load_per_building, buildings_df, load_type)

            trafo_power = self.dbc.get_transformer_rated_power_from_bcid(self.plz, kcid, bcid, self.country_code)
            self.logger.info(
                f"Backend network initialized (buses={backend.get_component_count('buses')}, "
                f"loads={backend.get_component_count('loads')}, transformer_rated_power={trafo_power} kVA)"
            )

            # Install cables branch by branch (same logic as original)
            branch_deviation = 0
            connection_node_list = connection_nodes
            branch_index = 0

            while connection_node_list:
                self.current_stage = (
                    f"install_cables:kcid={kcid},bcid={bcid},branch={branch_index},remaining={len(connection_node_list)}"
                )
                self.current_stage_started_at = time.time()
                # Handle single remaining node case
                if len(connection_node_list) == 1:
                    remaining = connection_node_list[0]
                    self.logger.debug(
                        f"Final remaining connection node {remaining} (kcid={kcid}, bcid={bcid}); installing direct connection."
                    )
                    sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, connection_node_list)
                    Imax = self._required_line_current_ka(sim_load)

                    # Install consumer cables
                    local_length_dict = installer.install_consumer_cables(
                        self.plz, bcid, kcid, branch_deviation, connection_node_list,
                        ont_vertice, vertices_dict, sim_load_per_building, CONSUMER_CONNECTION_AVAILABLE_CABLES,
                        local_length_dict, self.country_code,
                    )

                    # Connect to transformer
                    if connection_node_list[0] == ont_vertice:
                        cable, count = installer.find_minimal_available_cable(Imax)
                        installer.create_line_ont_to_lv_bus(
                            self.plz, bcid, kcid, connection_node_list[0], branch_deviation, cable, count, ont_vertice,
                            self.country_code,
                        )
                    else:
                        cable, count = installer.find_minimal_available_cable(
                            Imax, vertices_dict[connection_node_list[0]]
                        )
                        length = installer.create_line_start_to_lv_bus(
                            self.plz, bcid, kcid, connection_node_list[0], branch_deviation,
                            vertices_dict, cable, count, ont_vertice, self.country_code
                        )
                        local_length_dict[cable] += length
                        self.logger.info(
                            f"Final branch backbone installed (PLZ={self.plz}, kcid={kcid}, bcid={bcid}, "
                            f"start_node={connection_node_list[0]}, cable={cable}, parallels={count}, length_km={length:.4f})"
                        )
                    break
                furthest_node_path_list = self.find_furthest_node_path_list(
                    connection_node_list, vertices_dict, ont_vertice
                )
                branch_node_list, Imax = self.determine_maximum_load_branch(
                    furthest_node_path_list, buildings_df, consumer_df, node_loads, sim_factors
                )
                self.logger.debug(
                    f"Selected branch {branch_index} (nodes={len(branch_node_list)}, first={branch_node_list[0]}, "
                    f"last={branch_node_list[-1]}, Imax={Imax:.3f} kA)"
                )

                # Install cables for this branch
                local_length_dict = installer.install_consumer_cables(
                    self.plz, bcid, kcid, branch_deviation, branch_node_list,
                    ont_vertice, vertices_dict, sim_load_per_building, CONSUMER_CONNECTION_AVAILABLE_CABLES,
                    local_length_dict, self.country_code
                )

                # Select appropriate cable and connect nodes
                branch_distance = vertices_dict[branch_node_list[0]]
                cable, count = installer.find_minimal_available_cable(
                    Imax, branch_distance
                )

                if len(branch_node_list) >= 2:
                    local_length_dict = installer.create_line_node_to_node(
                        self.plz, kcid, bcid, branch_node_list, branch_deviation,
                        vertices_dict, local_length_dict, cable, ont_vertice, count, self.country_code
                    )

                # Connect branch to transformer
                branch_start_node = branch_node_list[-1]
                if branch_start_node == ont_vertice:
                    installer.create_line_ont_to_lv_bus(
                        self.plz, bcid, kcid, branch_start_node, branch_deviation, cable, count, ont_vertice,
                        self.country_code,
                    )
                    self.logger.debug(
                        f"Branch {branch_index} connected directly to transformer (cable={cable}, parallels={count})."
                    )
                else:
                    length = installer.create_line_start_to_lv_bus(
                        self.plz, bcid, kcid, branch_start_node, branch_deviation,
                        vertices_dict, cable, count, ont_vertice, self.country_code
                    )
                    local_length_dict[cable] += length
                    self.logger.debug(
                        f"Branch {branch_index} connected to LV bus (cable={cable}, parallels={count}, length_km={length:.4f})."
                    )

                # Update processed nodes and visualization
                for vertice in branch_node_list:
                    connection_node_list.remove(vertice)

                installer.deviate_bus_geodata(branch_node_list, branch_deviation)
                branch_deviation += 1
                branch_index += 1

            # Cluster summary
            total_length = sum(local_length_dict.values())
            used_cables = {k: v for k, v in local_length_dict.items() if v > 0}
            if used_cables:
                cable_summary = ", ".join([f"{k}:{v:.3f} km" for k, v in sorted(used_cables.items(), key=lambda x: -x[1])])
            else:
                cable_summary = "no cables installed"

            lines_count = backend.get_component_count('lines')
            self.logger.info(
                f"Finished cluster kcid={kcid}, bcid={bcid}: branches={branch_index}, lines={lines_count}, "
                f"total_length={total_length:.3f} km ({cable_summary})"
            )

            # Track and report progress
            ci_count += 1
            progress_increment = 10  # Report progress in 10% increments
            progress_threshold = max(1, len(cluster_list) / progress_increment)

            if ci_count >= progress_threshold:
                ci_process += progress_increment
                ci_count = 0
                self.logger.info(
                    f"Cable installation: {min(ci_process, 100)}% complete ({ci_process // progress_increment}/{progress_increment})"
                )

            self.dbc.flush_lines()
            # Release DB transaction before CPU-heavy power-flow solve to avoid
            # long "idle in transaction" sessions during in-memory calculations.
            self.dbc.commit_changes()
            self.save_net(backend, kcid, bcid)
            # Persist net metadata insert/update done in save_net immediately.
            self.dbc.commit_changes()

    def save_net(self, backend: IElectricalBackend, kcid, bcid):
        """
        Validate and save grid to file and database using backend pattern.
        """
        # Validate grid with power flow before saving
        try:
            converged = backend.solve_power_flow()
            if converged:
                self.logger.info(f"[OK] Power flow converged for kcid={kcid}, bcid={bcid}")
            else:
                self.logger.warning(f"[WARN] Power flow did NOT converge for kcid={kcid}, bcid={bcid}")
        except Exception as e:
            self.logger.warning(f"[WARN] Power flow failed for kcid={kcid}, bcid={bcid}: {e}")

        if SAVE_GRID_FOLDER:
            savepath_folder = Path(RESULT_DIR, "grids", f"version_{VERSION_ID}", str(self.plz))
            savepath_folder.mkdir(parents=True, exist_ok=True)
            filename = f"kcid{kcid}bcid{bcid}.json"
            savepath_file = Path(savepath_folder, filename)
            backend.export_to_format(filename=savepath_file)

        json_string = backend.export_to_format(filename=None)

        metrics = backend.get_circuit_metrics()
        actual_transformer_capacity = metrics.get("total_transformer_capacity_kva")
        if actual_transformer_capacity is not None:
            persisted_capacity = self.dbc.get_transformer_rated_power_from_bcid(self.plz, kcid, bcid, self.country_code)
            exact_capacity = int(round(float(actual_transformer_capacity)))
            if exact_capacity != int(persisted_capacity):
                self.dbc.set_transformer_rated_power_exact(self.plz, kcid, bcid, exact_capacity, self.country_code)

        if ELECTRICAL_BACKEND == "pandapower":
            transformer_description = backend.net.trafo.name[0]
        else:
            transformer_description = "N/A"

        self.dbc.save_pp_net_with_json(self.plz, kcid, bcid, json_string, transformer_description)

        self.logger.info(f"Grid with kcid:{kcid} bcid:{bcid} is stored. ")
