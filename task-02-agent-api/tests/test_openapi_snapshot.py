"""Freeze the declarative HTTP surface without shipping operational routes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from openapi_contract import ERROR_RESPONSES, build_contract_app

EXPECTED_METHODS = {
    "/health/live": {"get"},
    "/health/ready": {"get"},
    "/v1/sessions": {"get", "post"},
    "/v1/sessions/{session_id}": {"delete", "get"},
    "/v1/sessions/{session_id}/memory": {"delete"},
    "/v1/sessions/{session_id}/runs": {"post"},
    "/v1/runs/{run_id}": {"get"},
    "/v1/runs/{run_id}/cancel": {"post"},
    "/v1/runs/{run_id}/events": {"get"},
}
SNAPSHOT = Path(__file__).parent / "snapshots" / "openapi_contract.json"


def openapi() -> dict[str, Any]:
    return build_contract_app().openapi()


def parameters(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {parameter["name"]: parameter for parameter in operation["parameters"]}


def test_versioned_http_surface_is_exact() -> None:
    document = openapi()
    actual = {
        path: set(path_item).intersection({"get", "post", "put", "delete", "patch"})
        for path, path_item in document["paths"].items()
    }

    assert actual == EXPECTED_METHODS


def test_required_retry_and_resume_headers_are_declared() -> None:
    document = openapi()
    operations = [
        path_item[method]
        for path_item in document["paths"].values()
        for method in path_item
        if method in {"get", "post", "put", "delete", "patch"}
    ]
    for operation in operations:
        correlation = parameters(operation)["X-Correlation-ID"]
        assert correlation["in"] == "header"
        assert correlation["required"] is False

    submit = document["paths"]["/v1/sessions/{session_id}/runs"]["post"]
    idempotency = parameters(submit)["Idempotency-Key"]
    assert idempotency["in"] == "header"
    assert idempotency["required"] is True

    stream = document["paths"]["/v1/runs/{run_id}/events"]["get"]
    resume = parameters(stream)["Last-Event-ID"]
    assert resume["in"] == "header"
    assert resume["required"] is False


def test_pagination_and_sse_transport_are_bounded() -> None:
    document = openapi()
    listing = document["paths"]["/v1/sessions"]["get"]
    listing_parameters = parameters(listing)

    assert listing_parameters["limit"]["schema"] == {
        "default": 50,
        "maximum": 100,
        "minimum": 1,
        "title": "Limit",
        "type": "integer",
    }
    assert listing_parameters["cursor"]["required"] is False

    stream = document["paths"]["/v1/runs/{run_id}/events"]["get"]
    assert set(stream["responses"]["200"]["content"]) == {"text/event-stream"}


def test_every_declared_error_uses_the_safe_envelope() -> None:
    document = openapi()
    for path_item in document["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            for status in set(map(str, ERROR_RESPONSES)).intersection(
                operation["responses"]
            ):
                schema = operation["responses"][status]["content"]["application/json"][
                    "schema"
                ]
                assert schema == {"$ref": "#/components/schemas/ErrorEnvelope"}


def test_public_schema_has_no_forbidden_information_channels() -> None:
    document = openapi()
    forbidden = {
        "tenant_id",
        "prompt",
        "system_prompt",
        "chain_of_thought",
        "reasoning",
        "raw_page",
        "raw_content",
        "exception",
        "traceback",
        "stack_trace",
        "internal_detail",
    }
    public_properties = {
        property_name
        for schema in document["components"]["schemas"].values()
        for property_name in schema.get("properties", {})
    }

    assert forbidden.isdisjoint(public_properties)
    assert "ScopedAnswer" in document["components"]["schemas"]
    assert "Citation" in document["components"]["schemas"]


def test_openapi_contract_matches_deterministic_snapshot() -> None:
    document = openapi()
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    summary = {
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "paths": {path: sorted(methods) for path, methods in EXPECTED_METHODS.items()},
        "schemas": sorted(document["components"]["schemas"]),
    }

    assert summary == json.loads(SNAPSHOT.read_text())
