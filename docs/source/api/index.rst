REST API Reference
******************************************

This section documents the pylovo REST API built with FastAPI.

.. contents:: Table of Contents
   :local:
   :depth: 2


Getting Started
===============

Running the API Server
----------------------

The API runs inside Docker via ``environment/docker-compose.yml``:

.. code-block:: bash

    # Start the API container
    make up
    
    # Or directly with Docker Compose
    docker compose up -d

**Base URL:** ``http://localhost:8086``

**Interactive Docs:** ``http://localhost:8086/docs`` (Swagger UI)


Health Check
============

GET /health
-----------

Check API health status.

**Response:**

.. code-block:: json

    {
        "status": "healthy",
        "worker": "1",
        "cache_stats": {
            "hits": 1250,
            "misses": 45,
            "size": 128
        }
    }


Energy Estimation
=================

POST /estimate-energy
---------------------

Estimate electrical energy demand and peak load for a single building.

**Request Body:**

.. list-table::
   :header-rows: 1
   :widths: 25 15 15 45

   * - Field
     - Type
     - Required
     - Description
   * - building_type
     - string
     - Yes
     - Building type (e.g., office, residential, retail)
   * - area_m2
     - float
     - Yes
     - Floor area in square meters
   * - year_of_construction
     - integer
     - No
     - Construction year (residential age multiplier input)
   * - household_size
     - integer
     - No
     - Optional residential household size override (1..5)
   * - num_floors
     - integer
     - No
     - Helps infer multi-dwelling residential behavior
   * - energy_label
     - string
     - No
     - Residential Stromspiegel class ``A``..``G``
   * - hot_water_electric
     - boolean
     - No
     - Use residential benchmark tables with electric hot water (default: ``false``)

**Example Request:**

.. code-block:: bash

    curl -X POST "http://localhost:8086/estimate-energy" \
         -H "Content-Type: application/json" \
         -d '{
           "building_type": "apartments",
           "area_m2": 600,
           "year_of_construction": 1998,
           "num_floors": 4,
           "energy_label": "C",
           "hot_water_electric": false
         }'

**Response:**

.. code-block:: json

    {
        "status": "success",
        "data": {
            "yearly_demand_kwh": 12345.67,
            "yearly_demand_base_kwh": 12345.67,
            "peak_load_kw": 45.67,
            "peak_connection_kva": 63.0,
            "specific_demand_kwh_m2": 20.58,
            "f_class": "apartments",
            "parent_category": "residential",
            "household_size_used": 2,
            "estimated_households_used": 6.15,
            "energy_label_used": "C",
            "hot_water_electric": false,
            "age_factor_applied": 1.04,
            "age_factor_peak_applied": 1.03,
            "effective_year_used": 1998,
            "source": "consumer_categories_fclass_model_v3"
        }
    }


POST /estimate-energy-batch
---------------------------

Estimate energy demand for multiple buildings.

**Request Body:**

.. code-block:: json

    {
        "buildings": [
            {"building_type": "office", "area_m2": 500, "year_of_construction": 2015},
            {"building_type": "house", "area_m2": 150, "energy_label": "D"},
            {"building_type": "retail", "area_m2": 200, "hot_water_electric": false}
        ]
    }

**Response:**

.. code-block:: json

    {
        "status": "success",
        "data": [
            {
                "yearly_demand_kwh": 16625.0,
                "peak_load_kw": 6.5,
                "f_class": "office",
                "parent_category": "commercial",
                "source": "consumer_categories_fclass_model_v3"
            },
            {
                "yearly_demand_kwh": 4100.0,
                "peak_load_kw": 2.4,
                "f_class": "house",
                "parent_category": "residential",
                "energy_label_used": "D",
                "source": "consumer_categories_fclass_model_v3"
            },
            {
                "yearly_demand_kwh": 14200.0,
                "peak_load_kw": 7.0,
                "f_class": "retail",
                "parent_category": "commercial",
                "source": "consumer_categories_fclass_model_v3"
            }
        ]
    }


Grid Generation
===============

POST /generate-grid
-------------------

Generate synthetic distribution grid for a geographic area.

**Request Body:**

.. list-table::
   :header-rows: 1
   :widths: 25 15 15 45

   * - Field
     - Type
     - Required
     - Description
   * - geom
     - GeoJSON
     - Yes
     - Area boundary polygon (EPSG:4326 or EPSG:3857)
   * - user_id
     - string
     - No
     - User identifier for custom buildings
   * - include_public_buildings
     - boolean
     - No
     - Include public buildings (default: true)

**Example Request:**

.. code-block:: json

    {
        "geom": {
            "type": "Polygon",
            "coordinates": [[[11.5, 48.1], [11.6, 48.1], [11.6, 48.2], [11.5, 48.2], [11.5, 48.1]]]
        },
        "include_public_buildings": true
    }

Returns buildings, transformers, lines, MV lines, and grid statistics.


POST /grid-statistics
---------------------

Get detailed statistics for generated grids.

**Request Body:**

.. code-block:: json

    {
        "grid_result_ids": [1, 2, 3, 4, 5]
    }


POST /power-flow
----------------

Run power flow analysis on a grid.

**Request Body:**

.. code-block:: json

    {
        "grid_result_id": 123,
        "load_scaling": 1.0
    }

Returns bus voltages, line loadings, and transformer loading.


Custom Buildings
================

POST /add-custom-building
-------------------------

Add a custom building for grid planning.

**Request Body:**

.. code-block:: json

    {
        "user_id": "user123",
        "building_type": "commercial",
        "area_m2": 500,
        "peak_load_kw": 25.0,
        "geometry": {
            "type": "Point",
            "coordinates": [11.55, 48.15]
        }
    }


GET /custom-buildings/{user_id}
-------------------------------

List custom buildings for a user.


DELETE /custom-buildings/{building_id}
--------------------------------------

Delete a custom building. Requires ``user_id`` query parameter.


Equipment Data
==============

GET /transformer-sizes
----------------------

Get available transformer sizes from database.

**Response:**

.. code-block:: json

    {
        "status": "success",
        "sizes": [
            {"kva": 100, "cost_eur": 5000, "type": "Transformer", "name": "100kVA"},
            {"kva": 160, "cost_eur": 6500, "type": "Transformer", "name": "160kVA"},
            {"kva": 250, "cost_eur": 8000, "type": "Transformer", "name": "250kVA"},
            {"kva": 400, "cost_eur": 10000, "type": "Transformer", "name": "400kVA"},
            {"kva": 630, "cost_eur": 14000, "type": "Transformer", "name": "630kVA"}
        ],
        "count": 5
    }


GET /cable-types
----------------

Get available cable specifications.

**Response:**

.. code-block:: json

    {
        "status": "success",
        "cables": [
            {"name": "NAYY 4x50", "max_current_a": 142, "r_mohm_per_km": 641, "cost_eur_per_m": 12.5},
            {"name": "NAYY 4x150", "max_current_a": 260, "r_mohm_per_km": 206, "cost_eur_per_m": 25.0}
        ],
        "count": 2
    }


GET /consumer-categories
------------------------

Get consumer category definitions with load data.

**Response:**

.. code-block:: json

    {
        "status": "success",
        "categories": [
            {
                "id": 1,
                "definition": "Commercial",
                "peak_load_kw": null,
                "yearly_consumption_kwh": null,
                "peak_load_per_m2": 0.04,
                "yearly_consumption_per_m2": 79,
                "sim_factor": 0.50
            },
            {
                "id": 3,
                "definition": "SFH",
                "peak_load_kw": 14.5,
                "yearly_consumption_kwh": 3500,
                "peak_load_per_m2": null,
                "yearly_consumption_per_m2": null,
                "sim_factor": 0.07
            }
        ],
        "count": 2
    }


GET /equipment-costs
--------------------

Get all equipment costs grouped by type.

**Response:**

.. code-block:: json

    {
        "status": "success",
        "equipment": {
            "Transformer": [
                {"name": "100kVA", "s_max_kva": 100, "max_current_a": null, "cost_eur": 5000}
            ],
            "Cable": [
                {"name": "NAYY 4x150", "s_max_kva": null, "max_current_a": 260, "cost_eur": 25}
            ],
            "cable_costs_config": [
                {"name": "NAYY 4x50", "cost_eur_per_m": 12.5}
            ]
        }
    }


GET /voltage-settings
---------------------

Get voltage band settings used for grid analysis.

**Response:**

.. code-block:: json

    {
        "status": "success",
        "settings": {
            "nominal_voltage_v": 400,
            "voltage_band_low_pu": 0.90,
            "voltage_band_high_pu": 1.10,
            "min_voltage_v": 360,
            "max_voltage_v": 440
        }
    }


Pipeline Management
===================

POST /pipeline/run
------------------

Start a data pipeline job.

**Request Body:**

.. list-table::
   :header-rows: 1
   :widths: 20 15 15 50

   * - Field
     - Type
     - Default
     - Description
   * - country
     - string
     - Required
     - Country code (e.g., germany, austria)
   * - state
     - string
     - Required
     - State/region code (e.g., hamburg, bavaria)
   * - step
     - string
     - Required
     - Pipeline step: datapipeline, constructor, grid, all
   * - workers
     - integer
     - 10
     - Number of parallel workers
   * - no_cache
     - boolean
     - true
     - Skip cached data

**Response:**

.. code-block:: json

    {
        "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "status": "started",
        "message": "Pipeline 'grid' started for germany/hamburg"
    }


GET /pipeline/status/{job_id}
-----------------------------

Get the status of a pipeline job.

**Response:**

.. code-block:: json

    {
        "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "status": "running",
        "step": "grid",
        "progress": 45,
        "logs": ["Processing PLZ 20001...", "Processing PLZ 20002..."],
        "started_at": "2024-01-15T10:30:00Z",
        "completed_at": null,
        "error": null
    }


GET /pipeline/regions
---------------------

Get available regions for the pipeline. Returns all European countries and their states/regions.

**Response (excerpt):**

.. code-block:: json

    {
        "countries": [
            {
                "code": "germany",
                "name": "Germany",
                "states": [
                    {"code": "hamburg", "name": "Hamburg"},
                    {"code": "bavaria", "name": "Bavaria (Bayern)"},
                    {"code": "berlin", "name": "Berlin"}
                ]
            },
            {
                "code": "austria",
                "name": "Austria",
                "states": [
                    {"code": "vienna", "name": "Vienna (Wien)"},
                    {"code": "salzburg", "name": "Salzburg"}
                ]
            }
        ]
    }


GET /pipeline/history
---------------------

Get the history of pipeline jobs.

**Query Parameters:**

- ``limit`` (optional): Number of jobs to return (default: 10)

**Response:**

.. code-block:: json

    {
        "jobs": [
            {
                "job_id": "a1b2c3d4-...",
                "country": "germany",
                "state": "hamburg",
                "step": "grid",
                "status": "completed",
                "started_at": "2024-01-15T10:30:00Z",
                "completed_at": "2024-01-15T11:45:00Z",
                "error": null
            }
        ]
    }


GET /pipeline/states/{country}
------------------------------

Return database-backed state statistics for a country, including postcode and
grid counts. ``country`` can be a country name (for example ``netherlands``) or
ISO code (for example ``NL``).

**Query Parameters:**

- ``version_id`` (optional): Version to use for ``postcode_result`` and ``grid_result`` counts (defaults to configured ``VERSION_ID``)

**Response (excerpt):**

.. code-block:: json

    {
        "country_code": "NL",
        "version_id": "1",
        "states": [
            {
                "state_code": "utrecht",
                "state_name": "Utrecht",
                "country_code": "NL",
                "osm_relation_id": 47780,
                "nuts_code": "NL31",
                "postcode_count": 403,
                "postcode_result_count": 403,
                "grid_count": 1284
            }
        ]
    }


DELETE /pipeline/states/{country}/{state}
-----------------------------------------

Delete one state scope across raw datapipeline tables and generated result
tables. By default the endpoint runs in preview mode (``dry_run=true``).

**Query Parameters:**

- ``dry_run`` (default ``true``): Preview impacted rows only; set to ``false`` to execute deletion
- ``drop_state_row`` (default ``true``): Also remove the state entry from the ``state`` registry table

**Dry-run Response (excerpt):**

.. code-block:: json

    {
        "status": "dry_run",
        "dry_run": true,
        "country_code": "NL",
        "state_code": "utrecht",
        "impact": {
            "state_rows": 1,
            "postcode_rows": 403,
            "postcode_result_rows": 403,
            "grid_result_rows": 1284,
            "lines_result_rows": 9876,
            "buildings_result_rows": 45231,
            "res_rows": 32000,
            "oth_rows": 13231,
            "ways_rows": 210000,
            "transformers_rows": 845
        },
        "deleted": {}
    }

**Delete Response (excerpt):**

.. code-block:: json

    {
        "status": "deleted",
        "dry_run": false,
        "country_code": "NL",
        "state_code": "utrecht",
        "impact": { "...": "same shape as dry_run impact" },
        "deleted": {
            "res_rows": 32000,
            "oth_rows": 13231,
            "ways_rows": 210000,
            "transformers_rows": 845,
            "municipal_register_rows": 403,
            "postcode_result_rows": 403,
            "postcode_rows": 403,
            "postcode_result_legacy_rows": 0,
            "state_rows": 1
        }
    }


Error Responses
===============

All endpoints return standard error responses:

**400 Bad Request:**

.. code-block:: json

    {"detail": "Invalid step. Must be one of: ['datapipeline', 'constructor', 'grid', 'all']"}

**404 Not Found:**

.. code-block:: json

    {"detail": "Job not found"}

**500 Internal Server Error:**

.. code-block:: json

    {"detail": "Database connection failed"}
