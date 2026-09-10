# System Design & Architecture

This document details some of the specific architectural decisions and engineering trade-offs that make this system robust for a production environment.

## 1. Content-Addressed Chunking (O(1) Deduplication)

Instead of relying on a costly vector search to identify duplicate chunks during ingestion, chunks are **content-addressed**.
- A chunk's unique ID is deterministically generated via `uuid.UUID(bytes=sha256(normalize(text)).digest()[:16], version=5)`.
- If the exact same text appears in multiple documents (e.g., standard disclaimers, repeated boilerplates), they yield the exact same UUID.
- Qdrant's payload indexes allow us to simply append the new document's source reference to the existing chunk's payload, giving us **multi-source retention** natively. Deduplication is handled as an identity property, avoiding O(N^2) similarity checks.

## 2. Reducer-backed State Machine

The LangGraph orchestrator avoids the default "last-write-wins" semantics, which are unsafe in parallel execution. 
- The query pipeline splits into parallel branches (e.g., retrieving from Vector and Graph stores simultaneously).
- The `QueryState` utilizes **reducers** (custom monoid aggregators) to safely accumulate state.
- **Failures**: Errors are appended (`operator.add`) rather than overwritten. This creates an uncorrupted, append-only audit trail that triggers the repair loop correctly, regardless of which parallel node failed.
- **Budgets/Spend**: Tracks accumulated spend rather than remaining limits. Because parallel branches execute simultaneously, accumulating "spend" guarantees that no quota leaks through race conditions.

## 3. Two-Tier Reciprocal Rank Fusion (RRF)

Fusion of retrieval results happens at two distinct layers:
1. **Intra-Store (Server-Side)**: Within Qdrant, we use the Universal Query API to issue dense and sparse (BM25) queries simultaneously via `prefetch`. The fusion happens *server-side* in Rust, saving the overhead of returning huge intermediate result sets over the network.
2. **Cross-Store (Client-Side)**: The results from Qdrant and the Cypher traversals from Neo4j are fundamentally incompatible in score (Cosine similarity bounds vs. unbounded BM25 vs. Graph hop weights). These are fused in Python using Reciprocal Rank Fusion, avoiding brittle score-normalization hacks.

## 4. LLM Role Segmentation & Gateway Fallbacks

Rate limits are strictly enforced on multiple axes (RPM, TPM, RPD) by a central LiteLLM gateway. The architecture segregates LLM calls by **role**, tailoring models to the specific constraint they face:
- **`router` & `grader`**: High request counts, tiny output. Routed to low-latency providers (e.g., Groq).
- **`bulk`**: Heavy token usage (e.g., whole-document extraction) but latency-tolerant. Routed to providers with high token limits and batched heavily to avoid RPM ceilings.
- **`synth`**: Quality-critical answer generation.

Each role is equipped with explicit **fallback chains**. A 429 rate limit triggers an automatic failover to alternative providers (e.g., Gemini to OpenRouter) inside the gateway, transparently protecting the application from transient API exhaustions.

## 5. Correlated Observability across Async Boundaries

Traditional W3C context propagation breaks when crossing from a synchronous API to a background queue.
- The FastAPI layer injects an OTel `traceparent` and ULID `correlation_id` into the payload of the ARQ job envelope itself.
- When the worker picks up the job, it extracts this context and resumes the trace.
- This creates a **single, unbroken Tempo trace** traversing the HTTP request, Redis queue, worker process, Qdrant lookup, and LiteLLM generation.

## 6. Asynchronous Ingestion State Machine

The document ingestion pipeline is driven by an asynchronous state machine designed to track the lifecycle of a document from raw upload to fully indexed and resolved entities in the knowledge graph. The states are defined as follows:
- **`PENDING`**: The document has been registered in the system ledger and is waiting in the queue.
- **`PARSING`**: The document is actively being parsed, chunked, and normalized.
- **`EMBEDDING`**: The chunks are being embedded (dense and sparse) and pushed to the vector store.
- **`EXTRACTING`**: LLMs are processing the chunks to extract entities and relationships.
- **`RESOLVING`**: Extracted entities are undergoing blocking, scoring, and union-find clustering to deduplicate and establish canonical identities.
- **`INDEXED`**: The document and its resolved entities have been successfully committed to the knowledge graph and vector store.
- **`FAILED`**: The ingestion process encountered a terminal error at some stage.
