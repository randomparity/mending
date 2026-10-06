"""Direct coverage tests for the update-skill command module."""

from __future__ import annotations

import argparse
import email.message
import io
import urllib.request
import urllib.response
from pathlib import Path

import pytest

import desloppify.app.commands.update_skill.cmd as update_skill_cmd_mod
from desloppify.base.exception_sets import CommandError


def test_update_skill_helper_functions_cover_frontmatter_resolution_and_replace() -> None:
    content = (
        "<!-- desloppify-begin -->\n"
        "<!-- version -->\n"
        "---\n"
        "name: skill\n"
        "---\n"
        "body\n"
    )
    reordered = update_skill_cmd_mod._ensure_frontmatter_first(content)
    assert reordered.startswith("---\nname: skill\n---\n")
    assert "<!-- desloppify-begin -->" in reordered

    section = update_skill_cmd_mod._build_section("skill body\n", "overlay body\n")
    assert section == "skill body\n\noverlay body\n"

    replaced = update_skill_cmd_mod._replace_section(
        f"prefix\n\n{update_skill_cmd_mod.SKILL_BEGIN}\nold\n{update_skill_cmd_mod.SKILL_END}\n",
        "new section\n",
    )
    assert "prefix" in replaced
    assert "new section" in replaced
    assert "old" not in replaced


def test_resolve_interface_prefers_explicit_then_install_metadata(monkeypatch) -> None:
    assert update_skill_cmd_mod.resolve_interface("CoDeX") == "codex"

    install = update_skill_cmd_mod.SkillInstall(
        rel_path=".claude/skills/desloppify/SKILL.md",
        version=5,
        overlay="windsurf",
        stale=False,
    )
    assert update_skill_cmd_mod.resolve_interface(None, install=install) == "windsurf"

    inferred = update_skill_cmd_mod.SkillInstall(
        rel_path=".cursor/rules/desloppify.md",
        version=5,
        overlay=None,
        stale=False,
    )
    monkeypatch.setattr(update_skill_cmd_mod, "find_installed_skill", lambda: inferred)
    assert update_skill_cmd_mod.resolve_interface() == "cursor"


def test_update_installed_skill_handles_download_and_shared_file_write(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    skill_content = (
        "<!-- desloppify-begin -->\n"
        "<!-- desloppify-skill-version: 5 -->\n"
        "---\n"
        "name: desloppify\n"
        "---\n"
        "body\n"
        "<!-- desloppify-end -->\n"
    )
    overlay_content = "overlay text\n"
    writes: list[tuple[Path, str]] = []
    target = tmp_path / ".agents" / "skills" / "desloppify" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("prefix only", encoding="utf-8")

    monkeypatch.setattr(
        update_skill_cmd_mod,
        "_download",
        lambda filename: skill_content if filename == "SKILL.md" else overlay_content,
    )
    monkeypatch.setattr(update_skill_cmd_mod, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        update_skill_cmd_mod,
        "safe_write_text",
        lambda path, text: writes.append((path, text)) or path.write_text(text, encoding="utf-8"),
    )
    monkeypatch.setattr(update_skill_cmd_mod, "colorize", lambda text, _style: text)

    assert update_skill_cmd_mod.update_installed_skill("codex") is True
    assert writes and writes[-1][0] == target
    written = target.read_text(encoding="utf-8")
    assert written.startswith("---\nname: desloppify\n---\n")
    assert "overlay text" in written
    out = capsys.readouterr().out
    assert "Updated .agents/skills/desloppify/SKILL.md" in out


def test_cmd_update_skill_handles_missing_and_unknown_interfaces(monkeypatch, capsys) -> None:
    monkeypatch.setattr(update_skill_cmd_mod, "resolve_interface", lambda _explicit=None: None)
    monkeypatch.setattr(update_skill_cmd_mod, "colorize", lambda text, _style: text)
    update_skill_cmd_mod.cmd_update_skill(argparse.Namespace(interface=None))
    out = capsys.readouterr().out
    assert "No installed skill document found." in out

    monkeypatch.setattr(
        update_skill_cmd_mod,
        "resolve_interface",
        lambda _explicit=None: "unknown_thing",
    )
    update_skill_cmd_mod.cmd_update_skill(argparse.Namespace(interface=None))
    out = capsys.readouterr().out
    assert "Unknown interface 'unknown_thing'." in out


class _Response:
    def __init__(
        self, body: bytes = b"skill text", url: str | None = None, length: int | None = None
    ) -> None:
        self._body = body
        self._url = url
        self.length = length
        self.read_sizes: list[int | None] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def geturl(self) -> str:
        return self._url or f"{update_skill_cmd_mod._RAW_BASE}/SKILL.md"

    def read(self, amt: int | None = None) -> bytes:
        self.read_sizes.append(amt)
        chunk = self._body if amt is None else self._body[:amt]
        if self.length is not None:  # http.client.HTTPResponse counts down what remains
            self.length -= len(chunk)
        return chunk


def _fake_opener(monkeypatch, response: _Response) -> tuple[list[str], list[object]]:
    requested: list[str] = []
    handlers: list[object] = []

    class _Opener:
        def open(self, url, **_kwargs):
            requested.append(url)
            return response

    def _build_opener(*given):
        handlers.extend(given)
        return _Opener()

    monkeypatch.setattr(update_skill_cmd_mod.urllib.request, "build_opener", _build_opener)
    return requested, handlers


def test_download_fetches_from_this_repository_docs(monkeypatch) -> None:
    context = update_skill_cmd_mod.ssl.create_default_context()
    monkeypatch.setattr(update_skill_cmd_mod, "_ssl_context", lambda: context)
    requested, handlers = _fake_opener(monkeypatch, _Response())

    assert update_skill_cmd_mod._download("SKILL.md") == "skill text"
    assert requested == [
        "https://raw.githubusercontent.com/randomparity/mending/main/docs/SKILL.md"
    ]
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert [h._context for h in https] == [context]
    assert any(isinstance(h, update_skill_cmd_mod._SourceRedirectHandler) for h in handlers)


def test_download_accepts_body_at_the_size_limit(monkeypatch) -> None:
    limit = update_skill_cmd_mod._MAX_DOWNLOAD_BYTES
    _fake_opener(monkeypatch, _Response(b"x" * limit))

    assert len(update_skill_cmd_mod._download("SKILL.md")) == limit


def test_download_rejects_body_over_the_size_limit(monkeypatch) -> None:
    limit = update_skill_cmd_mod._MAX_DOWNLOAD_BYTES
    response = _Response(b"x" * (limit + 1))
    _fake_opener(monkeypatch, response)

    with pytest.raises(CommandError) as excinfo:
        update_skill_cmd_mod._download("SKILL.md")
    assert response.read_sizes == [limit + 1]
    message = str(excinfo.value)
    assert "Download of SKILL.md" in message
    assert str(limit) in message
    assert "desloppify update-skill" in message


def test_download_rejects_body_shorter_than_declared_length(monkeypatch) -> None:
    _fake_opener(monkeypatch, _Response(b"partial", length=100))

    with pytest.raises(CommandError) as excinfo:
        update_skill_cmd_mod._download("SKILL.md")
    message = str(excinfo.value)
    assert "Download of SKILL.md ended before its declared length" in message
    assert "desloppify update-skill" in message


def test_download_accepts_body_that_satisfied_declared_length(monkeypatch) -> None:
    _fake_opener(monkeypatch, _Response(b"skill text", length=len(b"skill text")))

    assert update_skill_cmd_mod._download("SKILL.md") == "skill text"


def test_download_rejects_final_url_on_another_host(monkeypatch) -> None:
    _fake_opener(monkeypatch, _Response(url="https://evil.example/SKILL.md"))

    with pytest.raises(CommandError) as excinfo:
        update_skill_cmd_mod._download("SKILL.md")
    assert "evil.example" not in str(excinfo.value)


def _redirect(target: str, header: str = "Location"):
    class _Parent:
        def open(self, new_request, **_kwargs):
            return new_request

    handler = update_skill_cmd_mod._SourceRedirectHandler("SKILL.md")
    handler.parent = _Parent()
    request = urllib.request.Request(f"{update_skill_cmd_mod._RAW_BASE}/SKILL.md")
    request.timeout = 15  # set by OpenerDirector.open in real use
    headers = email.message.Message()
    headers[header] = target
    return handler.http_error_302(request, io.BytesIO(b""), 302, "Found", headers)


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/SKILL.md",
        "https://raw.githubusercontent.com.evil.example/SKILL.md",
        "https://raw.githubusercontent.com@evil.example/SKILL.md",
        "https://user@raw.githubusercontent.com/SKILL.md",
        "http://raw.githubusercontent.com/SKILL.md",
        "https://raw.githubusercontent.com:8443/SKILL.md",
        "https://raw.githubusercontent.com:bad/SKILL.md",
        "//evil.example/SKILL.md",
        "file:///etc/passwd",
        "data:text/plain,\x1b[31mowned",
    ],
)
def test_redirect_handler_rejects_targets_off_the_source_host(target: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        _redirect(target)
    message = str(excinfo.value)
    assert "Download of SKILL.md" in message
    assert "raw.githubusercontent.com" in message
    assert "desloppify update-skill" in message
    assert target not in message
    assert "evil" not in message


def test_redirect_handler_checks_uri_header_when_location_is_absent() -> None:
    with pytest.raises(CommandError):
        _redirect("https://evil.example/SKILL.md", header="URI")


def test_redirect_handler_follows_same_host_https_redirect() -> None:
    new_request = _redirect("/randomparity/mending/other/docs/SKILL.md")

    assert new_request.full_url == (
        "https://raw.githubusercontent.com/randomparity/mending/other/docs/SKILL.md"
    )


def test_download_rejects_cross_host_redirect_through_real_opener(monkeypatch) -> None:
    opened: list[str] = []

    class _RedirectingHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            opened.append(req.full_url)
            headers = email.message.Message()
            headers["Location"] = "https://evil.example/SKILL.md"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            response.msg = "Found"
            return response

    monkeypatch.setattr(update_skill_cmd_mod.urllib.request, "HTTPSHandler", _RedirectingHandler)

    with pytest.raises(CommandError) as excinfo:
        update_skill_cmd_mod._download("SKILL.md")
    assert "evil.example" not in str(excinfo.value)
    assert opened == [f"{update_skill_cmd_mod._RAW_BASE}/SKILL.md"]
