# pylovo — Synthetic Low-Voltage Grid Generator (Enerplanet fork)

> **pylovo** = **PY**thon tool for **LO**w-**VO**ltage distribution grid generation.

This repository is the **Enerplanet fork** of pylovo, maintained by
[THD-Spatial-AI](https://github.com/THD-Spatial-AI). It builds on the original
[`tum-ens/pylovo`](https://github.com/tum-ens/pylovo) tool from the Technical
University of Munich and extends it into a containerised, API-driven service used
by the Enerplanet energy-planning platform.

> [!NOTE]
> **This is a modified fork, not the original pylovo.**
> We have made substantial changes to the upstream project — most notably a new
> REST API, full Docker/Compose deployment, a database connection pool, a caching
> layer, and several new analysis modules (power flow, EV hosting capacity, AI
> building-energy estimation). The package layout was also flattened from
> `src/pylovo/` to `src/`. For the complete, file-by-file list of what changed,
> see **[CHANGELOG.md](CHANGELOG.md)**. The original tool and its academic
> reference are credited in [Attribution & citation](#attribution--citation).

---

## What is pylovo?

pylovo generates **synthetic low-voltage (LV) distribution grids** for any chosen
research area, using only open, georeferenced public data. It applies graph-based
algorithms, clustering, and grid-planning heuristics to produce realistic,
representative grids where real network data is unavailable or confidential.

**Inputs** (primarily from OpenStreetMap):

- buildings, roads and transformer locations,
- postal-code (PLZ) area polygons to define the research area,
- consumer categories to estimate building/household loads,
- standard infrastructure parameters (cable and transformer catalogues).

**Outputs:**

- a feasible set of aggregated LV grid networks for the selected area, and
- automatically computed grid statistics so the generated grids can be evaluated.

The approach is scalable from a single postal-code area up to cities, states, or
whole countries (hundreds of thousands of grids), and supports multiple feeders
per transformer, greenfield/brownfield transformer placement, and variable
equipment dimensioning.

### What this fork adds on top

- **REST API** (`api/`) — a FastAPI service exposing grid generation, power-flow
  analysis, building-energy estimation, EV hosting capacity, and reference data.
- **Containerised deployment** — `Dockerfile`, `docker-compose*.yml`, HAProxy
  load balancing, and a `Makefile` that wraps every common task.
- **Performance & robustness** — pooled database connections (`src/database/connection_pool.py`),
  a Redis-backed cache with in-memory fallback (`src/cache.py`), and added
  indexes (`migrations/`).
- **New analysis modules** — power flow (`src/analysis/powerflow_analysis.py`),
  EV hosting capacity (`src/ev_hosting.py`), and a rule-based AI energy estimator
  (`src/ai_estimation/`).

---

## Architecture at a glance

```
                       ┌─────────────────────────────┐
   HTTP clients ──────▶│  HAProxy (prod)  :8086      │
                       └──────────────┬──────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              pylovo-api-1      pylovo-api-2      pylovo-api-3   (FastAPI, service api)
                    └─────────────────┼─────────────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  ▼                                        ▼
          PostgreSQL + PostGIS + pgRouting            Redis (cache)
              (pylovo_db)
```

- **Grid pipeline:** `datapipeline` (ingest open data) → `constructor` (build
  base tables) → `grid` (`GridGenerator` → `DatabaseClient`).
- **Database:** PostgreSQL with PostGIS and pgRouting (shortest-path routing for
  cable layout).
- **Cache:** Redis, with automatic fallback to an in-memory store for dev.

---

## Prerequisites

- **Docker** and **Docker Compose** (recommended path — everything runs in
  containers).
- **Git LFS** — large raw datasets are stored via LFS and must be pulled before
  the first build.
- Roughly a few GB of free disk for the raw data and database volume.

---

## Quick start

```bash
# 1. Clone and enter the repository
git clone https://github.com/THD-Spatial-AI/enerplanet-pylovo.git
cd enerplanet-pylovo

# 2. Configure the environment
cp .env.docker .env.docker.local   # then edit credentials/keys as needed
#    Required keys: DBNAME, DBUSER, PASSWORD, HOST, PORT, TARGET_SCHEMA,
#    EP_ONLINE_API_KEY

# 3. One-shot setup: pull LFS data, create the database, build the image
make setup

# 4. Start the service
make dev      # development: single API with hot reload, no load balancer
# or
make prod     # production: 3 API instances behind HAProxy
```

Once running:

- **API:** http://localhost:8086
- **Health check:** http://localhost:8086/health
- **HAProxy stats (prod only):** http://localhost:8404

> [!IMPORTANT]
> `.env.docker` holds database credentials and the `EP_ONLINE_API_KEY`. Never
> commit real secrets — `.env*` files are git-ignored. Keep your filled-in copy
> local only.

---

## Generating grids

The full workflow is **datapipeline → constructor → grid**. You can run the
stages individually or chain them with `make process`.

```bash
# Full pipeline for one state (ingest + construct + generate grids)
make process COUNTRY=germany STATE=hamburg WORKERS=10

# …or run the stages one at a time:
make datapipeline COUNTRY=germany STATE=hamburg   # ingest & enrich open data
make constructor  COUNTRY=germany STATE=hamburg   # build base DB tables
make grid         COUNTRY=germany STATE=hamburg WORKERS=10   # generate grids
```

`WORKERS` controls the number of parallel processes used during grid generation.

### Other common Make targets

| Command | Purpose |
|---|---|
| `make setup` | Full setup: pull LFS, create DB, build image, start |
| `make dev` / `make prod` | Start dev (single API) / prod (3 APIs + HAProxy) |
| `make down` | Stop and remove containers |
| `make shell` | Open an interactive shell inside the container |
| `make logs` | Follow container logs |
| `make status` | Show container status |
| `make enrich COUNTRY=… STATE=…` | Run building enrichment only |
| `make prepare-country COUNTRY=…` | Download & prepare data for a country |
| `make dump-state STATE=…` / `make load-state STATE=…` | Dump / restore a processed state |
| `make delete-dry-run COUNTRY=… STATE=…` | Preview deletion impact for a state |
| `make run CMD="…"` | Run an arbitrary command in the container |
| `make help` | List all available targets |

Run `make help` for the complete, always-current list.

---

## Configuration

Runtime behaviour is driven by:

- **`.env.docker`** — deployment secrets and connection details
  (`DBNAME`, `DBUSER`, `PASSWORD`, `HOST`, `PORT`, `TARGET_SCHEMA`,
  `EP_ONLINE_API_KEY`).
- **`config/*.yaml`** — application configuration: `config_database.yaml`,
  `config_data.yaml`, `config_generation.yaml`, `config_analysis.yaml`,
  `config_plotting.yaml`, `config_version.yaml`.

Plotting uses Mapbox. Tokens are **not** committed — replace the
`YOUR_MAPBOX_TOKEN` placeholder in `config/config_plotting.yaml` (and the
`plotting/` modules) with your own Mapbox access token if you need the map
backgrounds.

---

## Repository layout

| Path | Contents |
|---|---|
| `api/` | FastAPI service: routers, Pydantic models, services, utils |
| `src/` | Core pylovo library (grid generation, database, analysis) |
| `datapipeline/` | Open-data ingestion and enrichment |
| `config/` | YAML configuration and table-structure definitions |
| `migrations/` | SQL database migrations |
| `plotting/` | Grid/network plotting utilities |
| `runme/` | Operational scripts (create / analyze / export / import / delete) |
| `initial-data/` | Bootstrap/seed datasets |
| `raw_data/` | Large input datasets (Git LFS) |
| `docs/` | Documentation sources |
| `haproxy/`, `nginx/` | Reverse-proxy / load-balancer configuration |
| `QGIS/` | QGIS project templates |

---

## Documentation

- **This fork's docs:** see `docs/` (Sphinx sources under `docs/source/`,
  including the Enerplanet and REST API sections).
- **Changes vs. upstream:** [CHANGELOG.md](CHANGELOG.md).
- **Original pylovo:** [`tum-ens/pylovo`](https://github.com/tum-ens/pylovo).

---

## Attribution & citation

This project is a fork of the original **pylovo** tool developed at the Chair of
Renewable and Sustainable Energy Systems, Technical University of Munich
([`tum-ens/pylovo`](https://github.com/tum-ens/pylovo)). If you use pylovo in
academic work, please cite the original paper:

> *Generation of low-voltage synthetic grid data for energy system modeling with
> the pylovo tool.* Renewable and Sustainable Energy Reviews / ScienceDirect.
> https://www.sciencedirect.com/science/article/pii/S2352467724003473

See [CITATION.cff](CITATION.cff) for machine-readable citation metadata and
[ATTRIBUTIONS.md](ATTRIBUTIONS.md) for third-party credits.

## License

See [LICENSE](LICENSE).
