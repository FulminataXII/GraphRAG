"""Static checks on `litellm/config.yaml`, `docker-compose.yml`, and `Makefile`'s BO-06 wiring.

No live LiteLLM process involved — these parse the checked-in files directly, the same style as
`tests/unit/test_repo_hygiene.py`'s `test_images_pinned_by_digest`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from graphrag.config.schema import SecretsSection
from graphrag.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Provider keys that must reach ONLY the litellm container — see .env.example's header comment
# and LiteLLMClient's contract (BLUEPRINT §5.6): "Provider keys must NOT be present in this
# process's environment."
_PROVIDER_KEY_VARS = frozenset(
    {
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GROQ_API_KEY_1",
        "GROQ_API_KEY_2",
        "OPENROUTER_API_KEY_1",
    }
)
_APP_SERVICES = ("api", "worker", "projection-worker")


def _compose_services() -> dict[str, dict]:
    doc = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    return doc["services"]


def _litellm_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "litellm" / "config.yaml").read_text(encoding="utf-8"))


def test_no_provider_key_in_app_env(settings: Settings) -> None:
    services = _compose_services()
    for name in _APP_SERVICES:
        service = services[name]
        assert "env_file" not in service, (
            f"{name} uses env_file, which would blanket-inject the litellm-only provider keys "
            "from .env into this process's environment"
        )
        env = service.get("environment") or {}
        leaked = _PROVIDER_KEY_VARS & env.keys()
        assert not leaked, f"{name} exposes provider key(s) {leaked} directly"

    # And the gateway app-side never holds a provider key either — only the virtual key.
    assert not (_PROVIDER_KEY_VARS & set(SecretsSection.model_fields))


def test_litellm_aliases_match_config_roles(settings: Settings) -> None:
    """Every `llm.roles[*].model` resolves to a `model_name` in `litellm/config.yaml`."""
    model_names = {entry["model_name"] for entry in _litellm_config()["model_list"]}
    for role, spec in settings.llm.roles.items():
        assert spec.model in model_names, (
            f"llm.roles['{role}'].model={spec.model!r} has no matching model_name in "
            "litellm/config.yaml"
        )


def test_litellm_config_has_key_rotation() -> None:
    """At least one alias has 2+ deployments with distinct api_key envs (key rotation)."""
    by_alias: dict[str, list[str]] = {}
    for entry in _litellm_config()["model_list"]:
        by_alias.setdefault(entry["model_name"], []).append(entry["litellm_params"]["api_key"])

    rotated = [alias for alias, keys in by_alias.items() if len(set(keys)) >= 2]
    assert rotated, "no alias in litellm/config.yaml has 2+ distinct api_key deployments"


def test_litellm_config_has_fallbacks_and_otel() -> None:
    litellm_settings = _litellm_config()["litellm_settings"]
    assert litellm_settings.get("fallbacks")
    assert "otel" in litellm_settings.get("callbacks", [])
    assert litellm_settings.get("num_retries", 0) > 0


def _makefile_recipe(target: str) -> str:
    """The (single-line) shell command under `<target>:` in the repo's Makefile."""
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() == f"{target}:":
            return lines[i + 1].strip()
    raise AssertionError(f"no {target!r} target found in Makefile")


def test_make_test_int_excludes_llm_quota() -> None:
    """BUILD_ORDER BO-06: 'Mark quota-consuming tests `llm_quota` and exclude them from the
    default `make test-int`' — these three make real provider calls on a free tier with an 8000
    TPM ceiling; left in the default integration run they drain the day's budget and unrelated
    later tests then fail for reasons that have nothing to do with their own code.

    Checks BOTH claims, not just one: that the Makefile recipe says `not llm_quota` (the marker
    is *registered* for exclusion), AND that pytest's own collection under that exact recipe
    actually drops every llm_quota-marked test while still collecting other integration tests
    (the exclusion is *enforced*) — a typo'd or reverted `-m` expression would pass the first
    check and fail the second.
    """
    recipe = _makefile_recipe("test-int")
    assert "not llm_quota" in recipe, f"test-int recipe does not exclude llm_quota: {recipe!r}"

    marker_expr = recipe.split("-m", 1)[1].strip().strip("'\"")
    result = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q", "-m", marker_expr, "tests/integration"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    collected = result.stdout

    quota_tests = ["test_key_rotation_spreads_load", "test_gateway_fallback", "test_cost_tracked"]
    leaked = [name for name in quota_tests if name in collected]
    assert not leaked, (
        f"test-int's marker expression {marker_expr!r} still collects llm_quota test(s) "
        f"{leaked}:\n{collected}"
    )
    assert "test_stack_healthy" in collected, (
        f"test-int's marker expression {marker_expr!r} collected no other integration tests "
        f"either — this proves nothing:\n{collected}"
    )
