.PHONY: up down test test-int test-all test-llm lint eval trail migrate

# Docker Compose only auto-loads docker-compose.yml — docker-compose.obs.yml must be named
# explicitly with -f, or `--profile obs` selects a profile that no loaded file declares and
# silently starts nothing extra (the failure BO-02 needs `make up obs=1` to hit).
COMPOSE := docker compose --profile core
COMPOSE_OBS := docker compose -f docker-compose.yml -f docker-compose.obs.yml --profile core --profile obs

# Bring up the core data plane + gateway (add `obs=1` to also bring up the observability plane).
up:
ifeq ($(obs),1)
	$(COMPOSE_OBS) up -d
else
	$(COMPOSE) up -d
endif

down:
	$(COMPOSE_OBS) down

# Unit tests only — no containers, no network.
test:
	uv run pytest -m "not integration and not eval"

# Integration tests — requires `make up` first. Excludes llm_quota (real provider calls on a
# free tier with an 8000 TPM ceiling — see `test-llm`); left in here they drain the day's budget
# and later stages fail for reasons unrelated to their own code (BUILD_ORDER BO-06).
test-int:
	uv run pytest -m "integration and not llm_quota"

# Everything except eval and llm_quota (eval needs live LLM provider keys; llm_quota burns them).
test-all:
	uv run pytest -m "not eval and not llm_quota"

# Quota-consuming integration tests only — run deliberately, not as part of test-int/test-all.
# A 429 here is a quota result, not a defect.
test-llm:
	uv run pytest -m llm_quota

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy $(wildcard graphrag/core graphrag/services graphrag/config)
	uv run python scripts/check_layering.py

eval:
	uv run pytest -m eval

trail:
	uv run graphrag trail $(cid)

# Apply Postgres migrations against the host-published port (config/local.yaml's `stores`
# overrides only cover qdrant/neo4j/redis; the Postgres DSN is a secret, so point APP_ENV at
# local and override just the DSN host here). Requires `make up` first.
migrate:
	APP_ENV=local GRAPHRAG_SECRETS__POSTGRES_DSN=postgresql://$${POSTGRES_USER:-graphrag}:$${POSTGRES_PASSWORD:-changeme}@localhost:5432/$${POSTGRES_DB:-graphrag} uv run alembic upgrade head
