# macp

The engine of MaestroACP: a markdown-first task system (Todo items → SQLite),
an orchestrator that dispatches tasks to headless Claude Code workers through a
staged pipeline (research → plan → implement → verify → report), a planner,
usage pacing, and the daemons that run it all (launchd on macOS, systemd --user
on Linux).

The engine holds no personal data. Everything personal — tasks, memory, notes,
decisions, run records, secrets and host identity — lives in a separate
**vault** directory.

## Vault

The engine resolves the vault root in this order:

1. `MACP_VAULT_ROOT`
2. the nearest `.macp/config.toml` above the working directory
3. fail with a clear error

`vault-template/` is an empty vault skeleton (`Todo/items`, `Todo/templates`,
`Memory`, `Notes`, `Decisions`, `Worker`). Host identity (daemon label prefix,
calendar id, repo slug) goes in the vault's `.macp/config.toml` or the matching
`MACP_*` environment variables.

## Status

The engine runs standalone against a vault: `uv run --with pytest python -m pytest -q
todo_system scripts/systemd` with `MACP_VAULT_ROOT=vault-template` passes. This
repository is published from a private vault by an export script, so history starts
fresh here. Checks are run locally (`uv run --with ruff ruff check .`,
`uv run --with pytest python -m pytest -q`); GitHub Actions is not used.

## License

MIT — see `LICENSE`.
