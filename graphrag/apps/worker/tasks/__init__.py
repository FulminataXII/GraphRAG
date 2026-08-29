"""arq task registry. See BLUEPRINT §7.2.

Uploaded file bytes travel from the `api` process to `worker` processes through a local-disk
convention neither BLUEPRINT nor ARCHITECTURE specifies: `IngestDocumentPayload` carries only a
`uri` string (no bytes field — `JobEnvelope` must be JSON-serializable end to end, and
`limits.max_upload_mb` allows uploads far too large to base64 into one anyway), so the worker
must independently resolve `uri` back to bytes. `apps/api/routers/documents.py` writes each
upload to a directory shared between the `api` and `worker` containers via a docker-compose bind
mount (`./data/uploads` on the host, `/app/data/uploads` in both containers) and sets
`uri = file:///app/data/uploads/{doc_id}`; `tasks/ingest.py` reads it back with a plain
`Path.read_bytes()`. Reported as a BO-05 spec gap — replacing this with real object storage
(S3/MinIO) is a reasonable follow-up, not something this BO invents config for on its own.
"""

from __future__ import annotations
