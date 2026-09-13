# 0001: Use uv for the development environment

## Status

Accepted

## Context

The Makefile installs development tools directly with pip every time a gate is
run. The repository has optional scanner dependencies but no lockfile, so fresh
developer environments and CI jobs can resolve different package versions.

## Decision

Use uv as the project environment manager. Declare development-only tools in a
PEP 735 `dev` dependency group, commit `uv.lock`, and provide a `make setup`
target that syncs the `full` extra and default `dev` group from that lockfile.
When uv is absent, install an unmanaged repository-local copy without changing
the user's shell configuration.

## Consequences

Developers receive a reproducible `.venv`, and Makefile gates use the same
locked tools. Updating dependency metadata requires an intentional lockfile
refresh. The setup path requires a POSIX shell, a downloader, and network access
only when uv is unavailable or dependencies are not cached.

## Considered & rejected

- **Keep per-target pip installation.** verified: the existing Makefile has
  separate pip install commands for core and full tools and no committed lockfile.
- **Install uv globally and edit shell profiles.** judgment: a repository setup
  target should not modify a developer's shell configuration.
- **Use transient `uv --with` tools.** verified: uv's project dependency groups
  are resolved into the lockfile, while transient tool additions are not project
  metadata and do not establish a durable development environment.
