# CS-OP-ARCH-002 §13. `make dev` must go clone -> running in under a minute.
.POSIX:
.PHONY: dev test seed clean image check gates fmt help

PY      ?= python3
DATA    ?= ./data
PORT    ?= 8080
IMAGE   ?= ghcr.io/commsecurityau/cs-ops
SHA     := $(shell git rev-parse --short=7 HEAD 2>/dev/null || echo dev)

help:
	@echo "dev    - run locally on :$(PORT), TLS off, data in $(DATA)"
	@echo "test   - full suite (<10s)"
	@echo "gates  - CI gates only (pinning, deps, secrets)"
	@echo "check  - test + gates, what CI runs"
	@echo "seed   - import the FY27 register into $(DATA)/ops.db"
	@echo "image  - build the container"
	@echo "clean  - remove $(DATA), caches, snapshots"

dev:
	@mkdir -p $(DATA)/secrets
	@test -f $(DATA)/secrets/store.json || \
		(echo "no secret store; run: echo -n 'value' | $(PY) -m ops.secrets set OIDC_CLIENT_SECRET" \
		 && echo "  (OPS_SECRETS_PATH=$(DATA)/secrets/store.json)")
	OPS_DATA=$(DATA) OPS_TLS=off OPS_PORT=$(PORT) \
	OPS_SECRETS_PATH=$(DATA)/secrets/store.json \
	$(PY) -m ops.main

test:
	$(PY) -W error::ResourceWarning -m unittest discover -s tests

gates:
	$(PY) -W error::ResourceWarning -m unittest tests.test_gates

check: test gates

seed:
	@mkdir -p $(DATA)
	$(PY) -c "import sys; sys.path.insert(0,'.'); \
		from ops.db import Db; Db('$(DATA)/ops.db','ops/migrations').migrate()"
	$(PY) tools/import_register.py \
		--csv tests/fixtures/project_register_fy27.csv --db $(DATA)/ops.db

image:
	docker build -t $(IMAGE):$(SHA) -t $(IMAGE):latest .
	@docker run --rm $(IMAGE):$(SHA) python3 -c \
		"import sqlite3;print('sqlite',sqlite3.sqlite_version)"
	@echo "size: $$(docker image inspect $(IMAGE):$(SHA) --format='{{.Size}}' | awk '{printf \"%.1f MB\", $$1/1048576}')"

clean:
	rm -rf $(DATA)
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
