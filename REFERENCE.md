# Reference Manual

This document provides a comprehensive overview of the functioning API endpoints and the CLI tools available for operating, testing, and debugging the Hybrid GraphRAG system.

## REST API Endpoints

The FastAPI server exposes several endpoints for interaction and job management.

### `POST /v1/query`
Synchronous query endpoint that runs the query through the full LangGraph orchestration pipeline.
- **Request Body**: `{"query": "string"}`
- **Response**: Returns the final generated answer, the strict array of exact citations, the routing strategy taken, and the `correlation_id` for distributed tracing.

### `POST /v1/query/stream`
Server-Sent Events (SSE) token stream endpoint.
- **Request Body**: `{"query": "string"}`
- **Response**: Emits `node_start` and `node_end` events as the LangGraph state machine traverses the pipeline. Excellent for building a live, agentic UI that shows the user exactly what the orchestrator is doing (e.g. routing, retrieving, grading, regenerating).

### `POST /v1/documents`
Asynchronous ingestion endpoint to register and upload a document.
- **Headers**: Requires `Idempotency-Key` (typically a UUID).
- **Form Data**: `file=@your_file.txt`
- **Response**: `202 Accepted`. Returns a `job_id`, `doc_id`, and `correlation_id`. The document processing (chunking, entity extraction, Neo4j mapping) happens asynchronously in the background.

### `GET /v1/jobs/{id}`
Check the status of an asynchronous ingestion job.
- **Path Parameter**: The `job_id` returned from `/v1/documents`.
- **Response**: Returns the current state of the document in the ingestion state machine.

### `DELETE /v1/documents/{id}`
Asynchronous deletion endpoint.
- **Path Parameter**: The `doc_id`.
- **Response**: `202 Accepted` + `job_id`. The background worker will perform a refcount-aware deletion of the document chunks and graph relationships.

---

## CLI Operations (Typer)

The CLI (invoked via `uv run graphrag`) provides direct control over the background planes, skipping the FastAPI layer. This is primarily for operations, debugging, and administration.

### `ingest`
Ingest a file or directory through the same pipeline as `POST /v1/documents`.
- **Usage**: `uv run graphrag ingest <path>`
- **Options**:
  - `--recursive`: Recurse into subdirectories.
  - `--wait`: Block until each document reaches `INDEXED` or `FAILED`.
  - `--force`: Repair a partial ingest. Purges the ledger row and arq's retained job keys for each document that is not INDEXED, then re-enqueues it. (Note: Re-pays extraction quota for every document it touches).
  - `--even-if-indexed`: With `--force`, also re-ingest documents already at `INDEXED`.

### `seed`
Automatically run the `ingest` command specifically for the `corpus/` directory without waiting.
- **Usage**: `uv run graphrag seed`

### `query`
Direct hybrid-search debug query. Embeds text and runs a single Qdrant hybrid_search call (bypasses the full orchestration pipeline).
- **Usage**: `uv run graphrag query "your question"`
- **Options**:
  - `--show-chunk-ids`: Print each retrieved chunk's UUID beside its text.
  - `--json`: Print raw JSON instead of a plain list.
  - `--strategy`: Retrieval strategy (currently `vector` is the default bypass).
  - `--top-k`: Override `retrieval.vector.top_k`.

### `reindex`
Re-project Qdrant payloads from Postgres. Acts as a reconciliation entrypoint if the vector store and ledger fall out of sync.
- **Usage**: `uv run graphrag reindex [--corpus-dir <path>]`

### `node`
Run ONE orchestration node by hand against real dependencies and print what it returned. Extremely useful for tuning router or grader prompts without running the full 4-call end-to-end pipeline.
- **Usage**: `uv run graphrag node <name> [--question "text"]`
- **Options**:
  - `<name>`: The name of the node to run. Available nodes:
    - `guard`: Deterministic initial check (budget/safety constraints).
    - `plan_route`: LLM router deciding if vector, graph, or hybrid search is needed.
    - `retrieve_vector`: Executes Qdrant hybrid search.
    - `retrieve_graph`: Executes Cypher traversal in Neo4j.
    - `fuse`: Merges retrieved results client-side using Reciprocal Rank Fusion (RRF).
    - `grade_context`: LLM evaluates if the fused context is relevant to the question.
    - `rewrite_query`: LLM rewrites the query for better retrieval if context was poor.
    - `generate`: LLM generates the final answer with citations.
    - `verify_citations`: Deterministic check to ensure all cited chunks exist in the retrieved context.
    - `verify_grounded`: LLM-as-a-judge check ensuring the answer is fully supported by the citations.
    - `repair`: LLM attempts to fix the answer if it failed groundedness, citations, or schema validation.
    - `finalize`: Formats the successful response to exit the pipeline.
    - `insufficient`: Formats a graceful fallback if all retries are exhausted.
  - `--chunk-ids`: Comma-separated chunk UUIDs to use as context instead of retrieving.
  - `--top-k`: Override `retrieval.vector.top_k`.
  - `--strategy`: `vector` | `graph` | `hybrid`.
  - `--json`: Print the full state update and LLM call log as JSON.

### `trail`
*(Admin / Dev Only)* Build a paste-ready debug markdown bundle for a given `correlation_id` and write it to disk.
- **Usage**: `uv run graphrag trail <correlation_id> [--out <path>]`
- **Output**: Generates a file like `debug_bundle_<cid>.md` containing the full trace, inputs, LLM responses, and errors associated with that trace.

---

## Observability & Debugging

This section details how to track, debug, and monitor queries and background jobs across the asynchronous boundaries of the system.

### 1. How to Find the Correlation ID
The `correlation_id` is the primary key for tracing any request across the synchronous API and asynchronous workers.
- **Successful Queries**: It is returned directly in the JSON body of `POST /v1/query` and `POST /v1/documents`.
- **Failed Queries**: The API's `ErrorEnvelope` (returned on 500, 502, 503 status codes) always includes the `correlation_id`.
- **HTTP Headers**: It is injected by the `CorrelationIdMiddleware` and echoed in the HTTP response headers.

### 2. Error Responses & Meanings
When an API call fails, it returns an `ErrorEnvelope` containing an HTTP status code and a specific machine-readable `code`. The following errors are defined in the system:
- `VALIDATION_ERROR` (422): Request schema validation failed (e.g., missing required fields).
- `AUTH_INVALID_KEY` (401): Incorrect or missing admin API key.
- `RATE_LIMITED` (429): Endpoint rate limit exceeded. You may receive a `retry_after` parameter in the details.
- `LLM_SCHEMA_VIOLATION` (502): The LLM repeatedly failed to generate the required Pydantic schema, even after the orchestrator's repair loop attempts.
- `LLM_PROVIDER_EXHAUSTED` (503): Every LLM provider in the gateway's fallback chain failed or hit rate limits for the requested role.
- `RETRIEVAL_BACKEND_UNAVAILABLE` (503): The Qdrant vector store is unreachable.
- `GRAPH_BACKEND_UNAVAILABLE` (503): The Neo4j knowledge graph is unreachable.
- `BUDGET_EXCEEDED` (200): A query component hit a safety budget cap. The response returns HTTP 200 because the orchestrator caught the failure and returned a degraded but safe response.
- `CONFLICT` (409): Idempotency key or document conflict during ingestion.
- `JOB_TIMEOUT` (504): A background ARQ job was cancelled because it exceeded the worker timeout.
- `INTERNAL_ERROR` (500): Unhandled system exception.

### 3. What to Look For After a Query
A successful `QueryResponse` includes the `answer`, exact `citations`, the `route` taken (vector, graph, or hybrid), and the `spent` metrics (token costs). 
**Note:** If the `degraded` array is not empty in the response, it means the LangGraph orchestrator encountered partial failures (e.g., a fallback triggered, or a node exhausted its retry attempts) but managed to recover enough to return a valid answer.

### 4. How to Investigate Failures (The `trail` command)
For immediate, terminal-based debugging, use the `trail` command:
```bash
uv run graphrag trail <correlation_id>
```
- **What it does**: It queries **Loki** (for logs) and **Tempo** (for spans) using the provided correlation ID. It interleaves both sources into a single, time-ordered markdown timeline.
- **Why it's useful**: It isolates and dumps the raw JSON attributes for any event marked as `ERROR` or `CRITICAL` at the bottom of the file (under `## Failures`). You do not need to open a browser; you can immediately paste this markdown bundle into a bug report or read it directly in your IDE.

### 5. Investigation via the Observability UIs
While the CLI provides immediate terminal feedback, the system provides two specialized UIs for deep, visual investigation of a trace.

**Arize Phoenix (Port 6006)** is your primary tool for debugging the LLM interactions and the LangGraph orchestration. The OpenTelemetry collector pushes the entire, unfiltered trace to Phoenix. It automatically detects `openinference` spans and renders them with rich LLM UI elements, allowing you to see the exact prompt injected, the raw string output, and token metrics. Because normal application spans are also included, you can view the complete call tree (from the HTTP request down to the LiteLLM generation) in one cohesive trace. 

**Grafana LGTM (Port 3000)** complements Phoenix by providing deep infrastructure tracing and log aggregation. You should use Grafana when you need to debug database timeouts, worker queues, or raw text logs.
- **Tracing (Tempo)**: Go to Explore > Tempo and use the TraceQL query `{ .app.correlation_id = "<cid>" }`. This is ideal for seeing exactly how long infrastructure calls like Qdrant's `hybrid_search` took or investigating Redis locking behavior.
- **Logs (Loki)**: Go to Explore > Loki and use the LogQL query `{service_name="graphrag"} | json | correlation_id="<cid>"`. Use Loki to view raw worker crash stack traces or background jobs that weren't caught as explicit span errors.
