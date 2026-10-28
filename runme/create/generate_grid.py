import argparse
import sys
import os
import time
import threading
import traceback
import pandas as pd
import yaml
from multiprocessing import Pool, TimeoutError, Manager

# Setup path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.config_loader import REGION_DICT, TARGET_SCHEMA, ANALYZE_GRIDS, get_country_code
# Import sample module carefully to handle database connection if needed
# But get_municipal_register_as_dataframe uses a global db connection defined at module level
from src.classification.sampling.sample import get_municipal_register_as_dataframe
from src.grid_generator import GridGenerator
from src.database.database_client import DatabaseClient


def load_regions_config():
    """Load regions from datapipeline config."""
    regions_path = os.path.join(PROJECT_ROOT, "datapipeline", "config", "regions.yaml")
    if os.path.exists(regions_path):
        with open(regions_path, 'r') as f:
            return yaml.safe_load(f)
    return None

# Worker-global GridGenerator instance, reused across PLZs in the same worker process
_worker_gg = None
_worker_status = None

def init_worker(country_code, status_dict=None):
    """Pool initializer: create a persistent GridGenerator per worker process."""
    global _worker_gg, _worker_status
    _worker_gg = GridGenerator(country_code=country_code, log_file="generate.log")
    _worker_status = status_dict

def process_plz(args):
    """
    Worker function to process a single PLZ.
    Reuses the worker-global GridGenerator (and its DB connection) for efficiency.
    Args: tuple of (plz, country_code)
    """
    global _worker_gg, _worker_status
    plz, country_code = args
    temp_schema = f"tmp_plz_{plz}"
    gg = _worker_gg
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    sys.stderr.write(f"[START] PLZ {plz}\n")
    sys.stderr.flush()

    def _update_status(state: str, message: str = ""):
        if _worker_status is None:
            return
        stage = "uninitialized"
        stage_elapsed = 0.0
        if gg is not None:
            stage = getattr(gg, "current_stage", "unknown") or "unknown"
            stage_started = getattr(gg, "current_stage_started_at", None)
            if isinstance(stage_started, (int, float)):
                stage_elapsed = max(0.0, time.time() - stage_started)
        try:
            _worker_status[str(plz)] = {
                "pid": os.getpid(),
                "state": state,
                "stage": stage,
                "stage_elapsed_s": round(stage_elapsed, 1),
                "message": message[:200],
                "updated_at": int(time.time()),
            }
        except Exception:
            pass

    try:
        # Create a unique temp schema for this process to isolate temporary tables
        if gg is None:
            # Fallback: create a new GridGenerator if init_worker was not called
            gg = GridGenerator(plz=plz, country_code=country_code, log_file="generate.log")
            _worker_gg = gg

        # Reconnect if the DB connection was lost (e.g. OOM kill, network timeout)
        reused_connection = gg.dbc.ensure_connection(clear_transaction=True)
        if not reused_connection:
            sys.stderr.write(f"[RECONNECT] PLZ {plz}: DB connection lost, reconnecting...\n")
            sys.stderr.flush()

        _update_status("initializing")

        gg.plz = plz
        gg.country_code = country_code

        # Setup search path to prioritize temp schema so that create_temp_tables uses it
        _update_status("prepare_schema")
        with gg.dbc.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {temp_schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {temp_schema}")
            cur.execute(f"SET search_path TO {temp_schema}, {TARGET_SCHEMA}, public")
        gg.dbc.conn.commit()

        def _heartbeat():
            while not heartbeat_stop.wait(5):
                _update_status("running")

        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

        # Generate grid — skip per-PLZ materialized view refresh (done once at end)
        _update_status("generate_grid")
        gg.generate_grid_for_single_plz(
            plz,
            analyze_grids=ANALYZE_GRIDS,
            refresh_mv=False,
            raise_on_error=True,
        )
        if not gg.dbc.is_grid_generated(plz, country_code):
            raise RuntimeError(f"no grid_result rows for PLZ {plz} ({country_code}) after generation")
        _update_status("success")

        return f"PLZ {plz}: Success"
    except Exception as e:
        stage = getattr(gg, "current_stage", "unknown") if gg is not None else "unknown"
        stage_started = getattr(gg, "current_stage_started_at", None) if gg is not None else None
        stage_elapsed = max(0.0, time.time() - stage_started) if isinstance(stage_started, (int, float)) else 0.0
        tb = traceback.format_exc()
        sys.stderr.write(
            f"[FAIL] PLZ {plz} stage={stage} stage_elapsed={stage_elapsed:.1f}s error={e}\n{tb}\n"
        )
        sys.stderr.flush()
        _update_status("failed", f"{stage}: {e}")
        # Rollback but keep connection alive for next PLZ
        try:
            if gg is not None:
                gg.dbc.ensure_connection(clear_transaction=True)
        except Exception:
            pass
        return f"PLZ {plz}: Failed (stage={stage}, stage_elapsed={stage_elapsed:.1f}s) - {str(e)}"
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        # Always cleanup isolated schema and reset search_path for next PLZ on this worker.
        try:
            if gg is not None:
                gg.dbc.ensure_connection(clear_transaction=True)
                with gg.dbc.conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {temp_schema} CASCADE")
                    cur.execute(f"SET search_path TO {TARGET_SCHEMA}, public")
                gg.dbc.conn.commit()
        except Exception:
            try:
                if gg is not None:
                    gg.dbc.ensure_connection(clear_transaction=True)
            except Exception:
                pass
        if _worker_status is not None:
            try:
                _worker_status.pop(str(plz), None)
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(description="Generate grids for a region using multiprocessing")
    parser.add_argument("country", help="Country name (e.g. germany, netherlands)")
    parser.add_argument("state", help="State name (e.g. hamburg, flevoland)")
    parser.add_argument("--worker", type=int, default=1, help="Number of worker processes")
    parser.add_argument("--resume", action="store_true", help="Skip PLZs that already have grid results")
    parser.add_argument("--plz", nargs="+", help="Only generate grids for these PLZs (must belong to the state)")
    
    args = parser.parse_args()
    country = args.country.lower()
    state_name_lower = args.state.lower()
    
    # Load regions config
    regions_config = load_regions_config()
    
    # Check if country is in regions.yaml
    state_id = None
    state_name = None
    country_code = None
    
    if regions_config and country in regions_config:
        # Use datapipeline regions.yaml
        country_config = regions_config[country]
        # Get country code from centralized config
        country_code = get_country_code(country)
        states = country_config.get('states', {})
        
        for skey, sconfig in states.items():
            if skey.lower() == state_name_lower or sconfig.get('name', '').lower() == state_name_lower:
                state_id = skey  # Use key as state identifier
                state_name = sconfig.get('name', skey)
                break
        
        if state_id is None:
            print(f"Error: State '{args.state}' not found for country '{country}'.")
            print("Available states:", list(states.keys()))
            return
    else:
        # Fallback to REGION_DICT (Germany legacy)
        country_code = 'DE'
        for sid, sname in REGION_DICT.items():
            if sname.lower() == state_name_lower:
                state_id = sid
                state_name = sname
                break
                
        if state_id is None:
            print(f"Error: State '{args.state}' not found in configuration.")
            print("Available states:", [v for k,v in REGION_DICT.items()])
            return

    print(f"Generating grids for {country}/{state_name} (country_code: {country_code}) with {args.worker} workers.")
    
    # Initialize version and parameter tables in main process to avoid race conditions
    print("Initializing database version and parameters...")
    try:
        dummy_gg = GridGenerator(plz=0)
        # Add f_classes columns to shared tables ONCE here, before workers spawn.
        # ALTER TABLE requires ACCESS EXCLUSIVE lock — running from multiple workers deadlocks.
        dummy_gg.dbc.ensure_shared_table_columns()
        # Force initialization logic in __init__
        del dummy_gg
    except Exception as e:
        print(f"Warning: Initialization check failed: {e}")

    # Get PLZs from database that have building data (buildings are loaded per state)
    print("Fetching PLZ list from database...")
    from src.database.database_client import DatabaseClient
    dbc = DatabaseClient()
    
    # Get PLZs that have residential or other buildings loaded (the constructor loads buildings per state)
    # This ensures we only process PLZs for which we have building data
    query = """
        SELECT DISTINCT p.plz
        FROM postcode p
        WHERE p.country_code = %s
    """
    params = [country_code]

    res_state_filter = ""
    oth_state_filter = ""
    ways_state_filter = ""
    if state_id:
        query += " AND p.state_code = %s "
        params.append(state_id)
        res_state_filter = " AND r.state_code = %s "
        oth_state_filter = " AND o.state_code = %s "
        ways_state_filter = " AND w.state_code = %s "

    query += f"""
          AND (
            EXISTS (
              SELECT 1 FROM res r
              WHERE r.country_code = %s
                {res_state_filter}
                AND r.geom && p.geom
                AND ST_Intersects(r.geom, p.geom)
              LIMIT 1
            )
            OR EXISTS (
              SELECT 1 FROM oth o
              WHERE o.country_code = %s
                {oth_state_filter}
                AND o.geom && p.geom
                AND ST_Intersects(o.geom, p.geom)
              LIMIT 1
            )
          )
          AND EXISTS (
              SELECT 1 FROM ways w
              WHERE w.country_code = %s
                {ways_state_filter}
                AND w.geom && p.geom
                AND ST_Intersects(w.geom, p.geom)
              LIMIT 1
          )
    """
    params.append(country_code)
    if state_id:
        params.append(state_id)
    params.append(country_code)
    if state_id:
        params.append(state_id)
    params.append(country_code)
    if state_id:
        params.append(state_id)

    with dbc.conn.cursor() as cur:
        cur.execute(query, tuple(params))
        plzs = [row[0] for row in cur.fetchall()]

    if args.plz:
        unknown = sorted(set(args.plz) - set(plzs))
        if unknown:
            print(f"Error: PLZs {unknown} have no building data in {state_name}.")
            return
        plzs = list(args.plz)
    
    # Sort PLZs by population descending so largest areas start first (better load-balancing)
    try:
        with dbc.conn.cursor() as cur:
            cur.execute("""
                SELECT p.plz, MAX(COALESCE(p.population, 0)) as pop
                FROM postcode p WHERE p.plz = ANY(%s)
                GROUP BY p.plz
                ORDER BY pop DESC;
            """, (plzs,))
            plzs = [row[0] for row in cur.fetchall()]
    except Exception:
        plzs = sorted(plzs)  # Fallback to deterministic order
    
    # Resume mode: skip PLZs that already have grid results
    if args.resume:
        with dbc.conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT plz FROM grid_result 
                WHERE country_code = %s
            """, (country_code,))
            done_plzs = set(row[0] for row in cur.fetchall())
        
        before = len(plzs)
        plzs = [p for p in plzs if p not in done_plzs]
        skipped = before - len(plzs)
        if skipped > 0:
            print(f"Resume mode: skipping {skipped} already-completed PLZs, {len(plzs)} remaining.")
    
    print(f"Found {len(plzs)} PLZs with building data in {state_name}.")
    
    if len(plzs) == 0:
        print("No PLZs found. Exiting.")
        return

    # Create argument tuples with (plz, country_code) for each PLZ
    plz_args = [(plz, country_code) for plz in plzs]

    # Parent connection is no longer needed during worker execution.
    # Close it to avoid lingering idle transactions while pool is running.
    try:
        dbc.conn.commit()
    except Exception:
        try:
            dbc.conn.rollback()
        except Exception:
            pass
    dbc.close()

    start_time = time.time()
    heartbeat_seconds = 20

    print("Starting processing...")
    # Use multiprocessing.Pool with initializer to reuse DB connections across PLZs
    manager = Manager()
    status_dict = manager.dict()
    pool = Pool(
        processes=args.worker,
        initializer=init_worker,
        initargs=(country_code, status_dict),
    )
    try:
        # Use imap_unordered for better load-balancing (largest PLZs submitted first)
        results = pool.imap_unordered(process_plz, plz_args)

        # Track completed count (results arrive in completion order, not submission order)
        completed = 0
        failed = []
        while completed < len(plzs):
            try:
                res = results.next(timeout=heartbeat_seconds)
                completed += 1
                if ": Failed" in res:
                    failed.append(res)
                print(f"[{completed}/{len(plzs)}] {res}", flush=True)
            except TimeoutError:
                elapsed = int(time.time() - start_time)
                active = []
                try:
                    now_ts = int(time.time())
                    for active_plz, info in list(status_dict.items()):
                        stage = info.get("stage", "unknown")
                        stage_elapsed = info.get("stage_elapsed_s", 0.0)
                        pid = info.get("pid", "?")
                        age = max(0, now_ts - int(info.get("updated_at", now_ts)))
                        active.append(
                            f"{active_plz}:stage={stage},stage_elapsed={stage_elapsed}s,pid={pid},age={age}s"
                        )
                except Exception:
                    active = []
                suffix = f" | active -> {'; '.join(active)}" if active else ""
                print(
                    f"[{completed}/{len(plzs)}] still processing... elapsed {elapsed}s{suffix}",
                    flush=True,
                )
            
        pool.close()
        pool.join()
        if failed:
            print(f"Completed with failures: {len(failed)} / {len(plzs)}", flush=True)
            for line in failed[:20]:
                print(f"  - {line}", flush=True)
            if len(failed) > 20:
                print(f"  ... and {len(failed) - 20} more", flush=True)
        else:
            print(f"All {len(plzs)} PLZ processed successfully.", flush=True)

    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Terminating workers immediately...")
        pool.terminate()
        pool.join()
        manager.shutdown()
        sys.exit(1)
    except Exception:
        pool.terminate()
        pool.join()
        raise
    finally:
        try:
            manager.shutdown()
        except Exception:
            pass

    # Refresh materialized views once after all workers complete
    dbc = DatabaseClient(country_code=country_code)

    reconcile_flag = os.getenv("RECONCILE_MISSING_BUILDINGS", "0").strip().lower()
    if reconcile_flag not in {"0", "false", "no"}:
        print("Reconciling missing buildings (including out-of-postcode edges)...", flush=True)
        reconciled = dbc.reconcile_missing_buildings_for_postcode_result(
            country_code=country_code,
            state_code=state_name_lower,
            include_out_of_scope=True,
        )
        dbc.commit_changes()
        print(f"Reconciled {reconciled} buildings.", flush=True)

    # Ensure postcode_result.state_code is populated so /boundary/available
    print("Syncing state relationships...", flush=True)
    state_stats = dbc.ensure_state_relationships(
        country=country_code,
        state=state_name_lower,
        state_name=state_name_lower,
    )
    dbc.commit_changes()
    print(f"State sync: {state_stats}", flush=True)

    print("Refreshing materialized views...", flush=True)
    dbc.refresh_materialized_views()
    dbc.commit_changes()
    dbc.close()

    elapsed = time.time() - start_time
    print(f"Finished in {elapsed:.2f} seconds.")

if __name__ == "__main__":
    main()
