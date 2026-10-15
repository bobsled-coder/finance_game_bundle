# Deployment

`compose.yaml` builds from the `main` branches of:

- `git@github.com:bobsled-coder/finance_game_ui.git`
- `git@github.com:bobsled-coder/finance_game_go.git`

Docker BuildKit fetches these Git contexts during `just up`. The machine running
Compose needs Docker, Docker Compose, Python 3.10+, OpenSSL, and `just`, plus
SSH access to both repositories when building from Git contexts. Set
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

## Disposable capability gate

`just capability-gate <go-source> <ui-source>` builds and exercises one isolated
Compose project against explicit local Git checkouts. Both checkouts must be
clean; the gate records their commit IDs and rejects dirty trees. For example:

```sh
just capability-gate /work/finance_game_go /work/finance_game_ui
```

The gate chooses a unique Compose project and a random high loopback UI port,
supplies an empty Compose env file, and validates the resolved config before
starting containers. Compose reports a bind error if another process already
owns the selected port. Postgres and the API stay private. Its database volume is
project-scoped. Cleanup checks Compose project labels and runs `down --volumes`
only for this generated project. The Go migration/API and UI images receive
unique project-scoped tags and an ownership label; cleanup verifies that label
before removing those tags. Shared base images are left in place. `--keep` on
the underlying Python command leaves the stack running and retains the
temporary OIDC fixture files for inspection; normal runs remove their project
and fixture files.
Evidence is written under `outputs/capability-gates/<project>/` with source
revisions, artifact IDs and hashes, migration failure output, and bounded
Compose logs. It never connects to or removes a pre-existing project or
volume.

The gate builds the Go source's fixed `capability-api` target, which carries a
separately versioned fabricated rules fixture. It does not select or override
the production API's rule catalog. The fixture creates a fabricated Person, Household, Chequing Account, exact
`100000.10` CAD balance, finalized 2026 Snapshot, Scenario, compilation, Run,
and unchanged comparison through the UI's published port and `/api/v1` proxy.
It checks SPA and deep-link fallback, selects
`canadian-planning-rules-test-v1` explicitly, and verifies a made-up missing
pack returns `rule_pack_not_found`. Its rule values and provenance are marked
as test fixtures and never presented as law.

The runner starts a temporary HTTPS OIDC/JWKS fixture with a generated signing
key, configured only in the API service override. It also rehearses a migration
connection failure, a future schema-version incompatibility marker, recovery
from that marker, and API-offline recovery. The schema rehearsal inserts and
deletes only its unique sentinel row in the disposable database. These checks
exercise service failure handling; they are not a production backup/restore or
SQL rollback test.
