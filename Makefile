PYTHON ?= .venv/bin/python

.PHONY: install test serve site confidence-data confidence-check experiments crawl offchain provenance payments grounding reproduce

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest

serve:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8000}

site:
	python3 -m app.build

confidence-data:
	$(PYTHON) -m eval.confidence

confidence-check:
	$(PYTHON) -m eval.confidence --check

experiments:
	$(PYTHON) -m sim.confidence

crawl:
	$(PYTHON) -m crawl.cli crawl --all

offchain:
	$(PYTHON) -m crawl.cli offchain --all

provenance:
	$(PYTHON) -m crawl.cli provenance --all

payments:
	$(PYTHON) -m crawl.cli payments --all

grounding:
	$(PYTHON) -m eval.grounding --config config.yaml --chain base

reproduce:
	$(PYTHON) -m crawl.cli reproduce --all
