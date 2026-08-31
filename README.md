# Hybrid GraphRAG

Dense vector search plus knowledge-graph traversal, with async ingestion, a self-hosted LLM
gateway, a self-correcting query pipeline, and full OpenTelemetry observability.

## Running the tests

    make test        # unit
    make test-int    # integration (quota-consuming tests excluded)
    make test-llm    # the three tests that make real provider calls

`make test-llm` calls live provider APIs on free tiers. **A 429 from it is a quota result, not a
defect** — the Groq free tier is 8000 TPM shared across the organisation. Re-run it later rather
than treating it as a failure.

## Corpus

The evaluation corpus is post-training-cutoff material, so a correct answer demonstrates
retrieval rather than recall.