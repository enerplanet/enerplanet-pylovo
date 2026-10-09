System Architecture
===================

A comprehensive energy planning and simulation platform for renewable energy assessment.

.. contents:: Table of Contents
   :local:
   :depth: 2

Overview
--------

Enerplanet is a full-stack energy planning platform that enables:

- **Interactive map-based location selection** for energy projects
- **Weather data integration** for solar/wind resource assessment
- **Energy simulation** using Calliope/PyPSA optimization models
- **Synthetic grid generation** via PyLovo integration
- **Multi-tenant workspace management** with role-based access

System Architecture Diagram
---------------------------

.. raw:: html

   <div style="background: #f8f9fa; padding: 30px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <!-- Title -->
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 18px; margin-bottom: 25px; border-bottom: 2px solid #333; padding-bottom: 15px;">ENERPLANET SYSTEM ARCHITECTURE</div>
   
   <!-- CLIENTS LAYER -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px; margin-bottom: 15px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 14px; margin-bottom: 15px;">CLIENTS</div>
   <div style="display: flex; justify-content: center; gap: 40px; flex-wrap: wrap;">
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 12px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">Browser</div>
   <div style="font-size: 11px; color: #555;">(React/Vite)</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 12px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">API Client</div>
   <div style="font-size: 11px; color: #555;">(REST/JSON)</div>
   </div>
   
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- NGINX PROXY -->
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px; margin-bottom: 15px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 14px; margin-bottom: 10px;">NGINX REVERSE PROXY (Port 80/443)</div>
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; font-size: 11px;">
   <div style="background: white; border: 1px solid #333; border-radius: 4px; padding: 8px 12px;"><code>/api/*</code> → Backend</div>
   <div style="background: white; border: 1px solid #333; border-radius: 4px; padding: 8px 12px;"><code>/auth/*</code> → Keycloak</div>
   <div style="background: white; border: 1px solid #333; border-radius: 4px; padding: 8px 12px;"><code>/geoserver/*</code> → GeoServer</div>
   <div style="background: white; border: 1px solid #333; border-radius: 4px; padding: 8px 12px;"><code>/*</code> → Frontend</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- MAIN SERVICES ROW -->
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; margin-bottom: 15px;">
   
   <!-- Frontend -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 180px; flex: 1; max-width: 220px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 13px; border-bottom: 1px solid #ccc; padding-bottom: 8px; margin-bottom: 8px;">FRONTEND</div>
   <div style="font-size: 11px; color: #555; text-align: center;">React 19</div>
   <div style="font-size: 10px; color: #666; margin-top: 8px;">
   • Vite 7<br>
   • OpenLayers<br>
   • TanStack Query<br>
   • Zustand<br>
   • Apache ECharts<br>
   • i18n (8 langs)
   </div>
   </div>
   
   <!-- Backend -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 180px; flex: 1; max-width: 220px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 13px; border-bottom: 1px solid #ccc; padding-bottom: 8px; margin-bottom: 8px;">BACKEND</div>
   <div style="font-size: 11px; color: #555; text-align: center;">Go 1.22 | Port: 8000</div>
   <div style="font-size: 10px; color: #666; margin-top: 8px;">
   • Gin Framework<br>
   • GORM ORM<br>
   • REST API<br>
   • Session mgmt<br>
   • Job queuing
   </div>
   </div>
   
   <!-- Keycloak -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 180px; flex: 1; max-width: 220px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 13px; border-bottom: 1px solid #ccc; padding-bottom: 8px; margin-bottom: 8px;">KEYCLOAK</div>
   <div style="font-size: 11px; color: #555; text-align: center;">v26.0.6 | Port: 8080</div>
   <div style="font-size: 10px; color: #666; margin-top: 8px;">
   • OIDC/OAuth2<br>
   • RBAC<br>
   • SSO<br>
   • User Groups<br>
   • Realm config
   </div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- MICROSERVICES ROW -->
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; margin-bottom: 15px;">
   
   <!-- Auth Service -->
   <div style="background: #f8f9fa; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 160px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 12px; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">AUTH-SERVICE</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Go/Gin | Port: 8001</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   • Keycloak sync<br>
   • Token validate<br>
   • User/Group mgmt
   </div>
   </div>
   
   <!-- Webservice -->
   <div style="background: #f8f9fa; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 160px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 12px; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">WEBSERVICE</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Go/Gin | Port: 8082</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   • Docker Webserv comm<br>
   • Instance mgmt<br>
   • Callback handle
   </div>
   </div>
   
   <!-- GeoServer -->
   <div style="background: #f8f9fa; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 160px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 12px; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">GEOSERVER</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Go/Gin | Port: 8083</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   • WMS/WFS proxy<br>
   • Map tile serve<br>
   • PostGIS query
   </div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- DOCKER WEBSERVICE -->
   <div style="background: white; border: 2px dashed #333; border-radius: 8px; padding: 20px; margin-bottom: 15px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 14px; margin-bottom: 15px;">DOCKER WEBSERVICE (Simulation Containers)</div>
   
   <div style="display: flex; justify-content: center; gap: 12px; flex-wrap: wrap; margin-bottom: 15px;">
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">PV</div>
   <div style="font-size: 9px; color: #555;">PySAM • PVLib<br>MERRA-2</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">WIND</div>
   <div style="font-size: 9px; color: #555;">PySAM<br>MERRA-2</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">BIOMASS</div>
   <div style="font-size: 9px; color: #555;">NREL data<br>Biogas calc</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">GEOTHERMAL</div>
   <div style="font-size: 9px; color: #555;">NREL data<br>Heat pump</div>
   </div>
   
   </div>
   
   <div style="background: #e9ecef; border: 1px solid #333; border-radius: 6px; padding: 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">CALLIOPE / PyPSA</div>
   <div style="font-size: 10px; color: #555;">Energy System Optimization & Power Flow Analysis</div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- PYLOVO -->
   <div style="background: white; border: 2px dashed #333; border-radius: 8px; padding: 20px; margin-bottom: 15px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 14px; margin-bottom: 15px;">PYLOVO (Grid Generation) — Port: 8086</div>
   
   <!-- Load Balancer -->
   <div style="background: #e9ecef; border: 1px solid #333; border-radius: 6px; padding: 10px; text-align: center; margin-bottom: 15px;">
   <div style="font-weight: bold; color: #000; font-size: 12px;">NGINX LOAD BALANCER</div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 16px; margin: 8px 0;">▼</div>
   
   <!-- API Instances -->
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; margin-bottom: 15px;">
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 110px;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">PyLovo API-1</div>
   <div style="font-size: 9px; color: #555;">FastAPI</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 110px;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">PyLovo API-2</div>
   <div style="font-size: 9px; color: #555;">FastAPI</div>
   </div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 15px; text-align: center; min-width: 110px;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">PyLovo API-3</div>
   <div style="font-size: 9px; color: #555;">FastAPI</div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 16px; margin: 8px 0;">▼</div>
   
   <!-- Redis Cache -->
   <div style="background: #e9ecef; border: 1px solid #333; border-radius: 6px; padding: 10px; text-align: center; margin-bottom: 15px; max-width: 200px; margin-left: auto; margin-right: auto;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">PyLovo Redis</div>
   <div style="font-size: 9px; color: #555;">Job queue • Result cache</div>
   </div>
   
   <!-- Capabilities -->
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 12px; font-size: 10px; color: #555;">
   <div style="font-weight: bold; color: #000; margin-bottom: 5px;">Capabilities:</div>
   <div style="display: flex; flex-wrap: wrap; gap: 10px; justify-content: center;">
   <span>• Synthetic LV grid generation</span>
   <span>• Building classification (150+ types)</span>
   <span>• Transformer placement</span>
   <span>• Cable routing (pgRouting)</span>
   <span>• Power flow analysis</span>
   <span>• MV line generation (MST)</span>
   </div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 10px 0;">▼</div>
   
   <!-- DATA STORES -->
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 20px;">
   <div style="text-align: center; font-weight: bold; color: white; font-size: 14px; margin-bottom: 15px;">DATA STORES</div>
   
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap;">
   
   <!-- PostgreSQL -->
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 15px; min-width: 180px; flex: 1; max-width: 220px;">
   <div style="font-weight: bold; color: #000; font-size: 12px; text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">POSTGRESQL 17</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Port: 5433</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   <strong>Databases:</strong><br>
   • spatialai<br>
   • pylovo_db<br><br>
   <strong>Extensions:</strong><br>
   • PostGIS 3.5<br>
   • pgRouting 3.7
   </div>
   </div>
   
   <!-- Redis -->
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 15px; min-width: 140px; flex: 1; max-width: 180px;">
   <div style="font-weight: bold; color: #000; font-size: 12px; text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">REDIS 7</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Port: 6379</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   • Sessions<br>
   • Job queues<br>
   • API cache<br>
   • Pub/Sub
   </div>
   </div>
   
   <!-- File Storage -->
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 15px; min-width: 140px; flex: 1; max-width: 180px;">
   <div style="font-weight: bold; color: #000; font-size: 12px; text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 6px; margin-bottom: 6px;">FILE STORAGE</div>
   <div style="font-size: 10px; color: #555; text-align: center;">Local/Volume</div>
   <div style="font-size: 9px; color: #666; margin-top: 6px;">
   • Simulation results<br>
   • Exported data<br>
   • User uploads
   </div>
   </div>
   
   </div>
   </div>
   
   </div>


Core Components
---------------

Frontend (enerplanet/frontend)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

React-based web application with client-side routing:

.. list-table::
   :widths: 20 50
   :header-rows: 1

   * - Feature
     - Description
   * - Interactive Map
     - OpenLayers for location selection and visualization
   * - Model Dashboard
     - Create, manage, and run energy models
   * - Simulation Charts
     - Apache ECharts for energy production/consumption visualization
   * - Technologies Panel
     - Configure PV, Wind, Battery, Biomass, Geothermal
   * - Custom Locations
     - Save and share custom map locations
   * - Multi-language
     - i18n support for 8 languages (EN, DE, FR, ES, IT, NL, PL, CS)

**Key Dependencies:**

- React 19 with TypeScript
- Vite 7 for build tooling
- OpenLayers for mapping
- TanStack Query for data fetching
- Zustand for state management
- Tailwind CSS 4 + shadcn/ui components
- Apache ECharts for charts

Backend (enerplanet/backend)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Go/Gin REST API providing:

.. list-table::
   :widths: 20 50
   :header-rows: 1

   * - Handler
     - Description
   * - ``/api/models``
     - Model CRUD operations, workspace filtering, model sharing
   * - ``/api/simulations``
     - Run Calliope/PyPSA simulations, job queue management, status tracking
   * - ``/api/results``
     - Simulation results retrieval, energy balance data, time series
   * - ``/api/locations``
     - Custom location CRUD, location sharing between users
   * - ``/api/weather``
     - Weather forecast data proxy (Open-Meteo API)
   * - ``/api/pylovo``
     - Synthetic grid generation, building data, transformer placement
   * - ``/api/technologies``
     - Technology configuration (PV, Wind, Battery, Biomass, Geothermal)
   * - ``/api/feedback``
     - User feedback submission, attachments, admin review
   * - ``/api/users``
     - User management, preferences, profile settings
   * - ``/api/groups``
     - Group management, member assignments, permissions
   * - ``/api/workspaces``
     - Workspace CRUD, member management, workspace sharing
   * - ``/api/notifications``
     - User notifications, read status, notification preferences
   * - ``/api/settings``
     - Application settings, user preferences, system configuration

**Backend Structure:**

::

    backend/
    ├── cmd/
    │   ├── main.go          # Application entry point
    │   ├── migrate/         # Database migrations
    │   └── seed/            # Initial data seeding
    ├── internal/
    │   ├── api/             # Route definitions
    │   ├── handler/         # Request handlers
    │   │   ├── model/       # Model operations
    │   │   ├── simulation/  # Simulation runner
    │   │   ├── weather/     # Weather data
    │   │   ├── pylovo/      # Grid generation
    │   │   └── ...
    │   ├── services/        # Business logic
    │   ├── config/          # Configuration
    │   └── worker/          # Background job processing
    └── migrations/          # SQL migration files


Platform Core (platform-core/)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Shared microservices:

**Auth Service (platform-core/auth-service)**

- Keycloak integration layer
- User synchronization
- Group/workspace management
- Token validation

**Webservice (platform-core/webservice)**

- Docker Webservice communication layer
- Manages simulation instances (auto-scaling, concurrency)
- Routes requests to external simulation containers
- Weather forecast proxy (Open-Meteo API)
- Callback handling for simulation results

**Docker Webservice (External)**

The actual energy simulations run in separate Docker containers:

- PV simulation (PySAM/PVLib + MERRA-2 data)
- Wind simulation (PySAM + MERRA-2 data)
- Biomass/Geothermal calculations (NREL data)
- Calliope/PyPSA energy optimization

Infrastructure (infrastructure/)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Docker Compose configurations:

**Common (infrastructure/common)**

- PostgreSQL with PostGIS
- Redis

**Platform (infrastructure/platform)**

- Keycloak
- Database configuration
- Logging stack

Technology Stack
----------------

Languages & Frameworks
^^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 40
   :header-rows: 1

   * - Component
     - Technology
   * - Frontend
     - TypeScript, React 18, Vite
   * - Backend
     - Go 1.22, Gin
   * - Auth Service
     - Go, Gin
   * - Webservice
     - Go, Gin
   * - PyLovo
     - Python 3.12, FastAPI

Databases
^^^^^^^^^

.. list-table::
   :widths: 20 40
   :header-rows: 1

   * - Database
     - Purpose
   * - PostgreSQL 17
     - Primary data store (pgrouting/pgrouting:17-3.5-3.7.3)
   * - PostGIS 3.5
     - Geospatial extensions
   * - pgRouting 3.7
     - Network routing (PyLovo)
   * - Redis 7
     - Caching, job queues, sessions

External APIs
^^^^^^^^^^^^^

.. list-table::
   :widths: 20 40
   :header-rows: 1

   * - API
     - Purpose
   * - Open-Meteo
     - Weather forecast data
   * - MERRA-2 Database
     - Historical weather for simulations
   * - NREL PySAM
     - PV/Wind/Geothermal simulations
   * - OpenStreetMap
     - Building/road data
   * - Geofabrik
     - OSM extracts for PyLovo

Data Flow
---------

Model Creation Flow
^^^^^^^^^^^^^^^^^^^

.. raw:: html

   <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 16px; margin-bottom: 20px; border-bottom: 2px solid #333; padding-bottom: 10px;">MODEL CREATION FLOW</div>
   
   <div style="display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 15px;">
   
   <!-- Step 1 -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #666; margin-bottom: 5px;">STEP 1</div>
   <div style="font-weight: bold; color: #000; font-size: 14px;">User</div>
   <div style="font-size: 11px; color: #555;">Draw Polygon</div>
   </div>
   
   <div style="color: #333; font-size: 24px; font-weight: bold;">→</div>
   
   <!-- Step 2 -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #666; margin-bottom: 5px;">STEP 2</div>
   <div style="font-weight: bold; color: #000; font-size: 14px;">Frontend</div>
   <div style="font-size: 11px; color: #555;">GeoJSON Request</div>
   </div>
   
   <div style="color: #333; font-size: 24px; font-weight: bold;">→</div>
   
   <!-- Step 3 -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #666; margin-bottom: 5px;">STEP 3</div>
   <div style="font-weight: bold; color: #000; font-size: 14px;">Backend</div>
   <div style="font-size: 11px; color: #555;">Create Model</div>
   </div>
   
   <div style="color: #333; font-size: 24px; font-weight: bold;">→</div>
   
   <!-- Step 4 -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #666; margin-bottom: 5px;">STEP 4</div>
   <div style="font-weight: bold; color: #000; font-size: 14px;">PyLovo</div>
   <div style="font-size: 11px; color: #555;">Fetch Buildings</div>
   </div>
   
   <div style="color: #333; font-size: 24px; font-weight: bold;">→</div>
   
   <!-- Step 5 -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #666; margin-bottom: 5px;">STEP 5</div>
   <div style="font-weight: bold; color: #000; font-size: 14px;">Statistics</div>
   <div style="font-size: 11px; color: #555;">Area Calculation</div>
   </div>
   
   <div style="color: #333; font-size: 24px; font-weight: bold;">→</div>
   
   <!-- Step 6 -->
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 120px;">
   <div style="font-size: 12px; color: #ccc; margin-bottom: 5px;">COMPLETE</div>
   <div style="font-weight: bold; color: white; font-size: 14px;">Ready</div>
   <div style="font-size: 11px; color: #ccc;">Model Configured</div>
   </div>
   
   </div>
   </div>


Simulation Flow
^^^^^^^^^^^^^^^

.. raw:: html

   <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 16px; margin-bottom: 20px; border-bottom: 2px solid #333; padding-bottom: 10px;">SIMULATION FLOW</div>
   
   <!-- Row 1: User Input -->
   <div style="display: flex; justify-content: center; margin-bottom: 20px;">
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 20px 30px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 16px;">Configure Technologies</div>
   <div style="font-size: 12px; color: #555; margin-top: 5px;">PV • Wind • Battery • Biomass • Geothermal</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Row 2: Backend Processing -->
   <div style="display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; margin-bottom: 20px;">
   
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">Backend</div>
   <div style="font-size: 11px; color: #555;">Queue Job</div>
   </div>
   
   <div style="color: #333; font-size: 24px; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">Redis Queue</div>
   <div style="font-size: 11px; color: #555;">Job Management</div>
   </div>
   
   <div style="color: #333; font-size: 24px; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">Webservice</div>
   <div style="font-size: 11px; color: #555;">Orchestration</div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Row 3: Docker Simulations -->
   <div style="background: #e9ecef; border: 2px dashed #333; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
   <div style="text-align: center; font-weight: bold; margin-bottom: 15px; font-size: 14px; color: #000;">Docker Webservice - Simulation Containers</div>
   <div style="display: flex; justify-content: center; gap: 12px; flex-wrap: wrap;">
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">PV</div>
   <div style="font-size: 10px; color: #555;">PySAM/PVLib</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Wind</div>
   <div style="font-size: 10px; color: #555;">PySAM/MERRA-2</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Battery</div>
   <div style="font-size: 10px; color: #555;">Storage Opt.</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Biomass</div>
   <div style="font-size: 10px; color: #555;">NREL Data</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Geothermal</div>
   <div style="font-size: 10px; color: #555;">NREL Data</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 12px 16px; text-align: center; min-width: 90px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Optimizer</div>
   <div style="font-size: 10px; color: #555;">Calliope/PyPSA</div>
   </div>
   
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Row 4: Results -->
   <div style="display: flex; justify-content: center; gap: 20px; flex-wrap: wrap;">
   
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">PostgreSQL</div>
   <div style="font-size: 11px; color: #555;">Store Results</div>
   </div>
   
   <div style="color: #333; font-size: 24px; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: #000;">ECharts</div>
   <div style="font-size: 11px; color: #555;">Visualization</div>
   </div>
   
   <div style="color: #333; font-size: 24px; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 15px 20px; text-align: center; min-width: 140px;">
   <div style="font-weight: bold; color: white;">Results</div>
   <div style="font-size: 11px; color: #ccc;">Charts & Metrics</div>
   </div>
   
   </div>
   </div>


Grid Generation Flow (PyLovo)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. raw:: html

   <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 16px; margin-bottom: 20px; border-bottom: 2px solid #333; padding-bottom: 10px;">GRID GENERATION FLOW (PyLovo)</div>
   
   <!-- Input -->
   <div style="display: flex; justify-content: center; margin-bottom: 20px;">
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 20px 40px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 16px;">Request Grid Generation</div>
   <div style="font-size: 12px; color: #555;">Polygon + Configuration Parameters</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Load Balancer -->
   <div style="display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; margin-bottom: 20px;">
   
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 25px; text-align: center;">
   <div style="font-weight: bold; color: #000;">Backend</div>
   <div style="font-size: 11px; color: #555;">POST /api/pylovo</div>
   </div>
   
   <div style="color: #333; font-size: 24px; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px 25px; text-align: center;">
   <div style="font-weight: bold; color: #000;">Nginx LB</div>
   <div style="font-size: 11px; color: #555;">Port 8086</div>
   </div>
   
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- PyLovo Instances -->
   <div style="background: #e9ecef; border: 2px dashed #333; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
   <div style="text-align: center; font-weight: bold; margin-bottom: 15px; font-size: 14px; color: #000;">PyLovo FastAPI Cluster</div>
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap;">
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 15px 20px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Instance 1</div>
   <div style="font-size: 10px; color: #555;">Port 8001</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 15px 20px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Instance 2</div>
   <div style="font-size: 10px; color: #555;">Port 8002</div>
   </div>
   
   <div style="background: white; border: 1px solid #333; border-radius: 6px; padding: 15px 20px; text-align: center; min-width: 100px;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">Instance 3</div>
   <div style="font-size: 10px; color: #555;">Port 8003</div>
   </div>
   
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Processing Steps -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
   <div style="text-align: center; font-weight: bold; color: #000; margin-bottom: 15px; font-size: 14px;">Grid Generation Pipeline</div>
   <div style="display: flex; justify-content: center; gap: 8px; flex-wrap: wrap;">
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">1. Load Buildings</div>
   <div style="font-size: 9px; color: #555;">From PostgreSQL</div>
   </div>
   
   <div style="color: #333; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">2. K-Means</div>
   <div style="font-size: 9px; color: #555;">Clustering</div>
   </div>
   
   <div style="color: #333; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">3. Transformers</div>
   <div style="font-size: 9px; color: #555;">Placement</div>
   </div>
   
   <div style="color: #333; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">4. Cable Routing</div>
   <div style="font-size: 9px; color: #555;">pgRouting</div>
   </div>
   
   <div style="color: #333; align-self: center; font-weight: bold;">→</div>
   
   <div style="background: #f8f9fa; border: 1px solid #333; border-radius: 6px; padding: 10px 12px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 11px;">5. Power Flow</div>
   <div style="font-size: 9px; color: #555;">Analysis</div>
   </div>
   
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 24px; margin: 10px 0;">▼</div>
   
   <!-- Output -->
   <div style="display: flex; justify-content: center; gap: 20px; flex-wrap: wrap;">
   
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 20px 30px; text-align: center;">
   <div style="font-weight: bold; color: white; font-size: 16px;">GeoJSON Response</div>
   <div style="font-size: 12px; color: #ccc;">Buildings • Transformers • Cables • Statistics</div>
   </div>
   
   </div>
   </div>


PyLovo Integration
------------------

The backend communicates with PyLovo for:

.. list-table::
   :widths: 30 50
   :header-rows: 1

   * - Endpoint
     - Purpose
   * - ``POST /generate-grid``
     - Generate LV grid for polygon
   * - ``GET /transformer-sizes``
     - Available transformer capacities
   * - ``GET /consumer-categories``
     - Building type classifications
   * - ``POST /estimate-energy``
     - Energy demand estimation
   * - ``POST /hosting-capacity``
     - Hosting capacity calculation

Configuration in ``.env``:

.. code-block:: bash

    PYLOVO_SERVICE_URL=http://10.1.66.22:8086


Workspace & Group Management
----------------------------

Multi-Tenancy Model
^^^^^^^^^^^^^^^^^^^

.. raw:: html

   <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 16px; margin-bottom: 20px; border-bottom: 2px solid #333; padding-bottom: 10px;">MULTI-TENANCY HIERARCHY</div>
   
   <!-- Organization -->
   <div style="display: flex; justify-content: center; margin-bottom: 15px;">
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 15px 40px; text-align: center;">
   <div style="font-weight: bold; color: white; font-size: 14px;">ORGANIZATION</div>
   <div style="font-size: 11px; color: #ccc;">Top-level entity</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 8px 0;">│</div>
   
   <!-- Workspaces -->
   <div style="display: flex; justify-content: center; margin-bottom: 15px;">
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px 30px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">WORKSPACES</div>
   <div style="font-size: 11px; color: #555;">(Keycloak Groups)</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 8px 0;">│</div>
   
   <!-- Users with Roles -->
   <div style="display: flex; justify-content: center; margin-bottom: 15px;">
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px 30px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">USERS</div>
   <div style="font-size: 11px; color: #555;">(with assigned roles)</div>
   </div>
   </div>
   
   <div style="text-align: center; color: #333; font-size: 20px; margin: 8px 0;">│</div>
   
   <!-- Models -->
   <div style="display: flex; justify-content: center;">
   <div style="background: #f8f9fa; border: 2px solid #333; border-radius: 8px; padding: 15px 30px; text-align: center;">
   <div style="font-weight: bold; color: #000; font-size: 13px;">MODELS</div>
   <div style="font-size: 11px; color: #555;">(belong to workspace)</div>
   </div>
   </div>
   
   </div>


Roles
^^^^^

.. raw:: html

   <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; margin: 20px 0; border: 2px solid #333;">
   
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 14px; margin-bottom: 15px; border-bottom: 1px solid #333; padding-bottom: 10px;">USER ROLES & PERMISSIONS</div>
   
   <div style="display: flex; justify-content: center; gap: 15px; flex-wrap: wrap;">
   
   <!-- Expert (Admin) -->
   <div style="background: #333; border: 2px solid #000; border-radius: 8px; padding: 15px; min-width: 170px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: white; font-size: 13px; margin-bottom: 8px;">EXPERT</div>
   <div style="text-align: center; font-size: 9px; color: #aaa; margin-bottom: 8px; border-bottom: 1px solid #555; padding-bottom: 6px;">(Administrator)</div>
   <div style="font-size: 9px; color: #ccc;">
   • Full system access<br>
   • User management<br>
   • Workspace management<br>
   • Unlimited buildings<br>
   • Unlimited models<br>
   • All simulations<br>
   • Export all data
   </div>
   </div>
   
   <!-- Manager -->
   <div style="background: #555; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 170px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: white; font-size: 13px; margin-bottom: 8px;">MANAGER</div>
   <div style="text-align: center; font-size: 9px; color: #bbb; margin-bottom: 8px; border-bottom: 1px solid #666; padding-bottom: 6px;">(Group Leader)</div>
   <div style="font-size: 9px; color: #ccc;">
   • Own group access<br>
   • Group management<br>
   • Unlimited buildings<br>
   • <strong>100 models</strong> maximum<br>
   • Run all simulations<br>
   • Export group data
   </div>
   </div>
   
   <!-- Intermediate -->
   <div style="background: #e9ecef; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 170px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 13px; margin-bottom: 8px;">INTERMEDIATE</div>
   <div style="text-align: center; font-size: 9px; color: #666; margin-bottom: 8px; border-bottom: 1px solid #999; padding-bottom: 6px;">(Standard User)</div>
   <div style="font-size: 9px; color: #555;">
   • Create/edit models<br>
   • <strong>100 buildings</strong> per polygon<br>
   • <strong>25 models</strong> maximum<br>
   • Run simulations<br>
   • View shared models<br>
   • Limited export
   </div>
   </div>
   
   <!-- Basic User -->
   <div style="background: white; border: 2px solid #333; border-radius: 8px; padding: 15px; min-width: 170px; flex: 1; max-width: 200px;">
   <div style="text-align: center; font-weight: bold; color: #000; font-size: 13px; margin-bottom: 8px;">BASIC USER</div>
   <div style="text-align: center; font-size: 9px; color: #666; margin-bottom: 8px; border-bottom: 1px solid #ccc; padding-bottom: 6px;">(Starter)</div>
   <div style="font-size: 9px; color: #555;">
   • Create models<br>
   • <strong>50 buildings</strong> per polygon<br>
   • <strong>10 models</strong> maximum<br>
   • Basic simulations<br>
   • View own models<br>
   • No export
   </div>
   </div>
   
   </div>
   </div>

Sharing
^^^^^^^

Models can be shared:

- **Private**: Only owner can access
- **Workspace**: All workspace members
- **Public**: Anyone with link (read-only)

Environment Configuration
-------------------------

Backend Environment Variables
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Core
    APP_NAME=Enerplanet
    APP_ENV=production
    APP_PORT=8000

    # Database
    DB_HOST=postgres
    DB_PORT=5432
    DB_DATABASE=spatialai
    DB_USERNAME=postgres
    DB_PASSWORD=<secret>

    # Redis
    REDIS_HOST=redis
    REDIS_PORT=6379

    # Services
    AUTH_SERVICE_URL=http://auth-service:8001
    WEBSERVICE_SERVICE_URL=http://webservice:8082
    PYLOVO_SERVICE_URL=http://pylovo:8086

    # Keycloak
    KEYCLOAK_URL=http://keycloak:8080
    KEYCLOAK_REALM=spatialhub
    KEYCLOAK_CLIENT_ID=spatialhub
    KEYCLOAK_CLIENT_SECRET=<secret>
