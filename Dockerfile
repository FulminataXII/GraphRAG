# Multi-stage, one image, three entrypoints (api, worker, cli). See BLUEPRINT §1 / ARCHITECTURE
# §7.1. Same code, same config, different command — this is what guarantees the worker can
# never drift from the API.

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.9.9

# Same absolute path as the runtime stage's WORKDIR below: uv bakes the venv's absolute path
# into every script shebang (e.g. `#!/build/.venv/bin/python3`), so building at a different
# path than where the runtime stage copies it to produces a venv whose entrypoints 404 on
# their own interpreter (`exec .../uvicorn: no such file or directory`).
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY graphrag ./graphrag
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

RUN groupadd --system graphrag && useradd --system --gid graphrag --create-home graphrag

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/graphrag /app/graphrag
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER graphrag

# No EXPOSE here on purpose: this same image also runs as `worker`/`projection-worker` (BO-05),
# neither of which serves HTTP. `EXPOSE` is baked into the image and would show up as a
# misleading "8000/tcp" on those containers in `docker ps`/`docker compose ps` even though
# nothing listens on it. Compose's `ports:`/`expose:` publish the api service's port 8000
# independently of any image-level EXPOSE, so declaring it there (not here) keeps the port
# documented for `api` without leaking into the other two entrypoints.

# docker-compose.yml sets the real command per service (api / worker / projection-worker).
# This default matches the api service so `docker run` alone is still useful for a smoke test.
CMD ["uvicorn", "graphrag.apps.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
