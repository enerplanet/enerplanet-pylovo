# Changelog

This changelog summarizes the modifications made to this fork of **pylovo** compared
to the upstream/original `pylovo` repository (located at `../pylovo-original`).

The comparison **excludes** the `datapipeline/` directory as requested.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Fork-Unreleased] - Modifications relative to upstream

### Added

#### REST API service (`api/`) — *new*
A full FastAPI-based web service has been added on top of pylovo:
- `api/main.py` — FastAPI application entrypoint.
- `api/routers/` — endpoints: `boundary`, `building`, `energy`, `grid`,
  `pipeline`, `power_flow`, `reference`, `transformer`.
- `api/models/` — Pydantic models: `building`, `grid`, `pipeline`,
  `power_flow`, `transformer`.
- `api/services/` — service layer (`mv_lines.py`, ...).
- `api/utils/` — shared API utilities.
- New runtime dependencies: `fastapi`, `uvicorn`, `pydantic>=2`,
  `typing-extensions`, `httpx`.

#### Containerization & deployment — *new*
- `Dockerfile` — container image for pylovo.
- `docker-compose.yml` / `docker-compose.dev.yml` — orchestration for app,
  database and supporting services.
- `nginx/nginx.conf` — reverse proxy configuration.
- `haproxy/haproxy.cfg` — load balancer configuration.
- `.env.docker` — environment file template for container deployments.
- `Makefile` — convenience targets for dev/build/run/deploy.

#### Database migrations (`migrations/`) — *new*
- `001_add_missing_indexes.sql`
- `002_add_user_scope_indexes.sql`

#### Initial / seed data — *new*
- `initial-data/` directory added for bootstrap datasets.

#### Plotting package (`plotting/`) — *moved + extended*
Plotting code moved out of `src/pylovo/plotting` to a top-level `plotting/`
package and extended:
- `config_plots.py`, `export_net.py`, `plot_for_plz.py`, `plot_networks.py`,
  `plotting_classification.py`.

#### Runme scripts (`runme/`) — *new*
Top-level operational scripts replacing the previous CLI:
- `analyze/`, `create/`, `delete/`, `export/`, `import/` directories.
- `main_classification.py`, `main_constructor.py`.

#### Caching layer
- `src/cache.py` — *new* caching utility.
- Added `redis>=5.0.0` dependency.

#### AI estimation module (`src/ai_estimation/`) — *new*
- `estimator.py` and package initializer.
- Documentation under `docs/source/ai_estimation/`.

#### EV hosting capacity
- `src/ev_hosting.py` — *new* module for EV hosting capacity analysis.

#### Parameter calculator
- `src/parameter_calculator.py` — *new* top-level parameter calculator
  (separate from `src/analysis/parameter_calculation.py`).

#### Power flow analysis
- `src/analysis/powerflow_analysis.py` — *new* (replaces
  upstream's `powerflow_calculation.py`).

#### Database connection pooling
- `src/database/connection_pool.py` — *new* pooled connection management.
- `src/database/base_mixin.py` — *new* base mixin.

#### SQL helpers
- `src/postgres_dump_functions.sql` — *new* Postgres helper functions.
- `src/ways_preprocessing_functions/core/generate_building_to_way_connections_infdb.sql`
  — *new* InfDB-targeted building-to-way connection generator.

#### Configuration files — *new*
- `config/config_database.yaml`
- `config/config_data.yaml`
- `config/config_plotting.yaml`
- `config/config_table_structure.py` (moved here from `src/pylovo/database/`)
- `config/config_version.yaml`

#### Clustering settings
- `src/classification/clustering/cluster_settings.py` — *new*.
- `src/classification/grid_examples/` — *new* example grid resources.

#### Documentation — extended
New documentation sections under `docs/source/`:
- `ai_estimation/`
- `api/` (REST API reference)
- `architecture/` — `configuration.rst`, `database_schema.rst`,
  `datapipeline.rst`, `deployment.rst`, `index.rst`
- `building_types/` — `consumer_categories_detail.rst`, `index.rst`
- `enerplanet/` — `api.rst`, `architecture.rst`, `deployment.rst`,
  `installation.rst`, `keycloak.rst`, `screenshots.rst`, `index.rst`
- `webservice/` — `index.rst`, `technologies.rst`
- `installation/quickstart.rst`
- New `_static/` assets and `ai_estimation` images.

#### Dependencies added
`fastapi`, `uvicorn`, `pydantic>=2`, `typing-extensions`, `redis`, `numba`,
`httpx`, `laspy[lazrs]==2.7.0`, `rasterstats==0.20.0`, `pyarrow==19.0.1`.

### Changed

#### Package layout flattened
- Upstream uses a `src/pylovo/` package layout (proper installable package
  with `pyproject.toml`).
- This fork flattens to `src/` directly (imports changed to
  `from src.<...> import ...`). `pyproject.toml` and `uv.lock` are removed.
- `requirements.txt` is now the single source of dependencies.

#### `src/__init__.py`
- Removed importlib-based versioning and the public re-exports of
  electrical-backend types (`IElectricalBackend`, `BusSpec`, `LineSpec`,
  `LoadSpec`, `TransformerSpec`, `ExtGridSpec`, `normalize_cable_name`,
  `create_backend`).
- `__all__` reduced to `["DatabaseClient", "GridGenerator", "DatabaseConstructor"]`.

#### `src/config_loader.py`
- Removed the hybrid/user-directory config search (no more
  `PYLOVO_ROOT` / `PYLOVO_CONFIG_DIR` / `~/.config/pylovo` lookup).
- Reverted to loading YAML files via fixed relative paths
  (`../config/config_*.yaml`).
- Added `PostalCode` type alias (supports numeric + alphanumeric formats,
  e.g. NL "1234 AB").
- Added `COUNTRY_CODE_MAP` with all major European countries and
  `get_country_code()` helper.
- `load_dotenv()` now uses `override=False` so real environment variables
  (e.g. from docker-compose) take precedence over `.env`.
- `USE_INFDB` is now read from `CONFIG_DATABASE` rather than the env.
- Added a `REGIONAL CONFIGURATION` section pulling `PLZ` / `AGS` from
  `CONFIG_GENERATION`.
- Replaced emoji error markers with plain `[ERROR]` markers.
- `CONFIG_PLOTTING` is now loaded as well.

#### Database layer (`src/database/`)
Substantial changes versus upstream (large diffs):
- `database_client.py` (~1k lines changed) — pooled connections, user
  scoping, multi-tenant additions.
- `database_constructor.py` (~1.3k lines changed) — initialization
  reworked.
- `grid_mixin.py`, `analysis_mixin.py`, `preprocessing_mixin.py`,
  `utils_mixin.py`, `clustering_mixin.py` all modified.
- `config_table_structure.py` moved out of this directory into
  `config/`.

#### Grid generation
- `src/grid_generator.py` — large rewrite (~1.6k lines diff).
- `src/cable_installer.py` — significant changes (~480 lines diff).
- `src/utils.py` — substantial changes (~325 lines diff).

#### Electrical backend
- `electrical_backend/pandapower/backend.py` and `factory.py`,
  `core/backend_base.py`, `core/specs.py`, `__init__.py` all modified.
- The upstream `electrical_backend/opendss/` subpackage is **removed** in
  this fork.

#### Analysis
- `analysis/parameter_calculation.py` — major rewrite (~2.1k lines diff).
- Upstream helpers removed in this fork:
  - `analysis/comparison_helpers.py`
  - `analysis/grid_analysis.py`
  - `analysis/powerflow_calculation.py`
  - `analysis/validation_helpers.py`

#### Data import (`src/data_import/`)
- `import_buildings.py`, `import_transformers.py` modified.
- Upstream files removed in this fork:
  - `municipal_register.py`
  - `region_resolver.py`
  - `test_postcodes.py`
  - `transformer_capacity_utils.py`
  - `transformers_ui.py`

#### Classification (`src/classification/`)
All clustering/sampling/utility modules modified relative to upstream:
- `clustering/apply_clustering_for_visualisation.py`
- `clustering/clustering_algorithms.py`
- `clustering/filter_grids.py`
- `clustering/get_no_clusters_for_clustering.py`
- `clustering/get_parameters_for_clustering.py`
- `clustering/prepare_data_for_clustering.py`
- `database_communication/database_communication.py`
- `sampling/replace_plz.py`, `sampling/sample.py`
- `utils/get_average_values_clustering_parameters.py`

#### InfDB integration
- `src/infdb/infdb_client.py` modified.

#### Configuration YAMLs
- `config/config_analysis.yaml` and `config/config_generation.yaml`
  modified relative to upstream.

#### Ways preprocessing SQL
- `ways_preprocessing_functions/utils/generate_building_way_connection_candidates.sql`
- `ways_preprocessing_functions/utils/generate_building_way_connection_candidates_infdb.sql`
- `ways_preprocessing_functions/utils/insert_way_segment.sql`
- Upstream's `core/generate_transformer_to_way_connections_infdb.sql` is
  **removed**, replaced by `core/generate_building_to_way_connections_infdb.sql`.

#### QGIS templates
- `QGIS/template_local_db.qgz` and `QGIS/template_remote_db.qgz` updated.

#### Documentation
- `docs/source/conf.py`, `docs/source/index.rst`, `docs/requirements.txt`
  modified.
- `docs/source/installation/installation.rst` replaced with
  `quickstart.rst`.
- Upstream `docs/source/grid_generation/`, `docs/source/classification/`,
  `docs/source/visualisation/`, and the `images/database/` and
  `images/install/` folders are **not present** in this fork; new
  `grid_generation/pylovo_grid_example.png` was added in their place.

#### Requirements
- Removed: `altdss>=0.2.0`, `pyproj==3.6.1`.
- Added: see "Dependencies added" above.

#### Compose services named by role
- Both compose files pin the project to `pylovo` and set no
  `container_name`. The dev API service is `api` (container
  `pylovo-api-1`), reachable on `spatialhub-net` as `pylovo` and,
  until callers move, `pylovo-api-dev`.
- Production runs one `api` service with `deploy.replicas: 3`; the
  containers keep the names `pylovo-api-1..3`. HAProxy is the `proxy`
  service (aliases `pylovo`, `pylovo-haproxy`) and finds the replicas
  through Docker DNS on the alias `pylovo-api-pool`.
- `WORKER_ID` is no longer set per replica; the API logs the container
  hostname when it is absent. The Postgres container is
  `pylovo-postgres-1`.

### Removed (relative to upstream)

- Top-level project metadata files: `pyproject.toml`, `uv.lock`,
  `CITATION.cff`, `CONTRIBUTING.md`, `LICENSE.txt`, `README.rst`,
  `CHANGELOG.md` (the upstream changelog — this file replaces it for
  fork-level tracking), `.env.example`, `.github/`, `notebook_tutorials/`.
- The upstream `src/pylovo/cli/` package (CLI entrypoint) — replaced by
  `runme/` scripts in this fork.
- The `opendss` electrical backend (`src/electrical_backend/opendss/`).
- Various analysis and data-import helpers (see "Changed" sections
  above).

---

## Module-level deep dives

This section gives more depth on the major new/changed analysis modules.

### Power flow analysis — `src/analysis/powerflow_analysis.py`

This module *replaces* upstream's `powerflow_calculation.py` and provides a
self-contained validation pipeline for pylovo-generated low-voltage grids
on top of **pandapower**.

**Library / stack used**
- `pandapower` (Newton–Raphson solver, network model, results tables).
- `numpy` (`numpy.random.default_rng`) for reproducible random load
  assignment.
- `pandas` for tabular collection of results across many grids.
- Logging to `src/log/grid_validation.log` via `utils.create_logger`.

**Constants / defaults**
- `DEFAULT_MIN_VM_PU = 0.9`, `DEFAULT_MAX_VM_PU = 1.1` — voltage band
  (EN 50160-style ±10 %).
- `DEFAULT_COS_PHI = 0.95` — load power factor.
- `DEFAULT_LOAD_STD_RATIO = 0.1` — std-dev / mean ratio for Gaussian
  load sampling.

**Workflow** (entrypoint: `process_and_collect_voltage_data`)
1. Iterates over a DataFrame of grids (`bcid`, `grid` JSON).
2. Deserializes each grid with `pp.from_json_string`.
3. Computes a *simultaneous* per-load value with
   `utils.oneSimultaneousLoad(peak_load_residential, n_loads,
   sim_factor=0.07)`.
4. Calls `preprocess_pylovo_network(...)` which:
   - clears existing loads (`_clear_network_loads`),
   - assigns new **Gaussian-distributed** loads
     (`assign_gaussian_loads`) on every non-swing bus (swing/ext-grid
     buses excluded via `_ext_grid_buses`),
   - sets `min_vm_pu` / `max_vm_pu` on every bus,
   - optionally adjusts a specific transformer-line length
     (`_adjust_transformer_line_length`),
   - optionally adds polynomial cost functions to external grids
     (`_add_external_grid_costs`) so OPF runs are well-posed.
5. Runs `run_powerflow(net)` → `pp.runpp(net, algorithm="nr",
   calculate_voltage_angles=True, enforce_q_lims=True)` and reports
   convergence via `net.converged`.
6. Collects `net.res_bus['vm_pu']` per bus into a long-format DataFrame.

**Additional helpers**
- `assign_random_loads(net, load_range, cos_phi, mode, seed)` — uniform
  random apparent-power loads (`pp.create_load_from_cosphi`).
- `assign_gaussian_loads(net, avg_load, std_dev, cos_phi, mode,
  min_sn_mva, seed)` — clipped normal-distributed loads.
- `calculate_network_metrics(net)` — aggregate KPIs (voltages, losses,
  loading).
- `check_voltage_violations(net, min_vm_pu, max_vm_pu)` — returns a
  DataFrame of buses outside the band.
- `check_line_overloads(net, threshold_pct=100.0)` — flags lines whose
  `loading_percent` exceeds threshold.
- `analyze_network_losses(net)` — separates line vs transformer
  copper/iron losses from `res_line` / `res_trafo`.
- `generate_validation_report(net, bcid=None)` — combines the above
  into a single dict report per grid.

**Why it changed vs upstream**
Upstream's `powerflow_calculation.py` was largely tied to the CLI and
upstream's validation helpers (`validation_helpers.py`,
`comparison_helpers.py`). In this fork the validation logic is
self-contained, exposes a stable public API (used by the FastAPI
`power_flow` router), and adds explicit defensive handling (try/except
per grid, structured logging, deterministic seeding).

---

### Parameter calculator — `src/parameter_calculator.py` (new top-level module)

Top-level orchestrator for per-PLZ and per-grid parameter extraction.
This is **separate** from `src/analysis/parameter_calculation.py` (which
contains the lower-level numerical primitives) and acts as the
high-level driver used by the API and the runme scripts.

**Library / stack used**
- `pandapower` + `pandapower.topology` (NetworkX export for graph
  metrics).
- `networkx` for branch counting and graph distances.
- `geopandas` + `shapely` (via `gpd.points_from_xy`) for spatial
  handling of bus coordinates.
- `sklearn.metrics.pairwise.haversine_distances` to compute
  great-circle distances between house-connection buses (then converted
  to km using Earth radius 6371 km).
- `statistics.median` for robust averages.
- pylovo: `database.database_client.DatabaseClient`,
  `config_loader` constants (`VERSION_ID`, `CLASSIFICATION_VERSION`,
  `PEAK_LOAD_HOUSEHOLD`, `CLUSTERING_PARAMETERS`).

**Class `ParameterCalculator`**
Constructor: `ParameterCalculator(country_code="DE")` — opens a
`DatabaseClient` and pins to the active `VERSION_ID`.

State tracked as attributes (later flushed to the
`clustering_parameters` table):
- counts — `no_connection_buses`, `no_branches`, `no_house_connections`,
  `no_house_connections_per_branch`, `no_households`,
  `no_household_equ` (equivalent households =
  `max_power_mw·1000 / PEAK_LOAD_HOUSEHOLD`),
  `no_households_per_branch`, `max_no_of_households_of_a_branch`;
- geometry — `house_distance_km`, `cable_length_km`,
  `cable_len_per_house`, `avg_trafo_dis`, `max_trafo_dis`;
- power — `max_power_mw`, `simultaneous_peak_load_mw`,
  `transformer_mva`, `osm_trafo`;
- electrical — `resistance`, `reactance`, `ratio`, `vsw_per_branch`,
  `max_vsw_of_a_branch`.

**Public methods**
- `calc_parameters_per_plz(plz)` — guarded entrypoint; checks
  `is_grid_generated` / `is_grid_analyzed`; runs the three analysis
  passes (`analyse_basic_parameters_per_plz`, `analyse_cables_per_plz`,
  `analyse_trafo_parameters_per_plz`); commits, with rollback-by-delete
  from the sample set on failure.
- `calc_parameters_per_grid(plz)` — per-(kcid, bcid) loop that loads
  the pandapower grid from the DB and runs `compute_parameters`.
- `calc_grid_parameters(plz, bcid, kcid)` — single-grid wrapper that
  persists results via `dbc.insert_clustering_parameters(params)`.
- `compute_parameters(net)` — orchestrates all per-grid metrics
  (described below).
- `get_simultaneous_peak_load()` — looks up the simultaneous peak load
  matching this grid's transformer size and `max_trafo_dis` from the
  per-transformer table.
- `has_osm_trafo()` — `True` iff `bcid < 0` (negative ids encode
  OSM-imported transformers).
- `get_parameters_as_dataframe()` — exports all attributes as a single
  row using `CLUSTERING_PARAMETERS` as column order.

**Metric computations** (inside `compute_parameters`)
- Bus typing: `get_no_of_buses(net, "Consumer Nodebus")` /
  `"Connection Nodebus"` via substring match on `net.bus.name`.
- Demand: `get_max_power` = `net.load.max_p_mw.sum()`.
- Equivalent households: `max_power_mw · 1000 / PEAK_LOAD_HOUSEHOLD`.
- Cable length: `net.line.length_km.sum()` and per-house density.
- Graph metrics via `pp.topology.create_nxgraph(net)`:
  - `get_no_branches` — counts root-adjacent edges,
  - `get_distances_in_graph` — `(avg, max)` shortest-path distances
    from the transformer root to each consumer bus,
  - `calc_resistance` — accumulates R, X along feeder paths and
    derives `ratio = R/X` and worst-case voltage drop
    `max_vsw_of_a_branch`.
- House-to-house distance: per-consumer-bus haversine distance matrix
  → for each bus take its 4 nearest neighbours and average → take the
  **median** across buses (robust to outliers).
- Voltage-drop indicator: `vsw_per_branch = resistance / no_branches`.

**Database hooks** (via `DatabaseClient`)
- `is_grid_generated`, `is_grid_analyzed`,
  `insert_clustering_parameters`, `read_per_trafo_dict`,
  `delete_plz_from_sample_set_table` (used to drop a PLZ when analysis
  fails so that downstream sampling skips it).

---

### AI estimation — `src/ai_estimation/estimator.py` (new)

Despite the name, this is **not** a machine-learning model. It is a
research-backed, rule-based electricity-demand estimator for
buildings, intended as the "AI estimation" service exposed by the API
(`docs/source/ai_estimation/`).

**Library / stack used**
- Pure Python: `typing`, `re`, `math` only.
- Pylovo configuration: `CONSUMER_CATEGORIES` and `PEAK_LOAD_HOUSEHOLD`
  from `src.config_loader`.
- No external ML libraries — values are looked up from internal tables
  derived from published German/EU benchmarks.

**Reference data sources** (cited inline)
- **Stromspiegel 2025** (co2online / BMWK) — n = 57 000 real
  household electricity bills; per-occupant kWh class bounds (A–G), with
  and without electric hot water, for apartments vs houses.
- **BDEW** standard load profiles (H0, G0–G6, L0–L2) and sector
  statistics.
- **DIN 18015-1:2020-05** — coincidence factors for residential
  installations.
- **AMEV 2019** — measured peak loads for 1 270 German public
  buildings.
- **EHI Retail Institute 2024** — retail electricity benchmarks.
- **MDPI / González et al. 2018** — German hospital benchmarks.
- **CIBSE TM46:2008** — non-residential energy benchmarks.

**Class `BuildingEnergyEstimator`** — internal tables include:
- `_STROMSPIEGEL_2025_APARTMENT_KWH` / `_HOUSE_KWH` — mid-range
  kWh/yr per household size 1–5 (D-class).
- `_STROMSPIEGEL_2025_HOUSE_BOUNDS` /
  `_APARTMENT_BOUNDS` — class upper bounds A–F (G = above F).
- Twin tables with `_EWH_` suffix for the *electric hot water* case.
- `_ENERGY_LABEL_INDEX` — maps "A".."G" → 0..6.
- Age-multiplier tables for building age / renovation correction
  (calibrated so EnEV-2002 = 1.0).
- Full-load-hours and W/m² peak-power tables per f_class / parent
  category.

**Design rules** (encoded as code)
- Residential electricity (appliances/lighting) is *not* strongly
  age-dependent — only a moderate age correction is applied
  (heating-energy aging effects are out of scope).
- Diversity follows DIN 18015-1 approximated as
  `g(N) = N^(-0.45)` (within ±5 % of the DIN table for N ∈ [1, 200]).
- Non-residential kWh/m² figures align with BDEW
  Verbrauchskennwerte (2015), EHI 2024 and CIBSE TM46.
- Supermarket: 284 kWh/m² is on *Verkaufsfläche* (sales area), the
  model applies a discount on gross floor area.

**Public API**
- `BuildingEnergyEstimator.estimate(building_type, area_m2,
  year_of_construction=None, household_size=None, num_floors=None,
  renovation_year=None, energy_label=None,
  hot_water_electric=False) -> Dict[str, float]` — returns at minimum
  `yearly_demand_kwh` and `peak_load_kw` (plus auxiliary fields like
  `household_size_used`, `peak_connection_kva`).
- Module-level helpers `_get_estimator()` (cached singleton) and
  `estimate_building_energy(...)` (functional wrapper used by the API
  router).
- Internal pipeline: `_resolve_row` → consumer-category lookup via
  fuzzy `_normalize` matching → branch on `load_method` (`household`
  vs `area`) → apply Stromspiegel/EHI/BDEW base × age factor × energy
  label factor × diversity factor.

---

### EV hosting capacity — `src/ev_hosting.py` (new)

Deterministic hosting-capacity calculator answering "how many EV
chargers can be added to this building/transformer area?".

**Method**
- Implements the **deterministic method** from
  *Umoh et al., Energies 2023, 16, 3609* — "Methods and Tools for PV
  and EV Hosting Capacity Determination in Low Voltage Distribution
  Networks — A Review", §3.1 and §4.
- Worst-case, fixed-input analysis; returns the *most-limiting*
  constraint.

**Library / stack used**
- Pure Python (`math`, `typing`). No pandapower call — this is an
  analytic screening tool.

**Constraints checked**
1. **Transformer thermal**:
   `remaining_kVA = trafo_kVA − P_load_kW / pf_grid`,
   chargers limited by `remaining_kVA / (charger_kVA · sim_factor)`.
2. **Voltage** (EN 50160 lower band): default min 0.90 p.u., 400 V
   nominal LV, evaluated against the line voltage drop produced by the
   new charger current.
3. **Cable thermal**: line current vs `cable_max_current_a`, with the
   typical NAYY 4×150 impedance default (`0.32 Ω/km`).

**Parameters**
`transformer_capacity_kva`, `current_peak_load_kw`,
`charger_power_kw=11.0` (Level 2 default), `simultaneity_factor=0.8`
(paper ref [143]: 46–85 % of customers charging simultaneously),
`power_factor_grid=0.95`, `power_factor_ev=0.99`,
`nominal_voltage_v=400.0`, `voltage_limit_pu=0.90`,
`cable_impedance_ohm_per_km=0.32`, `cable_length_km`,
`cable_max_current_a`.

**Returns** — dict with `max_chargers`, `remaining_capacity_kva`,
`limiting_factor` (`Transformer` | `Voltage` | `Cable`), `status`
(`safe` | `warning` | `critical`) and a per-constraint `details`
breakdown.

---

### Cache layer — `src/cache.py` (new)

Lightweight cache wrapper used by the FastAPI service.

**Library / stack used**
- `redis` (Python client) when reachable.
- Falls back automatically to a **thread-safe in-memory dict**
  (`_cache` guarded by `threading.Lock`) so the API stays operational
  without Redis (dev / unit-test mode).
- `json` for value serialization, `functools.wraps` for the decorator.

**Public API**
- `init_redis()` / `close_redis()` — lifecycle.
  Connects to `REDIS_HOST` / `REDIS_PORT` env vars; on any failure
  logs a warning and switches to in-memory mode (`_use_redis = False`).
- `get(key)`, `set(key, value, ttl=DEFAULT_TTL)`, `delete(key)`,
  `clear()`.
- `@cached(key_prefix, ttl=DEFAULT_TTL)` — function decorator that
  builds a deterministic cache key from prefix + positional/keyword
  args.
- `get_stats()` — returns size / hits / mode (Redis vs memory).

**Defaults**
- `DEFAULT_TTL = 3600` s (1 hour) — appropriate for the
  intended payloads (transformer-size catalogue, cable-type table,
  consumer categories — all rarely changing static data).

---

### Database connection pooling — `src/database/connection_pool.py` (new)

New module supporting the API's concurrent request handling — the
upstream code assumed a single-process synchronous `DatabaseClient`.
Combined with the new `base_mixin.py` it provides:
- a pooled `psycopg2` connection acquisition path,
- per-request scoping so mixins can reuse the same connection,
- safe release/rollback on exceptions.

(All other DB mixins were modified to integrate with this pool, which
explains the large diffs in `database_client.py`,
`database_constructor.py`, `grid_mixin.py`,
`preprocessing_mixin.py`, `analysis_mixin.py`, `clustering_mixin.py`
and `utils_mixin.py`.)

---

### Notes

- The `datapipeline/` directory in this fork was intentionally **excluded**
  from this comparison per the user's request and is not described here.
- Several modules show very large textual diffs (>1k lines). Those are
  flagged at the module level above; consult `git diff` against
  `../pylovo-original` for line-by-line details.
- This changelog was generated from a structural diff
  (`diff -rq pylovo-original/pylovo pylovo --exclude=datapipeline …`) and
  spot-checks of representative files; it summarizes scope rather than
  every individual edit.
