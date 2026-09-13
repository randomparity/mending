# UV Development Environment Design

## Charter

- **Interaction:** interactive.
- **Scope identity:** reproducible local development environment for this repository.
- **Outcome:** `make setup` provisions uv when absent and creates a full development environment.
- **Completion criteria:** the locked environment contains the `full` project extra and all existing Makefile development tools; Makefile gates execute in that environment; a fresh checkout can reproduce it.
- **Provenance:** user request to establish a uv environment and `setup` target; user selected the full developer environment and approved this design.
- **Exclusions:** no scanner behavior, persisted-state model, language support, or unrelated cleanup changes.
- **Surface:** `pyproject.toml`, `uv.lock`, `Makefile`, setup-target test, and developer documentation.
- **Ambiguities:** the target is POSIX-shell based, matching the existing Makefile and CI. It uses a repository-local unmanaged uv installation only when `uv` is absent from `PATH`.

## Decision

Declare the Makefile's existing developer tools in a PEP 735 `dev` dependency
group. Keep scanner capabilities in the existing `full` extra. Commit the
universal `uv.lock` generated for the complete resolution.

`make setup` first uses `uv` from `PATH`. If unavailable, it downloads a pinned
uv 0.12.13 archive for Linux or macOS on x86_64 or arm64, verifies its embedded
SHA-256 value, extracts it to `.tools/uv`, and invokes that exact binary. It
fails before extraction for an unsupported platform, unavailable downloader or
archiver, download error, or checksum mismatch. It does not alter shell profiles
or the global PATH. It then runs `uv sync --locked --extra full`, which creates
an exact editable `.venv` using the locked full and default `dev` dependencies.

All Makefile gate drivers depend on `setup` and execute their project tools with
`uv run --locked`. `package-smoke` is the deliberate exception after its locked
build and twine driver steps: it installs the built wheel into a new temporary
venv with pip so the wheel is tested independently of the editable development
environment. `install-ci-tools` and `install-full-tools` remain compatibility
aliases for `setup` rather than retaining a second pip-based installation path.

## Failure handling

The setup shell exits on failed platform detection, downloads, checksum
verification, extraction, locking, or syncing. It cannot fall through to a
system Python invocation. A stale or missing lockfile fails under `--locked`,
requiring an intentional `uv lock` update in the same change as a dependency
manifest edit.

## Verification

Hermetic setup tests supply an executable fake `uv` for the PATH branch, then a
fake downloader that returns a checksummed archive for the absent-uv branch;
both assert `sync --locked --extra full`. A failing downloader proves the target
exits without invoking a system Python. A package-smoke assertion proves its
build driver uses `uv run --locked` while its temporary wheel installation stays
isolated. The locked environment is then used to run the focused test, `uv lock
--check`, and the existing core checks. The optional full suite is exercised
under Python 3.11 when the resolved packages support the host.
