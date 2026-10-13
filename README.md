# Finance Game Bundle

This repository is a small Docker Compose package that builds the Finance Game
React UI and Go API from their GitHub repositories, then runs them with Postgres.
The source trees remain in their own repositories. Only the UI port is published
to the host.

## Launch

Install Docker with the Compose plugin and `just`, make sure your SSH agent can
read both source repositories, then run from this directory:

```sh
just up
```

Open <http://127.0.0.1:3000>. Compose builds the immutable UI/API source pair in
[`release-pair.json`](release-pair.json), starts Postgres, applies the Go SQL
migrations, then starts the API and UI. The pair is currently a candidate; it
is not runtime-qualified until the disposable capability gate passes. Run
`just release-check` to print and validate the pinned source and compatibility
identity before startup. The bundle does not copy either source repository.

To use local sibling checkouts or change the port, bind address, or database
password, copy `.env.example` to `.env` and edit it before launch. `just up`
records actual local checkout revisions as a development override. The pinned
defaults use authenticated SSH Git contexts.

```sh
just logs
just down
```

`just down` retains the named Postgres volume. The database is private to the
Compose network, and the UI binds to local loopback by default. Review
[`docs/deployment.md`](docs/deployment.md) before exposing the app or upgrading
the API schema. Before changing the release pair, take a consistent database
backup and run the disposable recovery rehearsal with `just recovery-rehearsal`
to verify restore compatibility and immutable artifact hashes.

Work for this bundle is tracked in the
[finance_game_bundle Linear project](https://linear.app/boblsed-coder/project/finance-game-bundle-58e2acfeb4fa).
The GitHub repository is `git@github.com:bobsled-coder/finance_game_bundle.git`.
