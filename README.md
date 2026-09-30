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

Open <http://127.0.0.1:3000>. Compose builds the `main` branch of both source
repositories, starts Postgres, applies the Go SQL migrations, then starts the
API and UI. It waits until both the UI and API are healthy. It does not copy
either source repository into this package.

To use local sibling checkouts or change the port, bind address, or database
password, copy `.env.example` to `.env` and edit it before launch. The Compose
file defaults to authenticated SSH Git contexts for both source repositories.

```sh
just logs
just down
```

`just down` retains the named Postgres volume. The database is private to the
Compose network, and the UI binds to local loopback by default. Review
[`docs/deployment.md`](docs/deployment.md) before exposing the app or upgrading
the API schema.

Work for this bundle is tracked in the
[finance_game_bundle Linear project](https://linear.app/boblsed-coder/project/finance-game-bundle-58e2acfeb4fa).
The GitHub repository is `git@github.com:bobsled-coder/finance_game_bundle.git`.
