"""Local-disk upload convention shared by `apps/api/routers/documents.py` and the CLI's
`ingest`/`seed` commands, and read back by `apps/worker/tasks/ingest.py`.

Not a BLUEPRINT-named component: neither BLUEPRINT nor ARCHITECTURE specifies how raw upload
bytes travel from the process that receives them to the worker that parses them.
`IngestDocumentPayload` carries only a `uri` string (`JobEnvelope` must be JSON-serializable end
to end, and uploads up to `limits.max_upload_mb` are far too large to base64 into one anyway).
The convention here: write bytes to a directory shared between the `api` and `worker` containers
via a docker-compose bind mount (`./data/uploads` on the host, `/app/data/uploads` in both
containers) and hand back the CONTAINER-side `file://` path, since only the worker ever reopens
it. Reported as a BO-05 spec gap — replacing this with real object storage (S3/MinIO) is a
reasonable follow-up, not something this BO invents config for on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

UPLOAD_DIR_HOST: Final[Path] = Path("data/uploads")
UPLOAD_DIR_CONTAINER: Final[str] = "/app/data/uploads"


def persist_upload(raw: bytes, doc_id: str) -> str:
    """Write `raw` under the shared uploads directory and return its container-side `file://`
    URI. Idempotent: re-persisting the same doc_id (content-addressed) just overwrites with the
    same bytes."""
    UPLOAD_DIR_HOST.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR_HOST / doc_id).write_bytes(raw)
    return f"file://{UPLOAD_DIR_CONTAINER}/{doc_id}"
