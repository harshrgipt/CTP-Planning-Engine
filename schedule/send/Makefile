SHELL := /bin/bash
ROOT := $(shell pwd)
PY := $(ROOT)/.venv/bin/python
PIP := $(ROOT)/.venv/bin/pip

.PHONY: venv ingest bom construction balance wip learn replay plan optimize sim validate smoke test all clean

venv:
	@bash scripts/bootstrap.sh

ingest: venv
	$(PY) -m planner.cli ingest

bom: venv
	$(PY) -m planner.cli bom

construction: venv
	$(PY) -m planner.cli construction

balance: venv
	$(PY) -m planner.cli balance

wip: venv
	$(PY) -m planner.cli wip

learn: venv
	$(PY) -m planner.cli learn

replay: venv
	$(PY) -m planner.cli replay

plan: venv
	$(PY) -m planner.cli plan

optimize: venv
	$(PY) -m planner.cli optimize

sim: venv
	$(PY) -m planner.cli sim

validate: venv
	$(PY) -m planner.cli validate

smoke: venv
	$(PY) -m planner.cli smoke

test: venv
	$(PY) -m pytest tests/

all: venv ingest bom construction balance learn smoke test
	@echo "[all] full pipeline built."

clean:
	rm -rf runs/*.tmp warehouse/**/_SUCCESS
