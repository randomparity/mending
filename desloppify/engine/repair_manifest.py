"""Bounded source evidence manifest read from one git revision of the target checkout.

A manifest's digest proves which source input was read; it never proves that a
concern is valid. Failed or unbindable analysis is an explicit ``AnalysisUnknown``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv is passed to the installed git CLI.
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "desloppify-source-manifest:v1"
ROLES = frozenset({"implementation", "sibling", "test", "decision"})
STATUSES = frozenset({"present", "missing", "unsupported"})
MAX_DEPENDENCIES = 64
MAX_PATH_BYTES = 1024
_GIT_TIMEOUT_SECONDS = 30
_REGULAR_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class DependencySpec:
    """A source file the caller says the concern depends on."""

    path: str
    role: str


@dataclass(frozen=True)
class Dependency:
    """One dependency as read at the manifest revision."""

    path: str
    role: str
    status: str
    object_id: str | None


@dataclass(frozen=True)
class AnalysisUnknown:
    """Analysis that could not be bound to source; never current."""

    reason: str


@dataclass(frozen=True)
class SourceManifest:
    """The commit and dependency blobs one analysis read."""

    revision: str
    dependencies: tuple[Dependency, ...]
    coverage: str

    def as_record(self) -> dict[str, Any]:
        """Return the canonical persisted form."""
        return {
            "schema": MANIFEST_SCHEMA,
            "revision": self.revision,
            "coverage": self.coverage,
            "dependencies": [
                {"path": d.path, "role": d.role, "status": d.status, "object_id": d.object_id}
                for d in self.dependencies
            ],
        }

    @property
    def digest(self) -> str:
        """SHA-256 of the canonical record: which input was read."""
        canonical = json.dumps(self.as_record(), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ManifestComparison:
    """Whether evidence recorded earlier is still current, and why."""

    current: bool
    reason: str
    changed_paths: tuple[str, ...]
    base_changed: bool
    previous_digest: str | None
    current_digest: str | None


def build_manifest(
    root: Path,
    revision: str,
    dependencies: Sequence[DependencySpec],
    *,
    coverage_declared_complete: bool,
) -> SourceManifest | AnalysisUnknown:
    """Read ``dependencies`` from ``revision`` of the repository at ``root``."""
    problem = _spec_problem(dependencies)
    if problem:
        return AnalysisUnknown(problem)
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or Path(top.strip()).resolve() != Path(root).resolve():
        return AnalysisUnknown("root is not a repository top level")
    resolved = _git(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    if resolved is None:
        return AnalysisUnknown("revision could not be resolved")
    commit = resolved.strip()
    recorded: list[Dependency] = []
    for spec in dependencies:
        dependency = _read_dependency(root, commit, spec)
        if dependency is None:
            return AnalysisUnknown("git could not read the revision tree")
        recorded.append(dependency)
    ordered = tuple(sorted(recorded, key=lambda d: d.path))
    return SourceManifest(commit, ordered, _coverage(ordered, coverage_declared_complete))


def manifest_from_record(record: object) -> SourceManifest | AnalysisUnknown:
    """Parse a stored manifest record; anything malformed is unknown."""
    try:
        return _parse_record(record)
    except (TypeError, ValueError, KeyError):
        return AnalysisUnknown("malformed manifest record")


def compare_manifests(
    previous: SourceManifest | AnalysisUnknown, current: SourceManifest | AnalysisUnknown
) -> ManifestComparison:
    """Report whether ``previous`` evidence is still current against ``current``."""
    old_digest = previous.digest if isinstance(previous, SourceManifest) else None
    new_digest = current.digest if isinstance(current, SourceManifest) else None
    if not isinstance(previous, SourceManifest) or not isinstance(current, SourceManifest):
        return ManifestComparison(False, "unknown", (), False, old_digest, new_digest)
    base_changed = previous.revision != current.revision
    changed = _changed_paths(previous, current)
    if "partial" in (previous.coverage, current.coverage):
        reason = "coverage-incomplete"
    elif changed:
        reason = "dependencies-changed"
    else:
        reason = "unchanged"
    current_now = reason == "unchanged"
    return ManifestComparison(current_now, reason, changed, base_changed, old_digest, new_digest)


def concern_dependencies(issue: Mapping[str, Any]) -> tuple[DependencySpec, ...] | None:
    """The concern file as implementation and its related files as siblings.

    ``None`` means ``related_files`` is not a list of strings.
    """
    detail = issue.get("detail")
    related = detail.get("related_files", []) if isinstance(detail, Mapping) else []
    if not isinstance(related, list) or not all(isinstance(path, str) for path in related):
        return None
    concern_file = issue.get("file")
    specs: list[DependencySpec] = []
    if isinstance(concern_file, str) and concern_file not in {"", "."}:
        specs.append(DependencySpec(concern_file, "implementation"))
    for path in related:
        if path not in {spec.path for spec in specs}:
            specs.append(DependencySpec(path, "sibling"))
    return tuple(specs)


@dataclass(frozen=True)
class SourceCheckout:
    """The operator's checkout and the revision concerns are checked against."""

    root: Path
    revision: str

    def manifest_for(self, issue: Mapping[str, Any]) -> SourceManifest | AnalysisUnknown:
        """Read the concern's dependencies, declared complete as the reviewed input set."""
        dependencies = concern_dependencies(issue)
        if dependencies is None:
            return AnalysisUnknown("malformed related_files")
        return build_manifest(
            self.root, self.revision, dependencies, coverage_declared_complete=True
        )


def _spec_problem(dependencies: Sequence[DependencySpec]) -> str | None:
    if len(dependencies) > MAX_DEPENDENCIES:
        return f"more than {MAX_DEPENDENCIES} dependencies"
    if any(not (isinstance(s.path, str) and isinstance(s.role, str)) for s in dependencies):
        return "invalid dependency spec"
    if any(_oversize(spec.path) for spec in dependencies):
        return f"dependency path longer than {MAX_PATH_BYTES} bytes"
    if any(spec.role not in ROLES for spec in dependencies):
        return "unknown dependency role"
    if len({spec.path for spec in dependencies}) != len(dependencies):
        return "duplicate dependency path"
    return None


def _oversize(path: str) -> bool:
    return len(path.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\0" in path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(segment not in {"", ".", ".."} for segment in path.split("/"))


def _read_dependency(root: Path, commit: str, spec: DependencySpec) -> Dependency | None:
    if not _valid_path(spec.path):
        return Dependency(spec.path, spec.role, "unsupported", None)
    listing = _git(root, "ls-tree", "-z", "--full-tree", commit, "--", spec.path)
    if listing is None:
        return None
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        if path != spec.path:
            continue
        fields = meta.split(" ")
        if len(fields) != 3:
            return None
        mode, kind, object_id = fields
        if kind == "blob" and mode in _REGULAR_MODES:
            return Dependency(spec.path, spec.role, "present", object_id)
        return Dependency(spec.path, spec.role, "unsupported", None)
    return Dependency(spec.path, spec.role, "missing", None)


def _coverage(dependencies: Sequence[Dependency], declared_complete: bool) -> str:
    complete = (
        declared_complete
        and any(d.role == "implementation" for d in dependencies)
        and all(d.status == "present" for d in dependencies)
    )
    return "complete" if complete else "partial"


def _changed_paths(previous: SourceManifest, current: SourceManifest) -> tuple[str, ...]:
    before = {d.path: (d.role, d.status, d.object_id) for d in previous.dependencies}
    after = {d.path: (d.role, d.status, d.object_id) for d in current.dependencies}
    return tuple(sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p)))


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        process = subprocess.run(  # nosec B603 - fixed argv, no shell.
            [git, "--literal-pathspecs", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None  # ValueError: NUL in an argument, or undecodable output.
    return process.stdout if process.returncode == 0 else None


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )


def _parse_record(record: object) -> SourceManifest:
    if not isinstance(record, Mapping) or record["schema"] != MANIFEST_SCHEMA:
        raise ValueError("not a manifest record")
    entries = record["dependencies"]
    if not isinstance(entries, list) or len(entries) > MAX_DEPENDENCIES:
        raise ValueError("invalid dependency list")
    dependencies = tuple(_parse_dependency(entry) for entry in entries)
    paths = [d.path for d in dependencies]
    if paths != sorted(set(paths)) or not _is_object_id(record["revision"]):
        raise ValueError("invalid ordering or revision")
    coverage = record["coverage"]
    if coverage not in {"complete", "partial"}:
        raise ValueError("invalid coverage")
    if coverage == "complete" and _coverage(dependencies, True) != "complete":
        raise ValueError("coverage contradicts dependencies")
    return SourceManifest(record["revision"], dependencies, coverage)


def _parse_dependency(entry: object) -> Dependency:
    if not isinstance(entry, Mapping):
        raise ValueError("invalid dependency")
    path, role, status, object_id = (entry[k] for k in ("path", "role", "status", "object_id"))
    if not isinstance(path, str) or role not in ROLES or status not in STATUSES:
        raise ValueError("invalid dependency field")
    if _oversize(path):
        raise ValueError("dependency path is too long")
    if not (_is_object_id(object_id) if status == "present" else object_id is None):
        raise ValueError("object id does not match status")
    if status != "unsupported" and not _valid_path(path):
        raise ValueError("only an unsupported dependency may carry an invalid path")
    return Dependency(path, role, status, object_id)


__all__ = [
    "AnalysisUnknown",
    "Dependency",
    "DependencySpec",
    "MANIFEST_SCHEMA",
    "MAX_DEPENDENCIES",
    "MAX_PATH_BYTES",
    "ManifestComparison",
    "ROLES",
    "SourceCheckout",
    "SourceManifest",
    "build_manifest",
    "compare_manifests",
    "concern_dependencies",
    "manifest_from_record",
]
