Production Deployment
******************************************

This section covers deploying pylovo in production environments with
HAProxy load balancing, Redis caching, and horizontal scaling.

.. contents:: Table of Contents
   :local:
   :depth: 2


Architecture Overview
=====================

The production deployment uses a scalable architecture designed to handle
100+ concurrent users:

::

    ┌─────────────────────────────────────────────────────────────────┐
    │                         Client Requests                          │
    └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                     HAProxy Load Balancer                        │
    │                      (Port 8086:80)                              │
    │                                                                   │
    │  • Round-robin load balancing                                     │
    │  • Health checks with automatic failover                          │
    │  • Stats dashboard on port 8404                                   │
    │  • Connection keepalive pooling                                   │
    └─────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
    ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
    │   PyLovo API 1    │ │   PyLovo API 2    │ │   PyLovo API 3    │
    │   (4 workers)     │ │   (4 workers)     │ │   (4 workers)     │
    │   WORKER_ID=1     │ │   WORKER_ID=2     │ │   WORKER_ID=3     │
    └───────────────────┘ └───────────────────┘ └───────────────────┘
                    │               │               │
                    └───────────────┼───────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
    ┌───────────────────────────┐   ┌───────────────────────────────┐
    │        Redis Cache        │   │     PostgreSQL + PostGIS      │
    │     (Shared across all    │   │      + pgRouting              │
    │        workers)           │   │        (pylovo_db)            │
    │   1GB LRU eviction        │   │                               │
    └───────────────────────────┘   │   host.docker.internal:5433   │
                                    └───────────────────────────────┘


Why 3 API Containers?
=====================

The production setup uses 3 API containers for several important reasons:

**High Availability**
    If one container crashes or becomes unresponsive, HAProxy automatically 
    routes traffic to the remaining healthy containers. Users experience no 
    downtime during single container failures.

**Load Distribution**
    HAProxy distributes incoming requests evenly across all 3 instances using 
    round-robin algorithm. This prevents any single container from becoming 
    overwhelmed during traffic spikes.

**Parallel Processing**
    Each container runs 4 Uvicorn workers, giving you 12 workers total. 
    Grid generation is CPU-intensive, so multiple containers allow parallel 
    processing of different requests.

**Resource Isolation**
    A heavy grid generation request in one container won't block API responses 
    in other containers. Each container has its own memory and CPU allocation.

**Zero-Downtime Deployments**
    You can restart containers one at a time during updates while the other 
    two continue serving requests.


Components
==========

HAProxy Load Balancer
---------------------

HAProxy distributes incoming requests across multiple PyLovo API instances
and provides health monitoring.

**Key Features:**

- **Round-robin routing**: Distributes requests evenly across all backends
- **Health checks**: Automatic failover if a worker becomes unresponsive
- **Stats dashboard**: Real-time monitoring at ``http://localhost:8404``
- **Connection pooling**: Keepalive connections reduce latency

**Configuration (haproxy/haproxy.cfg):**

.. code-block:: text

    global
        maxconn 4096
        log stdout format raw local0

    defaults
        mode http
        timeout connect 10s
        timeout client 600s
        timeout server 600s
        option httplog
        log global

    frontend http_front
        bind *:80
        default_backend pylovo_backend

    backend pylovo_backend
        balance roundrobin
        option httpchk GET /health
        http-check expect status 200
        
        server api1 pylovo-api-1:8086 check inter 5s fall 3 rise 2
        server api2 pylovo-api-2:8086 check inter 5s fall 3 rise 2
        server api3 pylovo-api-3:8086 check inter 5s fall 3 rise 2

    frontend stats
        bind *:8404
        stats enable
        stats uri /
        stats refresh 10s

**HAProxy Stats Dashboard:**

Access the real-time monitoring dashboard at ``http://localhost:8404``:

- View request counts per backend server
- Monitor response times and error rates
- Check health status of all API containers
- See active connections and queue depth


Redis Cache
-----------

Redis provides a shared cache layer across all API workers, storing:

- Consumer categories (rarely change)
- Transformer sizes (static data)
- Equipment specifications
- Frequently accessed query results

**Configuration:**

- Memory limit: 1GB (production) / 512MB (development)
- Eviction policy: ``allkeys-lru`` (Least Recently Used)
- Internal network only (no external port exposure)

**Cache Behavior:**

.. code-block:: python

    # Example: Cached consumer categories
    @cached(ttl=3600)  # Cache for 1 hour
    def get_consumer_categories():
        return db.fetch_consumer_categories()

When Redis is unavailable, the system falls back to in-memory caching
per worker instance.


PyLovo API Workers
------------------

Each API container runs Uvicorn with 4 workers:

- Connection pooling: 1-2 database connections per worker
- Unique ``WORKER_ID`` for logging and debugging
- Automatic restart on failure (``restart: unless-stopped``)

**Environment Variables:**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Description
   * - HOST
     - Database host (``host.docker.internal`` for Docker)
   * - PORT
     - Database port (default: 5433)
   * - DBNAME
     - Database name (``pylovo_db``)
   * - DBUSER
     - Database user
   * - PASSWORD
     - Database password
   * - REDIS_HOST
     - Redis host (``redis`` in Docker network)
   * - REDIS_PORT
     - Redis port (default: 6379)
   * - WORKER_ID
     - Unique worker identifier (1, 2, 3, ...)


Docker Compose Files
====================

PyLovo provides two Docker Compose configurations:

Production (environment/docker-compose.yml)
-------------------------------------------

Full production stack with 3 API instances, HAProxy, and Redis:

.. code-block:: yaml

    services:
      # HAProxy Load Balancer
      haproxy:
        image: haproxy:2.9-alpine
        container_name: pylovo-haproxy
        ports:
          - "8086:80"      # API endpoint
          - "8404:8404"    # Stats dashboard
        volumes:
          - ./haproxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
        depends_on:
          - pylovo-api-1
          - pylovo-api-2
          - pylovo-api-3
        restart: unless-stopped

      # Redis Cache (shared across all workers)
      redis:
        image: redis:7-alpine
        container_name: pylovo-redis
        command: redis-server --maxmemory 1gb --maxmemory-policy allkeys-lru
        restart: unless-stopped

      # PyLovo API Instance 1
      pylovo-api-1:
        build: .
        container_name: pylovo-api-1
        environment:
          - HOST=host.docker.internal
          - PORT=5433
          - DBNAME=pylovo_db
          - DBUSER=postgres
          - PASSWORD=postgres
          - REDIS_HOST=redis
          - REDIS_PORT=6379
          - WORKER_ID=1
        extra_hosts:
          - "host.docker.internal:host-gateway"
        depends_on:
          - redis
        restart: unless-stopped
        command: ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8086", "--workers", "4"]

      # Additional workers (pylovo-api-2, pylovo-api-3) follow same pattern

Development (environment/docker-compose.dev.yml)
------------------------------------------------

Single instance with hot reload for development:

.. code-block:: yaml

    services:
      haproxy:
        image: haproxy:2.9-alpine
        container_name: pylovo-haproxy-dev
        ports:
          - "8086:80"
          - "8404:8404"
        volumes:
          - ./haproxy/haproxy.dev.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro

      redis:
        image: redis:7-alpine
        command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru

      pylovo-api-dev:
        build: .
        container_name: pylovo-api-dev
        volumes:
          - ./api:/app/api          # Hot reload
          - ./src:/app/src          # Hot reload
        command: ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8086", "--reload"]


Deployment Instructions
=======================

Prerequisites
-------------

1. Docker and Docker Compose installed
2. PostgreSQL with PostGIS and pgRouting running
3. pylovo database initialized with ``make create-db``

Quick Start
-----------

**Development (with hot reload):**

.. code-block:: bash

    make dev
    # or: docker compose -f environment/docker-compose.dev.yml up -d

**Production (3 instances + HAProxy):**

.. code-block:: bash

    make prod
    # or: docker compose up -d

**Verify deployment:**

.. code-block:: bash

    # Check all containers are running
    docker compose ps

    # Check health endpoint
    curl http://localhost:8086/health

    # View HAProxy stats
    open http://localhost:8404

    # View logs
    make logs
    # or: docker compose logs -f


Access Points
-------------

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Endpoint
     - URL
     - Description
   * - API
     - ``http://localhost:8086``
     - Main API endpoint
   * - API Docs
     - ``http://localhost:8086/docs``
     - Swagger/OpenAPI documentation
   * - HAProxy Stats
     - ``http://localhost:8404``
     - Load balancer monitoring (production only)
   * - Health Check
     - ``http://localhost:8086/health``
     - Container health status


Scaling Workers
---------------

To add more workers, edit ``environment/docker-compose.yml``:

1. Copy an existing ``pylovo-api-*`` service definition
2. Update ``container_name`` and ``WORKER_ID`` to unique values
3. Add new server to ``haproxy/haproxy.cfg`` backend
4. Restart the stack:

.. code-block:: bash

    make down && make prod


Health Checks and Monitoring
============================

HAProxy Stats Dashboard
-----------------------

Access ``http://localhost:8404`` for real-time monitoring:

- **Green**: Server is healthy and receiving traffic
- **Yellow**: Server is in maintenance mode
- **Red**: Server failed health checks and is removed from pool

Health Endpoint
---------------

Each worker exposes a ``/health`` endpoint:

.. code-block:: bash

    curl http://localhost:8086/health

Response:

.. code-block:: json

    {
        "status": "healthy",
        "worker": "1",
        "database": "connected",
        "version": "1.0.0"
    }

Container Logs
--------------

.. code-block:: bash

    # All containers
    make logs

    # Specific container
    docker logs pylovo-api-1 --tail 100

    # HAProxy access logs
    docker logs pylovo-haproxy --tail 100


Troubleshooting
===============

Common Issues
-------------

**Connection Refused to Database:**

.. code-block:: bash

    # Check if host.docker.internal resolves correctly
    docker exec pylovo-api-1 ping host.docker.internal

    # Verify database is accessible
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

**Redis Connection Failed:**

The API will fall back to in-memory caching. To fix:

.. code-block:: bash

    # Check Redis is running
    docker exec pylovo-redis redis-cli ping
    # Should return: PONG

**All backends marked as DOWN in HAProxy:**

.. code-block:: bash

    # Check if API containers are running
    docker compose ps

    # Check API health directly
    docker exec pylovo-api-1 curl http://localhost:8086/health

**Worker Crashes:**

.. code-block:: bash

    # Check container status
    docker compose ps

    # View crash logs
    docker logs pylovo-api-1

    # Restart specific worker
    docker compose restart pylovo-api-1


Performance Tuning
==================

Database Connection Pool
------------------------

Each worker uses a small connection pool (1-2 connections) to prevent
overwhelming the database. With 3 containers × 4 workers = 12-24 total connections.

Redis Memory
------------

Increase Redis memory for larger deployments:

.. code-block:: yaml

    redis:
      command: redis-server --maxmemory 2gb --maxmemory-policy allkeys-lru

Adding More Containers
----------------------

For high-traffic deployments:

1. Add ``pylovo-api-4``, ``pylovo-api-5``, etc. to ``environment/docker-compose.yml``
2. Add corresponding servers to ``haproxy/haproxy.cfg``
3. Monitor database connection limits


Security Considerations
=======================

1. **Never expose Redis externally** - Keep Redis within Docker network only
2. **Use secrets for passwords** - Consider Docker secrets or environment files
3. **Enable HTTPS** - Add SSL termination at HAProxy level
4. **Restrict stats access** - Configure HAProxy stats with authentication
5. **Database credentials** - Use read-only users where possible

Example HAProxy SSL configuration:

.. code-block:: text

    frontend https_front
        bind *:443 ssl crt /etc/haproxy/certs/combined.pem
        default_backend pylovo_backend
