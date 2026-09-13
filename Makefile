.PHONY: \
	ci \
	ci-fast \
	setup \
	lint \
	typecheck \
	arch \
	ci-contracts \
	integration-roslyn \
	tests \
	tests-full \
	sync-docs \
	package-smoke \
	install-hooks \
	install-ci-tools \
	install-full-tools

UV_MIN_VERSION := 0.12.1
UV_VERSION := 0.12.13
UV_LOCAL ?= $(CURDIR)/.tools/uv/uv
UV_PATH := $(shell command -v uv 2>/dev/null)
UV ?= $(or $(UV_PATH),$(UV_LOCAL))
UV_CURL ?= curl
UV_SHA256SUM ?= sha256sum
UV_SHASUM ?= shasum
UV_TARGET ?= $(shell \
	os=$$(uname -s); arch=$$(uname -m); \
	if [ "$$os" = Linux ]; then \
		libc=$$(ldd --version 2>&1 || :); \
		if printf '%s' "$$libc" | grep -qi musl; then suffix=musl; \
		elif printf '%s' "$$libc" | grep -Eqi 'GNU|GLIBC'; then suffix=gnu; else exit 0; fi; \
		if [ "$$arch" = x86_64 ] || [ "$$arch" = amd64 ]; then echo x86_64-unknown-linux-$$suffix; \
		elif [ "$$arch" = aarch64 ] || [ "$$arch" = arm64 ]; then echo aarch64-unknown-linux-$$suffix; fi; \
	elif [ "$$os" = Darwin ] && { [ "$$arch" = x86_64 ] || [ "$$arch" = amd64 ]; }; then echo x86_64-apple-darwin; \
	elif [ "$$os" = Darwin ] && { [ "$$arch" = arm64 ] || [ "$$arch" = aarch64 ]; }; then echo aarch64-apple-darwin; fi)
UV_SHA256 ?=
UV_ARCHIVE_URL ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_TARGET).tar.gz
UV_RUN = $(UV) run --locked
LINT_IMPORTS = $(UV_RUN) lint-imports
IMPORTLINTER_CONFIG ?= .github/importlinter.ini
PYTEST_XML ?=
PYTEST_XML_FLAG := $(if $(PYTEST_XML),--junitxml=$(PYTEST_XML),)

UV_X86_64_LINUX_GNU_SHA256 := 745765a3b6e360ad76743599ae5c42e9278c7edf8bbff9fc76d05bf2623a04dd
UV_AARCH64_LINUX_GNU_SHA256 := 2eaa5d94f5db7b3a1a092156b9420459e42ab0217d917fe74a876309cef9b5e9
UV_X86_64_LINUX_MUSL_SHA256 := 4e2bfd0c9007b1032a50e539e965fd0a6037d87ad93ae1580d220a92d4c94098
UV_AARCH64_LINUX_MUSL_SHA256 := f44bc1037a17889fe562fffd2002d4ed108e499fbe68b4f022af244dc7b8244f
UV_X86_64_DARWIN_SHA256 := 5e287ef61cb6a9b61b3a83fef124fd143e400468a7dac794230147a810e17119
UV_AARCH64_DARWIN_SHA256 := 7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed

setup:
	@set -eu; \
	if [ ! -x "$(UV)" ]; then \
		case "$(UV_TARGET)" in \
			x86_64-unknown-linux-gnu) expected="$(UV_X86_64_LINUX_GNU_SHA256)" ;; \
			aarch64-unknown-linux-gnu) expected="$(UV_AARCH64_LINUX_GNU_SHA256)" ;; \
			x86_64-unknown-linux-musl) expected="$(UV_X86_64_LINUX_MUSL_SHA256)" ;; \
			aarch64-unknown-linux-musl) expected="$(UV_AARCH64_LINUX_MUSL_SHA256)" ;; \
			x86_64-apple-darwin) expected="$(UV_X86_64_DARWIN_SHA256)" ;; \
			aarch64-apple-darwin) expected="$(UV_AARCH64_DARWIN_SHA256)" ;; \
			*) echo "error: unsupported uv platform: $(UV_TARGET)" >&2; exit 1 ;; \
		esac; \
		if [ -n "$(UV_SHA256)" ]; then expected="$(UV_SHA256)"; fi; \
		archive=$$(mktemp); \
		trap 'rm -f "$$archive"' EXIT; \
		"$(UV_CURL)" --fail --location --silent --show-error "$(UV_ARCHIVE_URL)" -o "$$archive"; \
		if command -v "$(UV_SHA256SUM)" >/dev/null 2>&1; then \
			actual=$$("$(UV_SHA256SUM)" "$$archive" | awk '{print $$1}'); \
		elif command -v "$(UV_SHASUM)" >/dev/null 2>&1; then \
			actual=$$("$(UV_SHASUM)" -a 256 "$$archive" | awk '{print $$1}'); \
		else \
			echo "error: requires sha256sum or shasum for uv verification" >&2; exit 1; \
		fi; \
		if [ "$$actual" != "$$expected" ]; then \
			echo "error: uv archive checksum verification failed" >&2; exit 1; \
		fi; \
		mkdir -p "$$(dirname "$(UV_LOCAL)")"; \
		tar -xzf "$$archive" --strip-components=1 -C "$$(dirname "$(UV_LOCAL)")"; \
	fi; \
	if [ ! -x "$(UV)" ]; then \
		echo "error: uv binary not found at $(UV)" >&2; exit 1; \
	fi; \
	version=$$("$(UV)" --version | awk 'NR == 1 {print $$2}'); \
	if ! awk -v actual="$$version" -v minimum="$(UV_MIN_VERSION)" 'BEGIN { \
		actual_count = split(actual, a, "."); minimum_count = split(minimum, b, "."); \
		if (actual_count != 3 || minimum_count != 3) exit 1; \
		for (i = 1; i <= 3; i++) if (a[i] !~ /^[0-9][0-9]*$$/ || b[i] !~ /^[0-9][0-9]*$$/) exit 1; \
		for (i = 1; i <= 3; i++) { if (a[i] + 0 > b[i] + 0) exit 0; if (a[i] + 0 < b[i] + 0) exit 1; } \
		exit 0; \
	}'; then \
		echo "error: setup requires uv >= $(UV_MIN_VERSION), found $$version" >&2; exit 1; \
	fi; \
	"$(UV)" sync --locked --extra full

sync-docs:
	mkdir -p desloppify/data/global
	find desloppify/data/global -maxdepth 1 -type f -name '*.md' -delete
	cp docs/*.md desloppify/data/global/

install-hooks:
	mkdir -p .git/hooks
	cp .githooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Git hooks installed."

install-ci-tools: setup

install-full-tools: setup

lint: setup
	$(UV_RUN) ruff check . --select E9,F63,F7,F82

typecheck: setup
	$(UV_RUN) python -m mypy

arch: setup
	@if [ ! -f "$(IMPORTLINTER_CONFIG)" ]; then \
		echo "Missing $(IMPORTLINTER_CONFIG). Add import contracts before running arch gate."; \
		exit 1; \
	fi
	$(LINT_IMPORTS) --config $(IMPORTLINTER_CONFIG)

ci-contracts: setup
	$(UV_RUN) pytest -q desloppify/tests/ci/test_ci_contracts.py
	$(UV_RUN) pytest -q desloppify/tests/commands/test_lifecycle_transitions.py -k "assessment_then_score_when_no_review_followup"

integration-roslyn: setup
	$(UV_RUN) pytest -q desloppify/tests/lang/csharp/test_csharp_deps.py -k "roslyn"

tests: setup
	$(UV_RUN) pytest -q $(PYTEST_XML_FLAG)

tests-full: setup
	$(UV_RUN) pytest -q $(PYTEST_XML_FLAG)

package-smoke: setup
	rm -rf dist .pkg-smoke
	$(UV_RUN) python -m build
	$(UV_RUN) twine check dist/*
	$(UV_RUN) python -m venv .pkg-smoke
	. .pkg-smoke/bin/activate && \
		python -m pip install --upgrade pip && \
		WHEEL=$$(ls -t dist/desloppify-*.whl | head -n 1) && \
		python -m pip install "$$WHEEL[full]" && \
		python -c "from importlib.resources import files; from pathlib import Path; docs=Path('docs'); bundled=files('desloppify.data.global'); names=sorted(p.name for p in docs.glob('*.md')); assert names; missing=[name for name in names if not bundled.joinpath(name).is_file()]; assert not missing, f'missing bundled docs: {missing}'; mismatched=[name for name in names if bundled.joinpath(name).read_text(encoding='utf-8') != (docs / name).read_text(encoding='utf-8')]; assert not mismatched, f'mismatched bundled docs: {mismatched}'" && \
		python -c "import importlib.metadata as m,sys; extras=set(m.metadata('desloppify').get_all('Provides-Extra') or []); required={'full','treesitter','python-security','scorecard'}; missing=required-extras; print('missing extras metadata:', sorted(missing)) if missing else None; sys.exit(1 if missing else 0)" && \
		desloppify --help > /dev/null
	rm -rf .pkg-smoke

ci-fast: lint typecheck arch ci-contracts tests

ci: ci-fast tests-full package-smoke
