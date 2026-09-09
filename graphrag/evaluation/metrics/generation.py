"""Generation metrics — RAGAS + custom LangChain wrapper. See BLUEPRINT §8.

``GenerationJudge`` wraps RAGAS class-based metrics (Faithfulness, ResponseRelevancy,
LLMContextPrecisionWithoutReference, NonLLMContextRecall) and routes all LLM traffic
through our ``LiteLLMClient`` port via a custom ``BaseChatModel`` subclass, so every call
goes through the gateway and spend is tracked.

The wrapper records ``model_served`` from each upstream response so the eval report can
flag when the gateway silently falls back to a different model.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import types
from typing import TYPE_CHECKING, Any

# --- RAGAS 0.4.3 Workaround ---
# RAGAS 0.4.3 contains a hardcoded import for ChatVertexAI from a module that no longer
# exists in newer langchain-community versions. We inject a mock to satisfy the import.
if "langchain_community.chat_models.vertexai" not in sys.modules:
    _v = types.ModuleType("langchain_community.chat_models.vertexai")
    _v.ChatVertexAI = type("ChatVertexAI", (), {})
    sys.modules["langchain_community.chat_models.vertexai"] = _v
# ------------------------------

import warnings

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from ragas import EvaluationDataset, evaluate
from ragas.dataset_schema import SingleTurnSample
from ragas.run_config import RunConfig

from graphrag.config.settings import get_settings
from graphrag.core.errors import LLMProviderExhausted, RateLimited

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithoutReference,
        NonLLMContextRecall,
        ResponseRelevancy,
    )

if TYPE_CHECKING:
    from graphrag.core.ports import Embedder, LLMClient

_log = logging.getLogger(__name__)


class TrackedChatModel(BaseChatModel):
    """A LangChain ``BaseChatModel`` that delegates to our ``LiteLLMClient`` port.

    This ensures all judge LLM calls route through the LiteLLM gateway, tracked under the
    ``judge`` role. It also records ``model_served`` from each response so mixed-model
    runs can be detected.
    """

    model_name: str = Field(default="judge", description="LLM role alias used for display")

    _inner: LLMClient
    _role: str
    _models_served: list[str]
    _lock: threading.Lock

    def __init__(self, *, inner: LLMClient, role: str = "judge", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._inner = inner
        self._role = role
        self._models_served = []
        self._lock = threading.Lock()

    @property
    def _llm_type(self) -> str:
        return "tracked-litellm-judge"

    @property
    def models_served(self) -> list[str]:
        with self._lock:
            return list(self._models_served)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Synchronous generation — runs the async client in an event loop."""
        loop = _get_or_create_event_loop()
        return loop.run_until_complete(
            self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        converted: list[dict[str, str]] = []
        for msg in messages:
            role = "user"
            if msg.type == "system":
                role = "system"
            elif msg.type in ("ai", "assistant"):
                role = "assistant"
            converted.append({"role": role, "content": str(msg.content)})

        from pydantic import BaseModel

        class RagasResponse(BaseModel):
            text: str

        for attempt in range(5):
            try:
                settings = get_settings()
                max_repairs = (
                    settings.orchestration.max_structured_output_repairs if settings else 2
                )
                res = await self._inner.structured(
                    role=self._role,
                    messages=converted,
                    schema=RagasResponse,
                    max_repairs=max_repairs,
                )
                break
            except (RateLimited, LLMProviderExhausted) as exc:
                if attempt == 4:
                    raise
                if isinstance(exc, RateLimited) and exc.retry_after is not None:
                    sleep_time = exc.retry_after
                else:
                    sleep_time = [15.0, 30.0, 60.0, 120.0][attempt]
                if sleep_time > 300:
                    raise LLMProviderExhausted(
                        f"Rate limit sleep duration ({sleep_time}s) exceeds maximum allowed wait time. Aborting to prevent daily-limit hang."
                    ) from exc
                _log.info("Rate limited. Sleeping for %s seconds...", sleep_time)
                await asyncio.sleep(sleep_time)

        text = res.value.text

        with self._lock:
            self._models_served.append(res.model_served)

        generation = ChatGeneration(message=AIMessage(content=text))
        return ChatResult(generations=[generation])


class TrackedEmbeddings(Embeddings):
    """Wraps our internal Embedder protocol for RAGAS."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        loop = _get_or_create_event_loop()
        return loop.run_until_complete(self.aembed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        loop = _get_or_create_event_loop()
        return loop.run_until_complete(self.aembed_query(text))

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._inner.embed_dense(texts)

    async def aembed_query(self, text: str) -> list[float]:
        dense = await self._inner.embed_dense([text], is_query=True)
        return dense[0]


def _get_or_create_event_loop() -> asyncio.AbstractEventLoop:
    """Get the running event loop or create one if none exists."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


class GenerationJudge:
    """RAGAS-backed generation quality judge. See BLUEPRINT §8.

    Contract:
        - Uses ``llm.roles['judge']``, validated at eval time to differ from ``synth``.
        - Judge temperature 0 (enforced in config via role spec).
        - Tracks ``model_served`` per call for mixed-judge detection.
        - ``NonLLMContextRecall`` makes zero LLM calls.
    """

    def __init__(self, llm_client: LLMClient, embedder: Embedder) -> None:
        self._llm_client = llm_client
        self._tracked_model = TrackedChatModel(inner=llm_client, role="judge")
        self._tracked_embeddings = TrackedEmbeddings(inner=embedder)

    @property
    def models_served(self) -> list[str]:
        return self._tracked_model.models_served

    async def evaluate_items(
        self,
        items: list[dict[str, Any]],
        *,
        include_llm_metrics: bool = True,
    ) -> dict[str, float]:
        """Run RAGAS metrics over a list of evaluation items.

        Each item dict must contain:
            - ``user_input``: the question
            - ``response``: the generated answer text
            - ``retrieved_contexts``: list of retrieved chunk texts
            - ``reference_contexts``: list of gold chunk texts (for NonLLMContextRecall)
            - ``reference``: the gold answer (optional, for LLMContextRecall)

        Returns a dict of metric_name -> mean score.
        """
        samples = []
        for item in items:
            sample = SingleTurnSample(
                user_input=item["user_input"],
                response=item.get("response", ""),
                retrieved_contexts=item.get("retrieved_contexts", []),
                reference_contexts=item.get("reference_contexts", []),
                reference=item.get("reference", ""),
            )
            samples.append(sample)

        dataset = EvaluationDataset(samples=samples)

        metrics_list: list[Any] = [NonLLMContextRecall()]
        if include_llm_metrics:
            metrics_list.extend(
                [
                    Faithfulness(llm=self._tracked_model),
                    ResponseRelevancy(llm=self._tracked_model, embeddings=self._tracked_embeddings),
                    LLMContextPrecisionWithoutReference(llm=self._tracked_model),
                ]
            )

        run_config = RunConfig(max_workers=1, max_retries=3)
        result = evaluate(
            dataset=dataset,
            metrics=metrics_list,
            llm=self._tracked_model,
            embeddings=self._tracked_embeddings,
            run_config=run_config,
        )

        scores: dict[str, float] = {}
        result_df = result.to_pandas()
        for col in result_df.columns:
            if col not in (
                "user_input",
                "response",
                "retrieved_contexts",
                "reference_contexts",
                "reference",
            ):
                values = result_df[col].dropna()
                if len(values) > 0:
                    scores[col] = float(values.mean())

        return scores
