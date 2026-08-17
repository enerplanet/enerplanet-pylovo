# =============================================================================
# Pylovo Makefile
# =============================================================================
# Usage:
#   make setup     - Extract raw_data, create database, and build container
#   make build     - Build Docker container
#   make dev       - Start development container (hot reload)
#   make prod      - Start production containers (3 API instances + HAProxy)
#   make down      - Stop container
#   make shell     - Open shell in container
#   make logs      - View container logs
# =============================================================================

.PHONY: setup extract build up down restart shell logs clean help \
        datapipeline constructor repair-postcodes grid process-germany-chain delete delete-dry-run cleanup-user-dry-run cleanup-user \
        create-db lfs-pull dev prod prod-down dump-state load-state load-bremen

# Default target
.DEFAULT_GOAL := help

# Variables
CONTAINER_NAME_DEV := pylovo-api-dev
CONTAINER_NAME_PROD := pylovo-api-1

#   make <target> CONTAINER_NAME=pylovo-api-dev PGROUTING_CONTAINER=postgres
CONTAINER_NAME ?= $(CONTAINER_NAME_PROD)
COMPOSE_FILE_DEV := docker-compose.dev.yml
COMPOSE_FILE_PROD := docker-compose.yml
RAW_DATA_DIR := raw_data
RAW_DATA_ARCHIVE := raw_data.7z
DUMP_DIR := initial-data

# Database settings (from .env)
DB_HOST := localhost
DB_PORT := 5433
DB_USER := postgres
DB_PASSWORD := postgres
DB_NAME := pylovo_db
PGROUTING_CONTAINER ?= postgres
DB_HOST_EXEC ?= host.docker.internal
DB_PORT_EXEC ?= 5433

# Effective args support both uppercase and lowercase CLI vars:
#   make constructor COUNTRY=germany STATE=hamburg
#   make constructor country=germany state=hamburg
COUNTRY_EFFECTIVE = $(if $(country),$(country),$(if $(COUNTRY),$(COUNTRY),germany))
STATE_EFFECTIVE = $(if $(state),$(state),$(if $(STATE),$(STATE),hamburg))
WORKERS_EFFECTIVE = $(if $(workers),$(workers),$(if $(WORKERS),$(WORKERS),10))
GERMANY_CHAIN_STATES ?= bremen berlin hamburg brandenburg sachsen saarland thueringen bayern nordrhein_westfalen

# =============================================================================
# Setup & Build
# =============================================================================

## setup: Full setup - pull LFS files, create database and build container
setup: lfs-pull create-db build up
	@echo "[OK] Setup complete!"

## lfs-pull: Install and pull Git LFS files
lfs-pull:
	@echo "> Setting up Git LFS..."
	@which git-lfs > /dev/null 2>&1 || (echo "Installing git-lfs..." && sudo apt-get update && sudo apt-get install -y git-lfs)
	@git lfs install
	@echo "> Pulling LFS files..."
	@git lfs pull
	@echo "[OK] LFS files ready"

## extract: Extract raw_data.7z archive (handled by Dockerfile during build)
extract:
	@echo "> Extraction is handled automatically by Dockerfile during 'make build'"

## create-db: Create pylovo_db database in pgrouting container
create-db:
	@echo "> Creating database $(DB_NAME) in pgrouting container..."
	@docker exec $(PGROUTING_CONTAINER) psql -U $(DB_USER) -tc \
		"SELECT 1 FROM pg_database WHERE datname = '$(DB_NAME)'" | grep -q 1 || \
		docker exec $(PGROUTING_CONTAINER) psql -U $(DB_USER) -c \
		"CREATE DATABASE $(DB_NAME);"
	@echo "> Enabling PostGIS and pgRouting extensions..."
	@docker exec $(PGROUTING_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME) -c \
		"CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pgrouting;" 2>/dev/null || true
	@echo "[OK] Database $(DB_NAME) ready"

## build: Build Docker container
build:
	@echo "> Building Docker container..."
	docker compose build
	@echo "[OK] Build complete"

## rebuild: Force rebuild Docker container (no cache)
rebuild:
	@echo "> Rebuilding Docker container (no cache)..."
	docker compose build --no-cache
	@echo "[OK] Rebuild complete"

# =============================================================================
# Container Management
# =============================================================================

## create-network: Create external docker network if it doesn't exist
create-network:
	@docker network inspect spatialhub-net >/dev/null 2>&1 || \
		(echo "> Creating external network spatialhub-net..." && \
		docker network create spatialhub-net)

## dev: Start development container (hot reload, single API, no HAProxy)
dev: create-network
	@echo "> .env check..."
	@if [ ! -f .env.docker ]; then \
		echo "Creating .env.docker..."; \
		cp .env.example .env.docker; \
	fi
	@echo "> Starting development container..."
	docker compose -f $(COMPOSE_FILE_DEV) up -d --build
	@echo "[OK] Development container started at http://localhost:8086"
	@if [ -f "$(DUMP_DIR)/bremen.sql.gz" ]; then \
		echo "> Loading Bremen dataset..."; \
		sleep 5; \
		docker exec postgres bash -c \
			'psql -U postgres -d pylovo_db -tc "SELECT 1 FROM buildings_result LIMIT 1" 2>/dev/null | grep -q 1' \
			&& echo "[OK] Data already loaded, skipping" \
			|| (gunzip -c $(DUMP_DIR)/bremen.sql.gz | docker exec -i postgres psql -U postgres -d pylovo_db -q \
				&& echo "[OK] Bremen dataset loaded"); \
	fi

## prod: Start production containers (3 API instances + HAProxy)
prod: create-network
	@echo "> Starting production containers..."
	docker compose -f $(COMPOSE_FILE_PROD) pull postgres
	docker compose -f $(COMPOSE_FILE_PROD) up -d
	@echo "[OK] Production started at http://localhost:8086 (stats: http://localhost:8404)"

## up: Start container in background (alias for dev)
up: dev

## down: Stop and remove containers (both dev and prod)
down:
	@echo "> Stopping containers..."
	docker compose -f $(COMPOSE_FILE_DEV) down 2>/dev/null || true
	docker compose -f $(COMPOSE_FILE_PROD) down 2>/dev/null || true
	@echo "[OK] Containers stopped"

## prod-down: Stop production containers only
prod-down:
	@echo "> Stopping production containers..."
	docker compose -f $(COMPOSE_FILE_PROD) down
	@echo "[OK] Production containers stopped"

## restart: Restart development container
restart:
	@echo "> Restarting container..."
	docker compose -f $(COMPOSE_FILE_DEV) restart
	@echo "[OK] Container restarted"

## shell: Open interactive shell in container
shell:
	docker exec -it $(CONTAINER_NAME) bash

## logs: Show container logs (follow mode)
logs:
	docker compose -f $(COMPOSE_FILE_DEV) logs -f 2>/dev/null || docker compose -f $(COMPOSE_FILE_PROD) logs -f

## logs-prod: Show production container logs
logs-prod:
	docker compose -f $(COMPOSE_FILE_PROD) logs -f

## status: Show container status
status:
	@docker ps --filter "name=$(CONTAINER_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# =============================================================================
# Pylovo Commands
# =============================================================================

## datapipeline: Run datapipeline (usage: make datapipeline COUNTRY=germany STATE=hamburg)
datapipeline:
	@echo "> Running datapipeline for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.main --country $(COUNTRY_EFFECTIVE) --state $(STATE_EFFECTIVE) --no-cache
	@echo "[OK] Datapipeline complete"

## enrich: Run building enrichment only (usage: make enrich COUNTRY=netherlands STATE=utrecht)
enrich:
	@echo "> Running enrichment for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.main --country $(COUNTRY_EFFECTIVE) --state $(STATE_EFFECTIVE) --only-enrich $(if $(EP_ONLINE_KEY),--ep-online-key $(EP_ONLINE_KEY))
	@echo "[OK] Enrichment complete"

## constructor: Run constructor (usage: make constructor COUNTRY=germany STATE=hamburg)
constructor:
	@echo "> Running constructor for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python runme/main_constructor.py --datapipeline --country $(COUNTRY_EFFECTIVE) --state $(STATE_EFFECTIVE)
	@echo "[OK] Constructor complete"

## repair-postcodes: Refresh postcode/state mapping without state raw_data (usage: make repair-postcodes COUNTRY=germany)
repair-postcodes:
	@echo "> Repairing postcode/state mapping for $(COUNTRY_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python runme/main_constructor.py --repair-postcodes-only --country $(COUNTRY_EFFECTIVE)
	@echo "[OK] Postcode/state repair complete for $(COUNTRY_EFFECTIVE)"

## grid: Generate grid (usage: make grid COUNTRY=germany STATE=hamburg WORKERS=10)
grid:
	@echo "> Generating grid for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE) with $(WORKERS_EFFECTIVE) workers..."
	docker exec -e PYTHONUNBUFFERED=1 -it $(CONTAINER_NAME) python runme/create/generate_grid.py $(COUNTRY_EFFECTIVE) $(STATE_EFFECTIVE) --worker $(WORKERS_EFFECTIVE) $(if $(RESUME),--resume)
	@echo "[OK] Grid generation complete"

## process: Full pipeline - datapipeline + constructor + grid (usage: make process COUNTRY=netherlands STATE=utrecht WORKERS=10)
process: datapipeline constructor grid
	@echo "[OK] Full processing complete for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)"

## process-germany-chain: Run full pipeline for selected German states in sequence
process-germany-chain:
	@set -e; \
	for state in $(GERMANY_CHAIN_STATES); do \
		echo "> Full process for germany/$$state..."; \
		$(MAKE) process COUNTRY=germany STATE=$$state WORKERS=$(WORKERS_EFFECTIVE) CONTAINER_NAME=$(CONTAINER_NAME); \
	done
	@echo "[OK] Germany chain complete"

## delete-dry-run: Preview deletion impact for one state (usage: make delete-dry-run country=germany state=bayern)
delete-dry-run:
	@echo "> Preview delete impact for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python runme/delete/delete_state.py --country $(COUNTRY_EFFECTIVE) --state $(STATE_EFFECTIVE) --dry-run

## delete: Delete one state's data (usage: make delete country=germany state=bayern)
delete:
	@echo "> Deleting data for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)..."
	docker exec -it $(CONTAINER_NAME) python runme/delete/delete_state.py --country $(COUNTRY_EFFECTIVE) --state $(STATE_EFFECTIVE) --execute --drop-state-row
	@echo "[OK] Delete complete for $(COUNTRY_EFFECTIVE)/$(STATE_EFFECTIVE)"

## cleanup-user-dry-run: Preview legacy unscoped USER transformer cleanup
cleanup-user-dry-run:
	@echo "> Preview legacy USER transformer scope cleanup..."
	docker exec -e HOST=$(DB_HOST_EXEC) -e PORT=$(DB_PORT_EXEC) -it $(CONTAINER_NAME) python runme/delete/cleanup_user_transformer_scope.py --dry-run --version-id 1

## cleanup-user: Execute legacy unscoped USER transformer cleanup (safe mode)
cleanup-user:
	@echo "> Executing legacy USER transformer scope cleanup..."
	docker exec -e HOST=$(DB_HOST_EXEC) -e PORT=$(DB_PORT_EXEC) -it $(CONTAINER_NAME) python runme/delete/cleanup_user_transformer_scope.py --execute --version-id 1 --analyze
	@echo "[OK] USER transformer cleanup complete"

## prepare-netherlands: Download and prepare Netherlands data
prepare-netherlands:
	@echo "> Preparing Netherlands data..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.prepare_country netherlands
	@echo "[OK] Netherlands data preparation complete"

## prepare-germany: Enrich German postcode data with Bundesland state_code
prepare-germany:
	@echo "> Preparing Germany data..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.prepare_country germany
	@echo "[OK] Germany data preparation complete"

## prepare-austria: Download and prepare Austria postcode data
prepare-austria:
	@echo "> Preparing Austria data..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.prepare_country austria
	@echo "[OK] Austria data preparation complete"

## prepare-czech_republic: Download and prepare Czech Republic postcode data
prepare-czech_republic:
	@echo "> Preparing Czech Republic data..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.prepare_country czech_republic
	@echo "[OK] Czech Republic data preparation complete"

## prepare-country: Prepare data for any country (usage: make prepare-country COUNTRY=austria)
prepare-country:
	@echo "> Preparing $(COUNTRY) data..."
	docker exec -it $(CONTAINER_NAME) python -m datapipeline.prepare_country $(COUNTRY)
	@echo "[OK] $(COUNTRY) data preparation complete"

## run: Run custom command (usage: make run CMD="python --version")
run:
	docker exec -it $(CONTAINER_NAME) $(CMD)

# =============================================================================
# Data Dump / Load (for localhost dev)
# =============================================================================

## dump-state: Dump a processed state from pylovo DB (usage: make dump-state STATE=bremen)
dump-state:
	@mkdir -p $(DUMP_DIR)
	@echo "> Dumping $(STATE_EFFECTIVE) data from pylovo_db..."
	docker exec postgres pg_dump -U postgres -d pylovo_db \
		--data-only --no-owner --no-privileges \
		--exclude-table=spatial_ref_sys \
		| gzip > $(DUMP_DIR)/$(STATE_EFFECTIVE).sql.gz
	@echo "[OK] Dump saved to $(DUMP_DIR)/$(STATE_EFFECTIVE).sql.gz"

## load-state: Load a state dump into pylovo DB (usage: make load-state STATE=bremen)
load-state:
	@if [ ! -f "$(DUMP_DIR)/$(STATE_EFFECTIVE).sql.gz" ]; then \
		echo "ERROR: $(DUMP_DIR)/$(STATE_EFFECTIVE).sql.gz not found"; \
		echo "Run 'make process COUNTRY=germany STATE=$(STATE_EFFECTIVE)' first, then 'make dump-state STATE=$(STATE_EFFECTIVE)'"; \
		exit 1; \
	fi
	@echo "> Loading $(STATE_EFFECTIVE) data into pylovo_db..."
	gunzip -c $(DUMP_DIR)/$(STATE_EFFECTIVE).sql.gz | docker exec -i pylovo-postgres psql -U postgres -d pylovo_db -q
	@echo "[OK] $(STATE_EFFECTIVE) data loaded"

## load-bremen: Shortcut to load Bremen dataset
load-bremen:
	@$(MAKE) load-state STATE=bremen

# =============================================================================
# Cleanup
# =============================================================================

## clean: Remove container and images
clean:
	@echo "> Cleaning up..."
	docker compose down --rmi local -v 2>/dev/null || true
	@echo "[OK] Cleanup complete"

# =============================================================================
# Help
# =============================================================================

## help: Show this help message
help:
	@echo "Pylovo Makefile Commands"
	@echo ""
	@echo "Setup & Build:"
	@echo "  make setup              Full setup (extract + create db + build + start)"
	@echo "  make extract            Extract raw_data.7z archive"
	@echo "  make lfs-pull           Install git-lfs and pull LFS files"
	@echo "  make create-db          Create pylovo_db in pgrouting container"
	@echo "  make build              Build Docker container"
	@echo "  make rebuild            Force rebuild (no cache)"
	@echo ""
	@echo "Container Management:"
	@echo "  make dev                Start development (single API, no HAProxy)"
	@echo "  make prod               Start production (3 instances + HAProxy)"
	@echo "  make up                 Alias for 'make dev'"
	@echo "  make down               Stop all containers"
	@echo "  make prod-down          Stop production containers only"
	@echo "  make restart            Restart development container"
	@echo "  make shell              Open shell in container"
	@echo "  make logs               View container logs"
	@echo "  make logs-prod          View production logs"
	@echo "  make status             Show container status"
	@echo ""
	@echo "Pylovo Commands:"
	@echo "  make process COUNTRY=netherlands STATE=utrecht WORKERS=10"
	@echo "                          Full pipeline (datapipeline + constructor + grid)"
	@echo "  make datapipeline COUNTRY=germany STATE=hamburg"
	@echo "  make constructor COUNTRY=germany STATE=hamburg"
	@echo "  make repair-postcodes COUNTRY=germany"
	@echo "  make grid COUNTRY=germany STATE=hamburg WORKERS=10"
	@echo "  make process-germany-chain WORKERS=10"
	@echo "  make delete-dry-run country=germany state=bayern"
	@echo "  make delete country=germany state=bayern"
	@echo "  make cleanup-user-dry-run"
	@echo "  make cleanup-user"
	@echo "  make prepare-netherlands    Download & prepare Dutch data"
	@echo "  make prepare-germany        Enrich German postcodes with state_code"
	@echo "  make run CMD=\"python --version\""
	@echo ""
	@echo "Data Dump / Load:"
	@echo "  make dump-state STATE=bremen   Dump processed state to initial-data/"
	@echo "  make load-state STATE=bremen   Load state dump into pylovo_db"
	@echo "  make load-bremen               Shortcut: load Bremen dataset"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean              Remove container and images"

# Default values for variables
COUNTRY ?= germany
STATE ?= hamburg
WORKERS ?= 10
CMD ?= python --version
