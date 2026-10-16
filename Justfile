set dotenv-load

default: help

help:
    @just --list

# Build and launch the UI, API, database, and migration job.
up backup_reference="" previous_release_reference="":
    python3 scripts/release.py up --backup-reference "{{backup_reference}}" --previous-release-reference "{{previous_release_reference}}"

# Resolve and validate the immutable UI/API source pair without starting services.
release-check:
    python3 scripts/release.py check

# Stop the stack while retaining its Postgres volume.
down:
    docker compose down

# Show service state.
ps:
    docker compose ps

# Follow logs for all services, or for one named service.
logs service="":
    docker compose logs --follow {{service}}

# Check the resolved Compose configuration.
config:
    docker compose config --quiet

# Exercise a disposable UI/API/Postgres stack and fabricated 2026 household.
# Requires explicit, clean local Go and UI source checkouts.
capability-gate go_source ui_source:
    python3 scripts/capability_gate.py --go-source "{{go_source}}" --ui-source "{{ui_source}}" --no-cache

test-capability-gate:
    python3 -m unittest discover -s tests -v
