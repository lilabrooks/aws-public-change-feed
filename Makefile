PYTHON ?= python3
LYCHEE ?= lychee
PYTHON_PATHS := scripts tests $(wildcard src)
YAML_PATHS := .yamllint.yaml examples .github/dependabot.yml $(wildcard .github/workflows)

.PHONY: help install format format-check lint lint-python lint-yaml typecheck validate validate-config \
	validate-references references-online test whitespace check clean

help:
	@echo "Available targets:"
	@echo "  install       Install pinned development dependencies"
	@echo "  format        Format Python and apply safe lint fixes"
	@echo "  format-check  Check Python formatting without changing files"
	@echo "  lint          Run Python and YAML linters"
	@echo "  typecheck     Run mypy"
	@echo "  validate      Validate contracts, references, local links, and review dates"
	@echo "  references-online  Check external links with Lychee (requires network)"
	@echo "  test          Run the unittest suite"
	@echo "  whitespace    Check changed files for Git whitespace errors"
	@echo "  check         Run every non-mutating repository check"
	@echo "  clean         Remove generated development caches"

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

format:
	$(PYTHON) -m ruff check --fix $(PYTHON_PATHS)
	$(PYTHON) -m ruff format $(PYTHON_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(PYTHON_PATHS)

lint: lint-python lint-yaml

lint-python:
	$(PYTHON) -m ruff check $(PYTHON_PATHS)

lint-yaml:
	$(PYTHON) -m yamllint -c .yamllint.yaml $(YAML_PATHS)

typecheck:
	$(PYTHON) -m mypy $(PYTHON_PATHS)

validate: validate-config validate-references

validate-config:
	$(PYTHON) scripts/validate_config.py

validate-references:
	$(PYTHON) scripts/validate_references.py

references-online: validate-references
	$(LYCHEE) .

test:
	$(PYTHON) -m unittest discover -s tests

whitespace:
	git diff --check HEAD

check: format-check lint typecheck validate test whitespace

clean:
	find $(PYTHON_PATHS) -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache build dist
