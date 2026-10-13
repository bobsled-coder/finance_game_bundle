# Deployment

`compose.yaml` builds from the immutable source pair recorded in
[`release-pair.json`](../release-pair.json):

- `finance_game_go` at `aa83b1a2b5e3a05626653e81c6dc737a9cdbae07`
- `finance_game_ui` at `f708b951cb429525c2f80a4db73042ae71a9e53f`

Docker BuildKit fetches the full Git SHA contexts during `just up`. The machine
running Compose needs Docker Compose, `just`, and SSH access to both
repositories. `just release-check` resolves Compose without starting services
and reports the source and compatibility identity.

This pair is a **candidate**, not a runtime-qualified release. The disposable
capability gate has not completed successfully for this exact pair. Its
compatibility record names database schema 26, the selected rule pack, and the
exact compiled-input, timeline, report, readiness, and Run artifact versions
expected from the fabricated journey. A successful gate writes a
`qualification.json` receipt containing the pair hash, observed versions,
source revisions, bundle revision, image IDs, and completed phases. Only a
receipt that binds this exact pair may be referenced when the manifest's
`qualification.status` is changed to `qualified`.

Startup reports both source revisions, qualification state, database schema,
rule pack, and compiler contract before migration starts. API and migration
share one Go build context and carry matching source labels; the UI has its own
source label. The deployment record saves container and image IDs. A mismatch
stops startup with expected and observed revisions in the diagnostic.

Set `GO_SOURCE` and `UI_SOURCE` in `.env` to local checkout paths when preferred.
Use `just up` with these overrides: it records each checkout's actual SHA and
dirty state, labels the deployment `local-override`, and does not identify it
as the pinned release. The capability gate accepts only clean checkouts whose
full SHAs match the manifest.

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
`just up` records each deployment attempt under `.release-deployments/`. A
restart with the already recorded pair needs no new backup. When the pair or
compatibility contract changes and the project volume exists, supply a JSON
backup reference with `just up path/to/backup-reference.json`; it must name the
prior release, database schema, backup location and creation time, and a
SHA-256 checksum. If this is a pre-controller volume without an active
deployment record, also pass a previous-release reference as the second
argument. The prior deployment and backup references are written before Compose
can start the migration job. First installs are marked explicitly and require
no prior release or backup reference. This records upgrade context; it does not
perform a restore rehearsal, which remains a separate recovery gate.
See [`backup-reference.example.json`](backup-reference.example.json) for the
input shape. The checksum is syntax-checked; the controller does not claim to
read or verify the backup bytes.
See [`previous-release-reference.example.json`](previous-release-reference.example.json)
for the one-time metadata needed to adopt an existing volume.

## Disposable capability gate

`just capability-gate <go-source> <ui-source>` builds without Docker cache and
exercises one isolated Compose project against the exact Git checkouts in
`release-pair.json`. Both checkouts must be clean and match their full recorded
SHA; the gate records their commit IDs and rejects mismatched or dirty trees.
For example:

```sh
just capability-gate /work/finance_game_go /work/finance_game_ui
```

The gate chooses a unique Compose project and loopback UI port, supplies an
empty Compose env file, and validates the resolved config before starting
containers. Postgres and the API stay private. Its database volume is
project-scoped. Cleanup checks Compose project labels and runs `down --volumes`
only for this generated project. `--keep` on the underlying Python command
leaves the stack running and retains the temporary OIDC fixture files for
inspection; normal runs remove their project and fixture files.
Evidence is written under `outputs/capability-gates/<project>/` with source
revisions, artifact IDs and hashes, migration failure output, and bounded
Compose logs. It never connects to or removes a pre-existing project or
volume.

The fixture creates a fabricated Person, Household, Chequing Account, exact
`100000.10` CAD balance, finalized 2026 Snapshot, Scenario, compilation, Run,
and unchanged comparison through the UI's published port and `/api/v1` proxy.
It checks SPA and deep-link fallback, selects
`canadian-planning-rules-v1` explicitly, and verifies a made-up missing pack
returns `rule_pack_not_found`. The shipped API does not load separately named
fabricated statutory packs. The test therefore uses fabricated household data
with the reviewed 2026 pack and never presents fabricated fiscal facts as law.

The runner starts a temporary HTTPS OIDC/JWKS fixture with a generated signing
key, configured only in the API service override. It also rehearses a migration
connection failure, a future schema-version incompatibility marker, recovery
from that marker, and API-offline recovery. The schema rehearsal inserts and
deletes only its unique sentinel row in the disposable database. These checks
exercise service failure handling; they are not a production backup/restore or
SQL rollback test.
