# Deployment

`compose.yaml` builds from the `main` branches of:

- `git@github.com:bobsled-coder/finance_game_ui.git`
- `git@github.com:bobsled-coder/finance_game_go.git`

Docker BuildKit fetches these Git contexts during `just up`. The machine running
Compose needs Docker Compose, `just`, and SSH access to both repositories. Set
`GO_SOURCE` and `UI_SOURCE` in `.env` to local checkout paths when preferred.

## Startup order

Compose starts Postgres and waits for its health check. The one-shot `migrate`
service applies the API's embedded SQL migrations. The API starts after that
job succeeds, and the UI starts after the API process starts. `just up` waits
for container health checks before returning.

The API and database use only the private Compose network. The UI is published
on `127.0.0.1:3000` by default. Configure `BIND_ADDRESS`, `UI_PORT`, and
`POSTGRES_PASSWORD` in `.env` for another deployment. Use a unique database
password before making the UI reachable from another machine.

## Data and upgrades

Postgres data lives in the Compose volume `finance_game_bundle_postgres_data`.
`just down` keeps this volume. The bundle does not automatically reuse the Go
repository's Compose volume or transfer existing data.

The API checks that the database schema matches its compiled migration version.
Back up the database before upgrading. After a migration advances the schema, a
rollback to an older API may require restoring the matching database backup.
