"""Site generation jobs: queue, run in a separate process, report status.

Run a queued job with `python -m src.site_mode.jobs <job_id>` from the project root.
"""
import subprocess
import sys
import threading
import traceback

import psycopg2

from src.config_loader import DBNAME, DBUSER, HOST, PASSWORD, PORT, TARGET_SCHEMA
from src.site_mode.errors import SiteNotFound
from src.site_mode.generation import generate_site
from src.site_mode.sites import STALE_JOB_SECONDS, lock_site_for_edit

HEARTBEAT_SECONDS = 15

JOB_SQL = f"""
SELECT job_id, site_id, revision,
       CASE WHEN status IN ('queued', 'running')
             AND COALESCE(heartbeat_at, started_at, created_at)
                 < NOW() - INTERVAL '{STALE_JOB_SECONDS} seconds'
            THEN 'failed' ELSE status END,
       stage,
       CASE WHEN status IN ('queued', 'running')
             AND COALESCE(heartbeat_at, started_at, created_at)
                 < NOW() - INTERVAL '{STALE_JOB_SECONDS} seconds'
            THEN 'interrupted' ELSE error END,
       overloaded_transformer_ids, unroutable_building_ids, started_at, finished_at
FROM site_job WHERE site_id = %s AND job_id = %s
"""


def queue_job(cur, site_id: int) -> dict:
    """Create a queued job for the site's current revision; 409 if one is active."""
    lock_site_for_edit(cur, site_id)
    cur.execute(
        """INSERT INTO site_job (site_id, revision)
           SELECT site_id, revision FROM site WHERE site_id = %s
           RETURNING job_id, revision""",
        (site_id,),
    )
    job_id, revision = cur.fetchone()
    return {"job_id": job_id, "revision": revision}


def start_job_process(job_id: int) -> None:
    """Run the job in a new process so generation never blocks an API worker."""
    subprocess.Popen([sys.executable, "-m", "src.site_mode.jobs", str(job_id)], start_new_session=True)


def get_job(cur, site_id: int, job_id: int) -> dict:
    """Return a job's status; a job without a recent heartbeat reads as failed."""
    cur.execute(JOB_SQL, (site_id, job_id))
    row = cur.fetchone()
    if row is None:
        raise SiteNotFound(f"Job {job_id} not found for site {site_id}")
    keys = ("job_id", "site_id", "revision", "status", "stage", "error",
            "overloaded_transformer_ids", "unroutable_building_ids", "started_at", "finished_at")
    return dict(zip(keys, row))


def _connect():
    """Open a dedicated connection for job bookkeeping, outside the generator's transaction."""
    conn = psycopg2.connect(dbname=DBNAME, user=DBUSER, password=PASSWORD, host=HOST, port=PORT,
                            options=f"-c search_path={TARGET_SCHEMA},public")
    conn.autocommit = True
    return conn


def _heartbeat(job_id: int, holder: dict, stop: threading.Event) -> None:
    """Record the generator's current stage every HEARTBEAT_SECONDS until stopped."""
    with _connect() as conn, conn.cursor() as cur:
        while not stop.wait(HEARTBEAT_SECONDS):
            generator = holder.get("generator")
            stage = generator.current_stage if generator is not None else "preparing"
            cur.execute("UPDATE site_job SET heartbeat_at = NOW(), stage = %s WHERE job_id = %s",
                        (stage[:100], job_id))


def _finish(cur, job_id: int, holder: dict, status: str, result: dict | None, error: str | None) -> None:
    """Store the job's final status, last generation stage and result."""
    result = result or {}
    generator = holder.get("generator")
    stage = generator.current_stage if generator is not None else "preparing"
    cur.execute(
        """UPDATE site_job SET status = %s, stage = %s, error = %s, finished_at = NOW(),
               heartbeat_at = NOW(), overloaded_transformer_ids = %s, unroutable_building_ids = %s
           WHERE job_id = %s""",
        (status, stage[:100], error, result.get("overloaded_transformer_ids"),
         result.get("unroutable_building_ids"), job_id),
    )


def run_job(job_id: int) -> None:
    """Generate the job's site, keeping its heartbeat and final status current."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """UPDATE site_job SET status = 'running', started_at = NOW(), heartbeat_at = NOW()
           WHERE job_id = %s AND status = 'queued' RETURNING site_id, revision""",
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Job {job_id} is not queued")
    holder, stop = {}, threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(job_id, holder, stop), daemon=True)
    beat.start()
    try:
        result = generate_site(row[0], row[1], on_generator=lambda g: holder.update(generator=g))
        _finish(cur, job_id, holder, "succeeded", result, None)
    except Exception as e:
        _finish(cur, job_id, holder, "failed", None, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        stop.set()
        beat.join()
        conn.close()


if __name__ == "__main__":
    run_job(int(sys.argv[1]))
