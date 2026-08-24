# CS-OP-ARCH-002 §13. `make dev` must go clone -> running in under a minute.
.POSIX:
.PHONY: dev test seed clean image check gates session help

PY      ?= python3
DATA    ?= ./data
PORT    ?= 5173   # 8080 is commonly held by Docker on dev machines
IMAGE   ?= ghcr.io/commsecurityau/cs-ops
# Placeholder so the dev server boots before the real client is registered.
# /login will not work until OIDC_CLIENT_ID is real; use `make session`.
DEV_CLIENT_ID ?= dev-client-not-registered
SHA     := $(shell git rev-parse --short=7 HEAD 2>/dev/null || echo dev)

help:
	@echo "dev    - run locally on :$(PORT), TLS off, data in $(DATA)"
	@echo "test   - full suite (<10s)"
	@echo "gates  - CI gates only (pinning, deps, secrets)"
	@echo "check  - test + gates, what CI runs"
	@echo "seed   - import the FY27 register into $(DATA)/ops.db"
	@echo "session- mint a local session cookie (dev only, no OIDC needed)"
	@echo "image  - build the container"
	@echo "clean  - remove $(DATA), caches, snapshots"

dev:
	@mkdir -p $(DATA)/secrets
	@test -f $(DATA)/secrets/store.json || \
		printf 'dev-not-a-real-secret' | OPS_SECRETS_PATH=$(DATA)/secrets/store.json \
		$(PY) -m ops.secrets set OIDC_CLIENT_SECRET
	OPS_DATA=$(DATA) OPS_TLS=off OPS_PORT=$(PORT) \
	OPS_SECRETS_PATH=$(DATA)/secrets/store.json \
	OIDC_CLIENT_ID=$(DEV_CLIENT_ID) \
	OIDC_REDIRECT_URI=http://localhost:$(PORT)/auth/callback \
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

session:
	OPS_TLS=off $(PY) tools/dev_session.py --data $(DATA) --port $(PORT)

image:
	docker build -t $(IMAGE):$(SHA) -t $(IMAGE):latest .
	@docker run --rm $(IMAGE):$(SHA) python3 -c \
		"import sqlite3;print('sqlite',sqlite3.sqlite_version)"
	@echo "size: $$(docker image inspect $(IMAGE):$(SHA) --format='{{.Size}}' | awk '{printf \"%.1f MB\", $$1/1048576}')"

clean:
	rm -rf $(DATA)
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
