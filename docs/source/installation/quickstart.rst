Docker Quickstart Guide
***********************

This guide walks you through setting up PyLovo using Docker.

.. contents:: Table of Contents
   :local:
   :depth: 2


Prerequisites
=============

Before starting, ensure you have:

1. **Docker** (20.10+) and **Docker Compose** (2.0+)
2. **Git** with **Git LFS** (for large files)
3. **Make** (optional, for convenience commands)
4. **Running PostgreSQL container** named ``postgres`` with PostGIS/pgRouting

Install Docker
--------------

**Ubuntu/Debian:**

.. code-block:: bash

    # Install Docker
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    
    # Add user to docker group (logout required)
    sudo usermod -aG docker $USER
    
    # Install Docker Compose plugin
    sudo apt-get update
    sudo apt-get install docker-compose-plugin

**Verify installation:**

.. code-block:: bash

    docker --version
    docker compose version


Quick Setup
===========

Clone and Setup
---------------

.. code-block:: bash

    # Clone the repository
    git clone https://mygit.th-deg.de/thd-spatial-ai/pylovo.git
    cd pylovo

    # Install Git LFS and pull large files
    git lfs install
    git lfs pull

    # One-command setup (creates database, builds containers, starts services)
    make setup

This runs the following steps automatically:

1. Pulls Git LFS files (raw_data.7z)
2. Creates ``pylovo_db`` database in PostgreSQL
3. Builds the Docker image (extracts raw_data.7z)
4. Starts all services (HAProxy, Redis, PostgreSQL, 3 API replicas)

Verify Installation
-------------------

.. code-block:: bash

    # Check all containers are running
    docker compose -f environment/docker-compose.yml ps --format "table {{.Name}}\t{{.Status}}"

    # Expected output:
    # NAME                STATUS
    # pylovo-api-1        Up
    # pylovo-api-2        Up
    # pylovo-api-3        Up
    # pylovo-postgres-1   Up (healthy)
    # pylovo-proxy-1      Up
    # pylovo-redis-1      Up

    # Test health endpoint
    curl http://localhost:8086/health


Manual Setup Steps
==================

If ``make setup`` doesn't work, follow these steps:

Step 1: Pull LFS Files
----------------------

.. code-block:: bash

    # Install git-lfs if not installed
    sudo apt-get install git-lfs

    # Initialize and pull LFS files
    git lfs install
    git lfs pull

    # Verify raw_data.7z exists (should be ~500MB)
    ls -lh raw_data.7z

Step 2: Create Database
-----------------------

PyLovo requires a PostgreSQL database with PostGIS and pgRouting extensions.

.. code-block:: bash

    # Create the pylovo_db database (assumes postgres container exists)
    docker exec postgres psql -U postgres -c "CREATE DATABASE pylovo_db;"
    
    # Enable required extensions
    docker exec postgres psql -U postgres -d pylovo_db -c \
        "CREATE EXTENSION IF NOT EXISTS postgis; \
         CREATE EXTENSION IF NOT EXISTS pgrouting;"

Step 3: Build and Start
-----------------------

.. code-block:: bash

    # Build the Docker image
    docker compose build
    
    # Start all services
    docker compose up -d
    
    # View logs
    docker compose logs -f


Generating Grids
================

Full Pipeline (One Command)
---------------------------

The easiest way to process a region is with the ``make process`` command, which runs
all three steps (datapipeline, constructor, grid) automatically:

.. code-block:: bash

    # Process Netherlands/Utrecht with 10 workers
    make process COUNTRY=netherlands STATE=utrecht WORKERS=10
    
    # Process Germany/Hamburg
    make process COUNTRY=germany STATE=hamburg WORKERS=10

This is equivalent to running:

1. ``make datapipeline`` - Download OpenStreetMap data (buildings, roads, transformers)
2. ``make constructor`` - Load data into PostgreSQL database  
3. ``make grid`` - Generate synthetic low-voltage grids

Individual Steps
----------------

You can also run each step separately for more control:

Step 1: Run Data Pipeline
^^^^^^^^^^^^^^^^^^^^^^^^^

Download and process OpenStreetMap data:

.. code-block:: bash

    # Run datapipeline for Hamburg
    make datapipeline COUNTRY=germany STATE=hamburg
    
    # Or for Netherlands
    make datapipeline COUNTRY=netherlands STATE=flevoland

**Duration:** 10-30 minutes depending on region size

Step 2: Run Constructor
^^^^^^^^^^^^^^^^^^^^^^^

Load processed data into the database:

.. code-block:: bash

    # Run constructor
    make constructor COUNTRY=germany STATE=hamburg

**Duration:** 15-45 minutes

Step 3: Generate Grids
^^^^^^^^^^^^^^^^^^^^^^

Generate synthetic grids using multiprocessing:

.. code-block:: bash

    # Generate grids with 10 parallel workers
    make grid COUNTRY=germany STATE=hamburg WORKERS=10

**Duration:** 1-4 hours depending on region size


Processing Multiple States
--------------------------

You can process multiple states for the same country. Each state's data
is added to the database without removing previously loaded states:

.. code-block:: bash

    # Process Flevoland first
    make process COUNTRY=netherlands STATE=flevoland WORKERS=10
    
    # Then process Utrecht (Flevoland data is preserved)
    make process COUNTRY=netherlands STATE=utrecht WORKERS=10
    
    # Then process Noord-Holland
    make process COUNTRY=netherlands STATE=noord_holland WORKERS=10

The ``state_code`` in the postcode table ensures grid generation only 
processes postcodes for the specified state.


Example: Complete Workflow for Netherlands (Flevoland)
------------------------------------------------------

Flevoland is the smallest Dutch province, ideal for testing.

.. code-block:: bash

    # 1. Prepare Netherlands postcode data (downloads CBS boundaries)
    make prepare-netherlands

    # 2. Run datapipeline for Flevoland (downloads buildings, roads, transformers)
    make datapipeline COUNTRY=netherlands STATE=flevoland

    # 3. Load data into database (imports shapefiles, creates tables)
    make constructor COUNTRY=netherlands STATE=flevoland

    # 4. Generate grids (creates synthetic low-voltage networks)
    make grid COUNTRY=netherlands STATE=flevoland WORKERS=10

**Expected Duration:**

- Step 1: ~2 minutes (one-time download)
- Step 2: ~10 minutes
- Step 3: ~5 minutes
- Step 4: ~30-60 minutes

Example: Complete Workflow for Austria (Wien)
---------------------------------------------

Wien (Vienna) is a good test region for Austria.

.. code-block:: bash

    # 1. Prepare Austria postcode data (downloads GADM districts)
    make prepare-austria

    # 2. Run datapipeline for Wien
    make datapipeline COUNTRY=austria STATE=wien

    # 3. Load data into database
    make constructor COUNTRY=austria STATE=wien

    # 4. Generate grids
    make grid COUNTRY=austria STATE=wien WORKERS=10


Makefile Commands
=================

Setup & Build
-------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Command
     - Description
   * - ``make setup``
     - Full setup: pull LFS, create DB, build, start
   * - ``make lfs-pull``
     - Install and pull Git LFS files
   * - ``make create-db``
     - Create pylovo_db in postgres container
   * - ``make build``
     - Build Docker container
   * - ``make rebuild``
     - Force rebuild without cache

Container Management
--------------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Command
     - Description
   * - ``make up``
     - Start containers in background
   * - ``make down``
     - Stop and remove containers
   * - ``make restart``
     - Restart containers
   * - ``make shell``
     - Open interactive shell in container
   * - ``make logs``
     - View container logs
   * - ``make status``
     - Show container status

PyLovo Commands
---------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Command
     - Description
   * - ``make process COUNTRY=netherlands STATE=utrecht WORKERS=10``
     - **Full pipeline** (datapipeline + constructor + grid)
   * - ``make datapipeline COUNTRY=germany STATE=hamburg``
     - Run data pipeline for a region
   * - ``make constructor COUNTRY=germany STATE=hamburg``
     - Build database for a region
   * - ``make grid COUNTRY=germany STATE=hamburg WORKERS=10``
     - Generate grids with N workers
   * - ``make prepare-netherlands``
     - Download and prepare Dutch data
   * - ``make prepare-country COUNTRY=spain``
     - Prepare data for other countries
   * - ``make run CMD="python --version"``
     - Run custom command in container


API Endpoints
=============

Once running, the API is available at ``http://localhost:8086``:

Grid Generation
---------------

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Endpoint
     - Method
     - Description
   * - ``/health``
     - GET
     - Health check and status
   * - ``/generate-grid``
     - POST
     - Generate grid for custom polygon
   * - ``/grid-statistics``
     - POST
     - Get statistics for generated grid
   * - ``/power-flow``
     - POST
     - Run power flow analysis

Configuration
-------------

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Endpoint
     - Method
     - Description
   * - ``/transformer-sizes``
     - GET
     - Get available transformer sizes
   * - ``/consumer-categories``
     - GET
     - Get consumer category definitions
   * - ``/cable-types``
     - GET
     - Get cable type catalog
   * - ``/equipment-costs``
     - GET
     - Get equipment cost data
   * - ``/voltage-settings``
     - GET
     - Get voltage band settings

Energy & Buildings
------------------

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Endpoint
     - Method
     - Description
   * - ``/estimate-energy``
     - POST
     - Estimate energy demand for buildings
   * - ``/estimate-energy-batch``
     - POST
     - Batch energy estimation
   * - ``/hosting-capacity``
     - POST
     - Calculate EV hosting capacity
   * - ``/add-custom-building``
     - POST
     - Add custom building
   * - ``/custom-buildings/{user_id}``
     - GET
     - Get user's custom buildings

Pipeline
--------

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Endpoint
     - Method
     - Description
   * - ``/pipeline/run``
     - POST
     - Start data pipeline job
   * - ``/pipeline/status/{job_id}``
     - GET
     - Get pipeline job status
   * - ``/pipeline/regions``
     - GET
     - Get available regions
   * - ``/pipeline/history``
     - GET
     - Get pipeline execution history


Available Regions
=================

PyLovo supports grid generation for several European countries.

**Supported Countries:**

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Country
     - Key
     - Data Source
   * - **Germany**
     - ``germany``
     - Postcode boundaries included in repository
   * - **Netherlands**
     - ``netherlands``
     - CBS (Statistics Netherlands) & PDOK
   * - **Austria**
     - ``austria``
     - GADM (Level 2 Districts)
   * - **Spain**
     - ``spain``
     - OpenDataSoft (Postal Codes)
   * - **Czech Republic**
     - ``czech_republic``
     - OpenDataSoft (Districts)

Country Data Preparation
------------------------

For non-German countries, you must first download and prepare the postcode boundary data.

**1. Germany:**
Data is already included. No preparation needed.

**2. Netherlands:**
Requires manual download of population data (Key Figures) due to licensing/availability.

.. code-block:: bash

    # 1. Download 'Kerncijfers wijken en buurten 2022' (Excel/Zip) from CBS
    # 2. Place kwb.zip in: raw_data/netherlands/downloads/
    # 3. Run preparation script:
    make prepare-netherlands

**3. Austria:**
Automatically downloads district boundaries from GADM.

.. code-block:: bash

    make prepare-austria

**4. Spain & Czech Republic:**
Automatically downloads data from OpenDataSoft.

.. code-block:: bash

    make prepare-country COUNTRY=spain
    make prepare-country COUNTRY=czech_republic


**Germany States:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - State Key
     - Name
   * - baden_wuerttemberg
     - Baden-Württemberg
   * - bayern
     - Bayern (Bavaria)
   * - berlin
     - Berlin
   * - brandenburg
     - Brandenburg
   * - bremen
     - Bremen
   * - hamburg
     - Hamburg
   * - hessen
     - Hessen
   * - mecklenburg_vorpommern
     - Mecklenburg-Vorpommern
   * - niedersachsen
     - Niedersachsen
   * - nordrhein_westfalen
     - Nordrhein-Westfalen
   * - rheinland_pfalz
     - Rheinland-Pfalz
   * - saarland
     - Saarland
   * - sachsen
     - Sachsen
   * - sachsen_anhalt
     - Sachsen-Anhalt
   * - schleswig_holstein
     - Schleswig-Holstein
   * - thueringen
     - Thüringen

**Netherlands Provinces:**

- noord_holland, zuid_holland, utrecht, flevoland, gelderland, etc.

**Austria States:**

- wien, burgenland, kaernten, nieroesterreich, oberoesterreich, salzburg, steiermark, tirol, vorarlberg


Troubleshooting
===============

PostgreSQL Checkpoint Warnings
------------------------------

If you see frequent checkpoint warnings during datapipeline or constructor:

.. code-block:: text

    LOG:  checkpoints are occurring too frequently (3 seconds apart)
    HINT:  Consider increasing the configuration parameter "max_wal_size".

Increase the WAL size from default 1GB to 4GB:

.. code-block:: bash

    # Increase max_wal_size to 4GB (recommended for large data imports)
    docker exec postgres psql -U postgres -c "ALTER SYSTEM SET max_wal_size = '4GB';"
    docker exec postgres psql -U postgres -c "ALTER SYSTEM SET checkpoint_completion_target = '0.9';"
    docker exec postgres psql -U postgres -c "SELECT pg_reload_conf();"

This reduces checkpoint frequency during heavy write operations like bulk imports.

Container Won't Start
---------------------

.. code-block:: bash

    # Check container logs
    docker logs pylovo-api-1

    # Rebuild without cache
    docker compose build --no-cache

Database Connection Failed
--------------------------

.. code-block:: bash

    # Test database connectivity
    docker exec pylovo-api-1 python -c "
    import psycopg2
    conn = psycopg2.connect(
        host='host.docker.internal',
        port=5433,
        dbname='pylovo_db',
        user='postgres',
        password='postgres'
    )
    print('Connected!')
    conn.close()
    "

LFS Files Missing
-----------------

.. code-block:: bash

    # Check if raw_data.7z exists
    ls -lh raw_data.7z
    
    # If missing, re-pull
    git lfs pull --include="*.7z"


.. seealso::

   - :doc:`../architecture/index` - System architecture
   - :doc:`../api/index` - Complete API documentation
