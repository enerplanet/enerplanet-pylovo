import sys
import os
from pathlib import Path
# Add project root and api directory to python path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # api/ directory
sys.path.append(str(Path(__file__).resolve().parent.parent))  # project root for src/

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Pylovo Grid API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Import connection pool and cache
from src.database.connection_pool import init_pool, close_pool
from src.database.database_client import DatabaseClient
from src import cache

# Include modular routers
from routers import reference, pipeline, power_flow, energy, transformer, building, grid, boundary
app.include_router(reference.router)
app.include_router(pipeline.router)
app.include_router(power_flow.router)
app.include_router(energy.router)
app.include_router(transformer.router)
app.include_router(building.router)
app.include_router(grid.router)
app.include_router(boundary.router)

@app.on_event("startup")
async def startup_event():
    """Initialize connection pool and Redis cache on startup."""
    init_pool()
    # Compatibility DDL is serialized here before the worker accepts traffic;
    # request handlers never perform schema changes.
    with DatabaseClient() as dbc:
        grid.ensure_runtime_schema(dbc)
    cache.init_redis()
    worker_id = os.environ.get('WORKER_ID', '?')
    print(f"[Startup] PyLovo API worker {worker_id} ready with connection pooling and Redis cache")

@app.on_event("shutdown")
async def shutdown_event():
    """Close connection pool and Redis on shutdown."""
    close_pool()
    cache.close_redis()
    print("[Shutdown] PyLovo API shutdown complete")

@app.get("/health")
async def health_check():
    """Health check endpoint — must never fail so HAProxy keeps backend in rotation."""
    worker_id = os.environ.get('WORKER_ID', '?')
    try:
        stats = cache.get_stats()
    except Exception:
        stats = {"error": "cache unavailable"}
    return {
        "status": "healthy",
        "worker": worker_id,
        "cache_stats": stats
    }
