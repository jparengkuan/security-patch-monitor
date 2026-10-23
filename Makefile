VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: help install run test lint format typecheck check app icon screenshot clean

help:  ## Show available targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

$(PY):
	python3 -m venv $(VENV)

install: $(PY)  ## Install the app with desktop and development extras (editable)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[desktop,dev]"

run:  ## Start the desktop app
	$(VENV)/bin/security-patch-monitor

test:  ## Run the test suite
	$(VENV)/bin/pytest

lint:  ## Lint and check formatting
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

format:  ## Auto-format and fix lint issues
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

typecheck:  ## Static type checking (mypy --strict)
	$(VENV)/bin/mypy src

check: lint typecheck test  ## Everything CI runs

app:  ## Build ~/Applications/Security Patch Monitor.app (macOS)
	scripts/build_macos_app.sh

icon:  ## Regenerate the icons (needs Pillow)
	$(PY) tools/make_icon.py

clean:  ## Remove build and cache artefacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -not -path "./$(VENV)/*" -exec rm -rf {} +
