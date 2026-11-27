# PEP 639 license metadata (#86)

## Problem

The legacy `project.license` table causes a setuptools deprecation warning.
Distribution consumers need the OSNL 0.2 identifier and original license text.

## Scope and authority

Issue [#86](https://github.com/randomparity/mending/issues/86) and the operator's
"Approve license metadata draft" authorize the precise metadata replacement and
`setuptools>=77.0.3` floor. The approved exclusions remain: license terms belong
to the license owner; runtime dependencies and publication belong to release maintainers.

## Design

Keep setuptools as the metadata owner. In `pyproject.toml`, replace the legacy
table with `license = "LicenseRef-OSNL-0.2"`, add `license-files = ["LICENSE"]`
under `[project]`, and require `setuptools>=77.0.3` under `[build-system]`.
The existing build frontend and package-smoke recipe consume this configuration.
No callers migrate and no compatibility path or ADR is required: the precise
solution and backend floor were approved. Leave `LICENSE` byte-for-byte unchanged.

## Success

The wheel and sdist each declare `License-Expression: LicenseRef-OSNL-0.2`
and `License-File: LICENSE`, and each contains the original license bytes.
Builds with setuptools 77.0.3 and the normal isolated production build succeed
without the license-table warning. `make package-smoke` passes.

## Failure model

- Actors and deployments: local builders and CI producing wheel and sdist;
  downstream installers consume those artifacts.
- Invariants and assets at stake: public distribution metadata and original
  license bytes; compatibility with the minimum supported backend.
- Accepted failure classes: offline dependency resolution can fail visibly;
  builds using backends below the declared floor are unsupported.
- Covered elsewhere: license interpretation and terms — license owner;
  runtime dependency changes and release publication — release maintainers.

## Threat model

- Boundary inventory: existing isolated build dependency resolution; no added
  entry points or permission changes. The backend lower bound changes.
- Actor model: trusted maintainers and CI build checked-out source; the existing
  package index supplies setuptools to the isolated build environment.
- Control per boundary: the frontend enforces the declared backend requirement;
  existing lock-based development tooling and package-smoke validate packaging.
- Out of scope: package-index compromise and publishing credentials remain with
  release maintainers; no release is published by this change.

## Validation

Use an ephemeral stdlib archive inspector, not a new permanent testing surface:
check parsed wheel METADATA and sdist PKG-INFO, license member bytes, and build
logs. Run it red on the original artifacts and green on the replacements.
Build both formats at setuptools 77.0.3 and through the normal isolated build;
then run package-smoke and the repository guardrails. No runtime behavior changes.
