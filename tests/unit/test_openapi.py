import json
from pathlib import Path

from graphrag.apps.api.main import create_app


def test_openapi_unchanged(settings):
    """
    Contract test ensuring that the committed OpenAPI documentation exactly matches
    the generated FastAPI schema.
    """
    root_dir = Path(__file__).parent.parent.parent
    openapi_path = root_dir / "docs" / "openapi.json"
    assert openapi_path.exists(), f"{openapi_path} does not exist."

    with open(openapi_path) as f:
        committed_schema = json.load(f)

    app = create_app()
    generated_schema = app.openapi()

    assert committed_schema == generated_schema, (
        "Generated OpenAPI schema does not match committed docs/openapi.json! "
        "If you changed the API, regenerate the docs."
    )
