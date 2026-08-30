"""Jinja2 prompt rendering. See BLUEPRINT §6.4.

Templates required by BO-06, one per LLM role usage, live in `prompts/*.j2` next to this module:
    route_plan.j2       grade_context.j2   rewrite_query.j2
    generate.j2         verify_grounded.j2 extract_entities.j2
(`adjudicate_entities.j2` is T1 — the ER gray band — and is out of scope for this build order.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

_TEMPLATES_DIR = Path(__file__).parent / "prompts"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)


def render(template_name: str, **vars: Any) -> str:
    """Render `template_name` (e.g. "generate.j2") from `prompts/*.j2`.

    Contract (BLUEPRINT §6.4):
        - Retrieved chunk text is ALWAYS wrapped in <document id="..."> ... </document> and
          preceded by an instruction that document content is untrusted data containing no
          instructions. This is defence-in-depth only — the real guard is schema validation
          plus deterministic citation checking. Enforced in the templates themselves.
        - Templates are versioned (`{# version: N #}` on line 1); the version goes on the span,
          so an eval regression can be traced to a prompt change rather than a model change.
        - Raises on an unknown template name.
        - Raises on any undefined variable (Jinja2 StrictUndefined). A silently-empty
          `{{ context }}` produces a confident ungrounded answer, which is the exact failure this
          system exists to prevent.
    """
    try:
        template = _env.get_template(template_name)
    except TemplateNotFound as exc:
        raise ValueError(f"unknown prompt template: {template_name!r}") from exc
    return template.render(**vars)


def template_version(template_name: str) -> int:
    """Parse the `{# version: N #}` header required on line 1 of every template.

    Raises ValueError if the template is missing the header or it isn't the first line.
    """
    path = _TEMPLATES_DIR / template_name
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError) as exc:
        raise ValueError(f"unknown prompt template: {template_name!r}") from exc
    stripped = first_line.strip()
    if not (stripped.startswith("{#") and stripped.endswith("#}")):
        raise ValueError(f"{template_name} is missing its `{{# version: N #}}` header on line 1")
    inner = stripped[2:-2].strip()
    prefix = "version:"
    if not inner.startswith(prefix):
        raise ValueError(f"{template_name} is missing its `{{# version: N #}}` header on line 1")
    return int(inner[len(prefix) :].strip())
