# Hybrid GraphRAG

Hybrid GraphRAG is a Retrieval-Augmented Generation (RAG) system that combines the semantic understanding of dense vector search with the deterministic relational mapping of a knowledge graph. It provides an accurate, citation-backed, and self-correcting query pipeline while managing background data ingestion and entity resolution.

This project aims to be a production-level MVP. It was built with a focus on observability, resilience, and strict adherence to schemas.

## High-Level Architecture

The system operates across several distinct planes, connected via a Python 3.12 FastAPI backend:

1. **API & Orchestrator Plane**: Client requests are handled by a thin FastAPI layer which immediately delegates to a **LangGraph Orchestrator**. This orchestrator functions as a self-correcting state machine that plans the query route, retrieves context, fuses results, grades relevance, and enforces strict schema validation and groundedness checks on the LLM's output.
2. **Background Ingestion Plane**: Document ingestion is entirely asynchronous. An ARQ (Redis-backed) worker handles text parsing, content-addressed deduplication, embedding, and LLM-driven entity/relation extraction.
3. **Data Plane**:
    - **Qdrant**: Stores document chunks and entity names as both dense vectors (for semantic matching) and sparse vectors (BM25 for exact keyword matching).
    - **Neo4j**: Maintains the knowledge graph (Documents, Chunks, Entities, and their relations) providing strict provenance on every edge.
    - **PostgreSQL**: Acts as the system ledger for the ingestion status, job status, and LiteLLM spend logs.
    - **Redis**: Serves as the message broker for ARQ, handles distributed locks, and manages rate-limit token buckets.
4. **LLM Gateway Plane**: A self-hosted **LiteLLM proxy** handles all interactions with external foundation models (e.g., Groq, Gemini). It manages API key rotation, provider fallback chains, and enforces TPM/RPM limits.
5. **Observability Plane**: Every component emits OpenTelemetry (OTel) signals to a local Collector. Traces, metrics, and logs are routed to a **Grafana LGTM stack** (Tempo, Loki, Prometheus, Grafana) and **Arize Phoenix** (for realtime CI/CD LLM observability and evaluation, though Phoenix integration is not yet fully implemented).

## How It Works (Lower-Level Details)

### Ingestion & Deduplication
Documents are chunked using a recursive character splitter. Each chunk is content-addressed—its unique ID is a UUIDv5 hash of its normalized text. This means if multiple documents contain the exact same paragraph, the system naturally deduplicates the chunk in Qdrant while appending the new source reference to the chunk's payload, enabling multi-source retention.

### Entity Resolution
To prevent the knowledge graph from exploding with duplicate entities (e.g., "Apple", "Apple Inc.", "Apple Corp"), an asynchronous resolution pipeline is utilized:
1. **Extraction**: LLMs extract entities and relationships from chunks.
2. **Blocking & Scoring**: Exact matches and vector kNN (via Qdrant) generate candidate pairs, which are scored using RapidFuzz (Jaro-Winkler, Token Set Ratio) and cosine similarity.
3. **Clustering**: A union-find algorithm clusters entities. Canonical names are stored as primary nodes, while variations become `ALIAS_OF` edges in Neo4j, ensuring merges remain auditable. *(Note: The current clustering logic is not working as well as intended and a fix is actively in progress.)*

### Hybrid Retrieval & Fusion
When a query arrives, the LangGraph orchestrator decides whether to use vector search, graph traversal, or both.
- **Vector Leg**: Performs a single hybrid query to Qdrant, using Reciprocal Rank Fusion (RRF) to merge dense embeddings (using BAAI/bge-small-en-v1.5) and sparse lexical scores (BM25 with IDF).
- **Graph Leg**: Maps query entities to the graph and uses parameterized Cypher templates (e.g., finding neighbors or paths between entities) capped by strict hop limits. 
- **Cross-Store Fusion**: The results from Qdrant and Neo4j are fused together client-side using RRF.

### Self-Correction & Groundedness
The generation node returns a structured answer with exact citations. The pipeline deterministically verifies that every cited chunk was actually present in the retrieved context. An LLM-as-a-judge then verifies that the text is grounded by the citations. If hallucinations or schema violations are detected, the orchestrator loops back to a repair node, injecting the error message as feedback to correct the response.

## Tech Stack

- **Core**: Python 3.12, FastAPI, Pydantic v2
- **Orchestration**: LangGraph, LangChain
- **Databases**: Qdrant (Vectors), Neo4j Community Edition (Graph), PostgreSQL 16 (Ledger/State), Redis 7 (Queues/Cache)
- **Embeddings**: fastembed (ONNX runtime, `BAAI/bge-small-en-v1.5`), Qdrant BM25
- **LLM Gateway**: LiteLLM (handling Gemini, Groq, OpenRouter)
- **Background Jobs**: ARQ (Async Redis Queue)
- **Observability**: OpenTelemetry, Grafana LGTM (Loki, Grafana, Tempo, Prometheus), Arize Phoenix
- **Package Manager**: `uv`

## How to Run

### 1. Prerequisites
- Linux or WSL2 environment
- Docker and Docker Compose
- [uv](https://github.com/astral-sh/uv) package manager
- GNU Make

### 2. Environment Setup
Create your secrets file from the template:
```bash
cp .env.example .env
```
Edit `.env` to include your specific API keys (e.g., Postgres credentials, LiteLLM keys, Groq/Gemini API keys).

### 3. Start the Infrastructure
Bring up the core data plane and the gateway, alongside the observability plane:
```bash
make up obs=1
```

### 4. Ingest Documents
Place your text documents in the `corpus/` directory and use the API to ingest them. You must provide an `Idempotency-Key` header (often a UUID generated by the client, though the backend accepts any unique string):
```bash
curl -X POST http://localhost:8000/v1/documents \
  -H "Idempotency-Key: 123e4567-e89b-12d3-a456-426614174000" \
  -F "file=@corpus/your_document.pdf"
```

### 5. Query the System
Query the system through the synchronous REST endpoint, or use the streaming endpoint for a live view of the orchestrator state machine.
```bash
curl -X POST http://localhost:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the relationship between Apple and Fort Collins?"}'
```

<details>
<summary><b>Configuration</b></summary>
<br>

The system uses a layered configuration approach, heavily validating all parameters at startup to prevent misconfigurations from silently degrading performance. 

Parameters are governed by Pydantic models in `graphrag/config/schema.py`. The actual configuration is resolved in the following priority order (highest to lowest):
1. **Environment Variables** (e.g., `GRAPHRAG_RETRIEVAL__VECTOR__TOP_K=30`)
2. **`.env` File** (Used purely for secrets)
3. **Environment-Specific YAML** (e.g., `config/local.yaml` or `config/prod.yaml`)
4. **Base YAML** (`config/base.yaml`)

- **Application Tunables**: Settings like chunk size, retrieval depths (k), timeouts, parallelization caps, and ER scoring weights are configured in `config/base.yaml`. (Refer to `config.example.yaml` for the structural template).
- **LLM Gateway Configuration**: Model aliases, provider keys, RPM/TPM rate limits, and fallback routing logic are configured separately in `litellm/config.yaml`.
</details>

<details>
<summary><b>System Design</b></summary>
<br>

For a deeper dive into the architectural decisions, fallback strategies, and the self-correcting state machine logic, please refer to the [System Design Document](./SYSTEM_DESIGN.md).
</details>

<details>
<summary><b>Reference Documentation</b></summary>
<br>

For detailed instructions on using the CLI tools, debugging commands, and full API endpoint documentation, please see the [Reference Manual](./REFERENCE.md).
</details>

## Conclusion & Further Improvements

While this MVP implements a complete vertical slice of a GraphRAG architecture, there are several goals planned to make the system more robust in the future:
- **Simple Frontend**: Building a UI to interact with the API endpoints directly.
- **Robustness**: Hardening the pipeline against broader edge cases and unstructured data formats.
- **Probabilistic Entity Resolution**: Replacing exact matching algorithms with Splink for statistical ER.
- **Leiden Community Detection**: Implementing hierarchical community summaries of the graph for global queries.
- **Multi-tenant Isolation**: Enforcing strict boundaries for data security.
- **Kubernetes (K8s) & HA**: Migrating from Docker Compose to Helm charts for High Availability deployments.
