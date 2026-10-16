# Deployment

The [2 October 2026 readiness review](product-readiness-review.md) and its
[workflow evidence](workflow-validation.md) are historical snapshots. They
predate the SLED-86 capability gate and SLED-88 recovery rehearsal; their
observations about release qualification or rule-pack availability should not
be read as current deployment status. This document and the committed release
evidence below describe the current bundle contract.

`compose.yaml` builds from the immutable source pair recorded in
[`release-pair.json`](../release-pair.json):

- `finance_game_go` at `58b79b94886713ce0912cfda01aa2943ec7483a1`
- `finance_game_ui` at `649ddf704ab06a530471def66316fa60b44e8887`

Docker BuildKit fetches the full Git SHA contexts during `just up`. The machine
running Compose needs Docker Compose, `just`, and SSH access to both
repositories. `just release-check` resolves Compose without starting services
and reports the source and compatibility identity.

This pair passed the disposable no-cache capability gate. Its compatibility
record names database schema 27, the test-only rule pack, and the exact
compiled-input, timeline, report, readiness, and Run artifact versions observed
in the fabricated journey. The committed receipt at
[`release-evidence/finance-game-2026-10-candidate-1.json`](../release-evidence/finance-game-2026-10-candidate-1.json)
records the pair hash, observed versions, source revisions, bundle revision,
image IDs, and completed phases.

Startup reports both source revisions, qualification state, database schema,
rule pack, and compiler contract before migration starts. API and migration
share one Go build context and carry matching source labels; the UI has its own
source label. The deployment record saves container and image IDs. A mismatch
stops startup with expected and observed revisions in the diagnostic.

Set `GO_SOURCE` and `UI_SOURCE` in `.env` to local checkout paths when preferred.
Use `just up` with these overrides: it records each checkout's actual SHA and
dirty state, labels the deployment `local-override`, and does not identify it
as the pinned release. For a local Go override, the controller reads and
validates the consecutive migration filenames and records that checkout's
latest schema version alongside the manifest compatibility contract. A dirty
local override, changed source pair, or changed schema identity requires a
backup reference before starting the migration job, even when its Git HEAD
matches the active record. The capability gate accepts only clean checkouts
whose full SHAs match the manifest.

## Startup order

Compose starts Postgres and waits for its health check. The one-shot `migrate`
service applies the API's embedded SQL migrations. The API starts after that
job succeeds, and the UI starts after the API process starts. `just up` waits
for container health checks before returning.

The API and database use only the private Compose network. The UI is published
on `127.0.0.1:3000` by default. Configure `BIND_ADDRESS`, `UI_PORT`, and
`POSTGRES_PASSWORD` in `.env` for another deployment. Use a unique database
password before making the UI reachable from another machine. A non-loopback
bind only changes the host listener; it does not add TLS or access control.
For hosted use, put the UI behind a TLS-terminating ingress, configure
authenticated access and network policy at that boundary, keep API/Postgres
private, and provide managed secrets and off-host backups. Local Compose is a
single-machine development deployment, not a hosted security configuration.

## Data and upgrades

Postgres data lives in the Compose volume `finance_game_bundle_postgres_data`.
`just down` keeps this volume. The bundle does not automatically reuse the Go
repository's Compose volume or transfer existing data.

The API checks that the database schema matches its compiled migration version.
Take a consistent custom-format `pg_dump` before upgrading and keep its checksum
and active release reference with the file. After a migration advances the
schema, rolling back an image does not reverse SQL; restoring the matching
database backup may be required.
`just up` records each deployment attempt under `.release-deployments/`. A
restart with the already recorded clean pair and schema needs no new backup.
When the pair or schema changes, or either local override is dirty, and the
project volume exists, supply a JSON
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

### Create a deployment backup

Run these read-only database commands before changing the UI/API pair. Keep the
backup outside the Compose project directory and restrict its permissions:

```sh
umask 077
backup_dir="$HOME/finance-game-backups"
mkdir -p "$backup_dir"
backup="$backup_dir/finance-game-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db pg_dump --format=custom --no-owner --no-acl \
  --username=financegame --dbname=financegame > "$backup"
sha256sum "$backup"
docker compose exec -T db psql -U financegame -d financegame -Atc \
  'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
cat .release-deployments/active.json
```

Use the checksum, schema result, release ID, pair SHA and Go/UI revisions from
`active.json` to fill in `backup-reference.json` from the example. Record the
absolute backup location and UTC creation time. Preserve the original dump and
reference together. `just up <backup-reference.json>` checks that the release
and schema metadata match the active deployment before it starts a migration;
the reference check does not read the dump, so verify the checksum separately.

Do not restore this backup over the deployment volume as a test. Use the
disposable recovery rehearsal below first. For an actual failed upgrade, keep
the old volume and failed attempt record intact and restore into a separately
named project/volume using the recorded previous release pair. The following
example assumes the previous Go/UI SHAs and release metadata are copied from
the preserved deployment references. It creates a new project and volume; do
not substitute the live Compose project name.

```sh
set -eu
export RECOVERY_PROJECT="finance-game-recovery-$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4)"
export GO_SOURCE='ssh://git@github.com/bobsled-coder/finance_game_go.git#<previous-go-sha>'
export UI_SOURCE='ssh://git@github.com/bobsled-coder/finance_game_ui.git#<previous-ui-sha>'
export GO_SOURCE_REVISION='<previous-go-sha>'
export UI_SOURCE_REVISION='<previous-ui-sha>'
export RELEASE_ID='<previous-release-id>'
export RELEASE_PAIR_SHA256='<previous-release-pair-sha256>'
export RELEASE_QUALIFICATION='<previous-qualification-status>'
export BIND_ADDRESS=127.0.0.1
export UI_PORT=3300
export POSTGRES_PASSWORD="$(openssl rand -hex 32)"
backup='/secure-backups/finance-game-previous.dump'
reference='/secure-backups/finance-game-previous-reference.json'
expected_sha="$(jq -r '.sha256' "$reference")"

actual_sha="$(sha256sum "$backup")"
test "${actual_sha%% *}" = "$expected_sha"
if docker volume inspect "${RECOVERY_PROJECT}_postgres_data" >/dev/null 2>&1; then
  echo "Recovery volume already exists; choose a new project name." >&2
  exit 1
fi
docker compose -p "$RECOVERY_PROJECT" up --detach --wait db
test -z "$(docker compose -p "$RECOVERY_PROJECT" exec -T db psql \
  -U financegame -d financegame -Atc \
  "SELECT to_regclass('public.schema_migrations')")"
docker compose -p "$RECOVERY_PROJECT" exec -T db pg_restore \
  --exit-on-error --single-transaction --no-owner --no-acl \
  --username=financegame --dbname=financegame < "$backup"
docker compose -p "$RECOVERY_PROJECT" run --rm migrate
docker compose -p "$RECOVERY_PROJECT" up -d --build
docker compose -p "$RECOVERY_PROJECT" exec -T db psql \
  -U financegame -d financegame -Atc \
  'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
docker compose -p "$RECOVERY_PROJECT" exec -T db postgres --version
docker compose -p "$RECOVERY_PROJECT" ps
```

Before opening the recovery UI at `http://127.0.0.1:3300`, inspect the
recovery project's container labels and image IDs and compare the release ID,
pair SHA, Go/UI SHAs, and database image with the preserved previous-release
reference. These raw Compose commands do not write the controller's deployment
record. Capture the values with:

```sh
for service in db migrate api ui; do
  container="$(docker compose -p "$RECOVERY_PROJECT" ps -a -q "$service")"
  test -n "$container"
  docker inspect "$container" --format '{{.Image}} {{json .Config.Labels}}'
done
```

Authenticate to the recovered API, then compare the saved finalized Snapshot
and Run hashes directly. The backup reference carries these IDs and hashes:

```sh
export API_TOKEN='<bearer-token-for-recovered-api>'
household_id="$(jq -r '.artifacts.householdId' "$reference")"
snapshot_id="$(jq -r '.artifacts.snapshotId' "$reference")"
run_id="$(jq -r '.artifacts.runId' "$reference")"
snapshot_hash="$(jq -r '.artifacts.snapshotHash' "$reference")"
run_hash="$(jq -r '.artifacts.runHash' "$reference")"
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:3300/api/v1/households/$household_id/snapshots/$snapshot_id" \
  | jq -e --arg expected "$snapshot_hash" '.finalized.contentHash == $expected'
curl -fsS -H "Authorization: Bearer $API_TOKEN" \
  "http://127.0.0.1:3300/api/v1/households/$household_id/scenarios/$(jq -r '.artifacts.scenarioId' "$reference")/runs/$run_id" \
  | jq -e --arg expected "$run_hash" '.resultHash == $expected'
```

Record the recovery project name, image IDs, schema result, and comparison in
the incident record. Keep the original volume and backup until the recovery has
been reviewed; plan cutover separately. An image rollback alone is not
database recovery.

### Rehearse backup and restore

The explicit recovery command creates a fabricated household in a fresh
project, stops its API for a consistent `pg_dump`, and records the dump's
SHA-256, release ID, source SHAs, API/migration/UI image IDs, Postgres image and
server version, schema migration set, and Snapshot/compilation/Run IDs and
hashes. It adds a future schema marker to the source project's disposable
database and confirms that the matching migration image refuses that schema.
It then starts a second generated Compose project with a new volume and the
captured image IDs, restores the archive into an empty database, runs the
matching migration, and checks the finalized Snapshot, compilation, and Run
hashes through the restored API. The incompatible source project is kept until
the evidence is collected.

Run the normal qualification rehearsal with clean local checkouts matching the
two SHAs in `release-pair.json`:

```sh
just recovery-rehearsal /work/finance_game_go /work/finance_game_ui
```

Before committing changes to the bundle gate or recovery controller, use the
precommit form against the same clean Go/UI pair:

```sh
just recovery-rehearsal-precommit /work/finance_game_go /work/finance_game_ui
```

Precommit validation records the bundle `HEAD` and a SHA-256 fingerprint of
tracked changes and untracked candidate files, then confirms that fingerprint
is unchanged at the end. Its manifest is labeled `precommit` and it never emits
a runtime qualification receipt. Run the normal clean-checkout rehearsal after
the reviewed change is integrated to create qualification evidence.

Evidence is written under
`outputs/capability-gates/<generated-project>/`, including
`fabricated-database.dump`, `backup-reference.json`, the phase manifest,
failure logs, and cleanup logs. The runner rejects non-generated project names,
external or non-project-scoped volumes/networks, unexpected bind mounts,
published API/database ports, and existing generated resource names. Database
backup/restore commands run only through the two generated projects. Cleanup
checks each container, volume, and network project label before removing it;
it never targets `finance_game_bundle_postgres_data`. `--keep` is available on
the Python runner for diagnosis and retains both projects and fixture files.

This rehearsal covers a rejected future-schema marker and logical restore of
fabricated data into a fresh volume. It does not claim to recover every
partially applied migration, change PostgreSQL major versions, restore external
identity-provider state, or perform production cutover. Run the rehearsal on a
Docker-capable host before treating recovery as verified.

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
It checks SPA and deep-link fallback, selects the test-only
`canadian-planning-rules-test-v1` pack explicitly, and verifies a made-up missing pack
returns `rule_pack_not_found`. The shipped API does not load separately named
fabricated statutory packs. The gate's `canadian-planning-rules-test-v1` is an
explicit test-only compatibility target used with fabricated household data;
it does not represent a production rule pack or fiscal facts as law.

The runner starts a temporary HTTPS OIDC/JWKS fixture with a generated signing
key, configured only in the API service override. It also rehearses a migration
connection failure, a future schema-version incompatibility marker, recovery
from that marker, and API-offline recovery. The schema rehearsal inserts and
deletes only its unique sentinel row in the disposable database. These checks
exercise service failure handling. The separate `just recovery-rehearsal`
command additionally creates and restores a logical backup using a second
project-scoped volume.
