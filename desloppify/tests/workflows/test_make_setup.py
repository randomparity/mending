"""Integration tests for the reproducible uv Makefile setup target."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = ROOT / "Makefile"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_uv(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
if [ "$1" = "--version" ]; then
    printf 'uv %s\\n' "$FAKE_UV_VERSION"
fi
""",
    )


def _make_env(tmp_path: Path, *, version: str = "0.12.13") -> dict[str, str]:
    return {
        **os.environ,
        "UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_UV_VERSION": version,
    }


def _run_make(
    target: str,
    tmp_path: Path,
    env: dict[str, str],
    *variables: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-f", str(MAKEFILE), target, *variables],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("version", ["0.12.1", "0.12.10", "0.12.13", "0.13.0"])
def test_setup_accepts_compatible_path_uv(tmp_path: Path, version: str) -> None:
    uv = tmp_path / "uv"
    _fake_uv(uv)

    result = _run_make("setup", tmp_path, _make_env(tmp_path, version=version), f"UV={uv}")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines() == [
        "--version",
        "sync --locked --extra full",
    ]


def test_setup_rejects_incompatible_path_uv_before_sync(tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    _fake_uv(uv)

    result = _run_make("setup", tmp_path, _make_env(tmp_path, version="0.12.0"), f"UV={uv}")

    assert result.returncode != 0
    assert "requires uv >= 0.12.1" in result.stderr
    assert (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines() == ["--version"]


def _write_uv_archive(path: Path) -> str:
    uv_source = path.parent / "uv"
    _fake_uv(uv_source)
    with tarfile.open(path, "w:gz") as archive:
        archive.add(uv_source, arcname="uv-test/uv")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_downloader(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
for argument in "$@"; do
    output="$argument"
done
cp "$UV_ARCHIVE_SOURCE" "$output"
""",
    )


@pytest.mark.parametrize(
    "target",
    [
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ],
)
def test_setup_bootstraps_supported_local_uv_and_reuses_it_for_gates(
    tmp_path: Path, target: str
) -> None:
    archive = tmp_path / "uv.tar.gz"
    checksum = _write_uv_archive(archive)
    downloader = tmp_path / "curl"
    _fake_downloader(downloader)
    local_uv = tmp_path / "tools" / "uv"
    env = _make_env(tmp_path)
    env["UV_ARCHIVE_SOURCE"] = str(archive)

    setup = _run_make(
        "setup",
        tmp_path,
        env,
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        f"UV_TARGET={target}",
        f"UV_SHA256={checksum}",
        f"UV_CURL={downloader}",
    )
    lint = _run_make(
        "lint",
        tmp_path,
        env,
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        f"UV_TARGET={target}",
        f"UV_SHA256={checksum}",
        f"UV_CURL={downloader}",
    )

    assert setup.returncode == 0, setup.stderr
    assert lint.returncode == 0, lint.stderr
    assert (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines() == [
        "--version",
        "sync --locked --extra full",
        "--version",
        "sync --locked --extra full",
        "run --locked ruff check . --select E9,F63,F7,F82",
    ]


def test_setup_rejects_an_unsupported_bootstrap_platform(tmp_path: Path) -> None:
    local_uv = tmp_path / "tools" / "uv"

    result = _run_make(
        "setup",
        tmp_path,
        _make_env(tmp_path),
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        "UV_TARGET=unsupported-platform",
    )

    assert result.returncode != 0
    assert "unsupported uv platform: unsupported-platform" in result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_setup_rejects_an_archive_with_an_unexpected_checksum(tmp_path: Path) -> None:
    archive = tmp_path / "uv.tar.gz"
    _write_uv_archive(archive)
    downloader = tmp_path / "curl"
    _fake_downloader(downloader)
    local_uv = tmp_path / "tools" / "uv"
    env = _make_env(tmp_path)
    env["UV_ARCHIVE_SOURCE"] = str(archive)

    result = _run_make(
        "setup",
        tmp_path,
        env,
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        "UV_TARGET=x86_64-unknown-linux-gnu",
        "UV_SHA256=unexpected-checksum",
        f"UV_CURL={downloader}",
    )

    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_setup_requires_a_checksum_verifier(tmp_path: Path) -> None:
    archive = tmp_path / "uv.tar.gz"
    checksum = _write_uv_archive(archive)
    downloader = tmp_path / "curl"
    _fake_downloader(downloader)
    local_uv = tmp_path / "tools" / "uv"
    env = _make_env(tmp_path)
    env["UV_ARCHIVE_SOURCE"] = str(archive)

    result = _run_make(
        "setup",
        tmp_path,
        env,
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        "UV_TARGET=x86_64-unknown-linux-gnu",
        f"UV_SHA256={checksum}",
        f"UV_CURL={downloader}",
        "UV_SHA256SUM=missing-sha256sum",
        "UV_SHASUM=missing-shasum",
    )

    assert result.returncode != 0
    assert "requires sha256sum or shasum" in result.stderr
    assert not (tmp_path / "uv.log").exists()


def test_setup_falls_back_to_shasum_when_sha256sum_is_unavailable(tmp_path: Path) -> None:
    archive = tmp_path / "uv.tar.gz"
    checksum = _write_uv_archive(archive)
    downloader = tmp_path / "curl"
    _fake_downloader(downloader)
    shasum = tmp_path / "shasum"
    sha256sum = shutil.which("sha256sum")
    assert sha256sum is not None
    _write_executable(
        shasum,
        f"#!/bin/sh\nfor argument in \"$@\"; do input=\"$argument\"; done\nexec {sha256sum} \"$input\"\n",
    )
    local_uv = tmp_path / "tools" / "uv"
    env = _make_env(tmp_path)
    env["UV_ARCHIVE_SOURCE"] = str(archive)

    result = _run_make(
        "setup",
        tmp_path,
        env,
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        "UV_TARGET=x86_64-unknown-linux-gnu",
        f"UV_SHA256={checksum}",
        f"UV_CURL={downloader}",
        "UV_SHA256SUM=missing-sha256sum",
        f"UV_SHASUM={shasum}",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines() == [
        "--version",
        "sync --locked --extra full",
    ]


def test_setup_stops_when_bootstrap_download_fails(tmp_path: Path) -> None:
    downloader = tmp_path / "curl"
    _write_executable(downloader, "#!/bin/sh\nexit 7\n")
    local_uv = tmp_path / "tools" / "uv"

    result = _run_make(
        "setup",
        tmp_path,
        _make_env(tmp_path),
        f"UV={local_uv}",
        f"UV_LOCAL={local_uv}",
        "UV_TARGET=x86_64-unknown-linux-gnu",
        "UV_SHA256=0123456789abcdef",
        f"UV_CURL={downloader}",
    )

    assert result.returncode != 0
    assert not (tmp_path / "uv.log").exists()
