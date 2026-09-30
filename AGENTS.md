# Finance Game Bundle agent guide

## Work tracking

Use the [finance_game_bundle Linear project](https://linear.app/boblsed-coder/project/finance-game-bundle-58e2acfeb4fa)
for issues and acceptance criteria. Use an issue's exact `gitBranchName` for its
branch and include its `BOB-` identifier in the PR title. See
`docs/agents/issue-tracker.md`.

## Repository scope

This repo contains the Compose package only. The Go API and React UI remain in
their own repositories. Keep their Git build contexts and service configuration
in `compose.yaml`; do not vendor their source trees here.

The root Compose file is the deployment entry point. Keep Postgres and the API
private on the Compose network, and expose the UI through the configured host
port. The API's database migrations run in the one-shot `migrate` service before
the API starts.
