# Finance Game Bundle agent guide

## Work tracking

Use the [finance_game_bundle Linear project](https://linear.app/boblsed-coder/project/finance-game-bundle-58e2acfeb4fa)
for issues and acceptance criteria. Use an issue's exact `gitBranchName` for its
branch and include its exact Linear identifier in commits and Linear updates.
Before implementation, have the architect plan the scope, design, sequence, and
validation. After implementation and required checks, have the reviewer inspect
the complete change before committing. Resolve blocking findings and get a
follow-up review after material changes. This solo workflow uses direct
integration: after review and validation, merge to `main` and push `main`; do
not create pull requests. See
`docs/agents/issue-tracker.md`.

## Repository scope

This repo contains the Compose package only. The Go API and React UI remain in
their own repositories. Keep their Git build contexts and service configuration
in `compose.yaml`; do not vendor their source trees here.

The root Compose file is the deployment entry point. Keep Postgres and the API
private on the Compose network, and expose the UI through the configured host
port. The API's database migrations run in the one-shot `migrate` service before
the API starts.
