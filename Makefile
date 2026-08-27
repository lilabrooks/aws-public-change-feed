PYTHON ?= python3
LYCHEE ?= lychee
PYTHON_PATHS := scripts tests $(wildcard src)
MYPY_PATHS := $(PYTHON_PATHS)
YAML_PATHS := .yamllint.yaml examples .github/dependabot.yml $(wildcard .github/workflows)
TERRAFORM ?= terraform
REQUIRE_TERRAFORM ?= 0
TERRAFORM_ROOTS := infra/bootstrap infra/central infra/preflight

.PHONY: help install format format-check lint lint-python lint-yaml typecheck validate validate-config \
	validate-references validate-site generate-slack-sample evaluate-corpus references-online screen-feeds terraform-check \
	test whitespace check clean

help:
	@echo "Available targets:"
	@echo "  install       Install pinned development dependencies"
	@echo "  format        Format Python and apply safe lint fixes"
	@echo "  format-check  Check Python formatting without changing files"
	@echo "  lint          Run Python and YAML linters"
	@echo "  typecheck     Run mypy"
	@echo "  validate      Validate contracts, references, local links, review dates, and the public page"
	@echo "  generate-slack-sample  Refresh the public sample from the canonical delivery renderer"
	@echo "  evaluate-corpus    Score matching against the labeled corpus and approved thresholds"
	@echo "  references-online  Check external links with Lychee (requires network)"
	@echo "  screen-feeds       Screen live feeds against the rules (requires network)"
	@echo "  terraform-check    Format-check and validate Terraform roots (REQUIRE_TERRAFORM=1 fails if absent)"
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

validate: validate-config validate-references validate-site evaluate-corpus

validate-config:
	$(PYTHON) scripts/validate_config.py

validate-references:
	$(PYTHON) scripts/validate_references.py

validate-site:
	$(PYTHON) scripts/validate_site.py

generate-slack-sample:
	$(PYTHON) scripts/generate_slack_sample.py --write

evaluate-corpus:
	$(PYTHON) scripts/evaluate_corpus.py --config config/dev.yaml

references-online: validate-references
	$(LYCHEE) .

screen-feeds:
	$(PYTHON) scripts/screen_feeds.py --config config/dev.yaml

# One recipe line keeps the absent-binary guard and Terraform loop in one shell.
# Split across two lines Make runs two shells, so the loop would still run after
# the first line reported a skip or required-mode failure.
terraform-check:
	@if ! command -v $(TERRAFORM) >/dev/null 2>&1; then \
		if [ "$(REQUIRE_TERRAFORM)" = "1" ]; then \
			echo "terraform is required but not installed" >&2; \
			exit 1; \
		fi; \
		echo "terraform not installed; skipping terraform-check"; \
	else \
		for root in $(TERRAFORM_ROOTS); do \
			echo "Checking $$root"; \
			$(TERRAFORM) -chdir=$$root fmt -check || exit 1; \
			$(TERRAFORM) -chdir=$$root init -backend=false -input=false -lockfile=readonly || exit 1; \
			$(TERRAFORM) -chdir=$$root validate || exit 1; \
		done; \
	fi

test:
	$(PYTHON) -m unittest discover -s tests

whitespace:
	git diff --check HEAD

check: format-check lint typecheck validate test terraform-check whitespace

clean:
	find $(PYTHON_PATHS) -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache build dist
