"""`services/orchestration/prompts.py` rendering tests. See BLUEPRINT §6.4."""

from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from graphrag.services.orchestration import prompts

_SIX_TEMPLATES = (
    "route_plan.j2",
    "grade_context.j2",
    "rewrite_query.j2",
    "generate.j2",
    "verify_grounded.j2",
    "extract_entities.j2",
)

_CHUNKS = [{"chunk_id": "c1", "text": "Acme acquired Widgets Inc. in 2024."}]


def test_all_six_templates_exist_and_are_versioned() -> None:
    for name in _SIX_TEMPLATES:
        version = prompts.template_version(name)
        assert isinstance(version, int)
        assert version >= 1


def test_render_raises_on_undefined_variable() -> None:
    with pytest.raises(UndefinedError):
        prompts.render("route_plan.j2")  # missing required `question`


def test_render_raises_on_unknown_template() -> None:
    with pytest.raises(ValueError, match="unknown prompt template"):
        prompts.render("does_not_exist.j2", question="hi")


@pytest.mark.parametrize("name", ["grade_context.j2", "generate.j2", "verify_grounded.j2"])
def test_prompts_wrap_documents_in_untrusted_delimiters(name: str) -> None:
    rendered = prompts.render(
        name,
        question="What happened to Widgets Inc.?",
        query="What happened to Widgets Inc.?",
        answer="Acme acquired Widgets Inc.",
        chunks=_CHUNKS,
    )
    assert '<document id="c1">' in rendered
    assert "</document>" in rendered
    assert "untrusted" in rendered.lower()
    assert "Acme acquired Widgets Inc. in 2024." in rendered


def test_extract_entities_wraps_document_in_untrusted_delimiters() -> None:
    rendered = prompts.render(
        "extract_entities.j2", chunk_id="c1", text="Acme acquired Widgets Inc. in 2024."
    )
    assert '<document id="c1">' in rendered
    assert "</document>" in rendered
    assert "untrusted" in rendered.lower()


def test_route_plan_renders_with_question() -> None:
    rendered = prompts.render("route_plan.j2", question="Who acquired Widgets Inc.?")
    assert "Who acquired Widgets Inc.?" in rendered


def test_rewrite_query_renders() -> None:
    rendered = prompts.render(
        "rewrite_query.j2", question="Who bought Widgets?", active_query="Widgets"
    )
    assert "Who bought Widgets?" in rendered
    assert "Widgets" in rendered
