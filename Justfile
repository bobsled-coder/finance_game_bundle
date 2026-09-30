set dotenv-load

default: help

help:
    @just --list

# Build and launch the UI, API, database, and migration job.
up:
    docker compose up --build --detach --wait

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
