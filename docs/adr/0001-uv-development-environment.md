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
When uv is absent, use one shared Makefile selector to download a pinned archive
for a supported operating-system, architecture, and libc combination, verify its
SHA-256, and install it as an unmanaged repository-local copy without changing
the user's shell configuration.

## Consequences

Developers receive a reproducible `.venv`, and Makefile gate drivers use the
same locked tools. Package smoke creates its temporary venv through the selected
uv Python, then retains a separate pip wheel installation to test the built
artifact outside the editable environment. A manifest change requires a lockfile
refresh only when it makes the current resolution stale. The setup path requires
a POSIX shell, a downloader, an archiver, and network access only when uv is
unavailable or dependencies are not cached.

## Considered & rejected

- **Keep per-target pip installation.** verified: the existing Makefile has
  separate pip install commands for core and full tools and no committed lockfile.
- **Install uv globally and edit shell profiles.** judgment: a repository setup
  target should not modify a developer's shell configuration.
- **Execute the mutable installer script.** verified: the official installer is
  a network-fetched script; a pinned archive plus SHA-256 check verifies the
  executable bytes before they run.
- **Use transient `uv --with` tools.** verified: uv's project dependency groups
  are resolved into the lockfile, while transient tool additions are not project
  metadata and do not establish a durable development environment.
