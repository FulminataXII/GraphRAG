.PHONY: up down test test-int test-all lint eval trail

COMPOSE := docker compose --profile core
COMPOSE_OBS := docker compose --profile core --profile obs

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

# Integration tests — requires `make up` first.
test-int:
	uv run pytest -m integration

# Everything except eval (eval needs live LLM provider keys).
test-all:
	uv run pytest -m "not eval"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy $(wildcard graphrag/core graphrag/services graphrag/config)
	uv run python scripts/check_layering.py

eval:
	uv run pytest -m eval

trail:
	uv run graphrag trail $(cid)
