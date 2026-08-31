from __future__ import annotations

import httpx
import pytest

from agent_api.app import create_app
from agent_api.ui import RESEARCH_UI_HTML, render_research_ui


class _FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


@pytest.mark.asyncio
async def test_reviewer_ui_is_packaged_safe_and_outside_openapi(tmp_path) -> None:
    app = create_app(
        database_path=tmp_path / "agent-api.sqlite3",
        pepper_provider=_FixedPepper(),
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "Research Agent" in response.text
    assert 'fetch("/health/ready")' in response.text
    assert 'api("/v1/sessions"' in response.text
    assert '"/runs"' in response.text
    assert 'api("/v1/runs/"' in response.text
    assert "run.memory_used === true" in response.text
    assert "Reviewed memory was used during this run." in response.text
    assert "textContent" in response.text
    for forbidden in ("innerHTML", "localStorage", "sessionStorage", "location.hash"):
        assert forbidden not in response.text
    assert "/" not in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_reviewer_ui_fills_in_a_supplied_local_review_key(tmp_path) -> None:
    app = create_app(
        database_path=tmp_path / "agent-api.sqlite3",
        pepper_provider=_FixedPepper(),
        ui_prefilled_api_key='local-review-key-"quoted"',
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        response = await client.get("/")

    assert response.status_code == 200
    assert 'value="local-review-key-&quot;quoted&quot;"' in response.text
    assert "filled in by the process that started the API" in response.text
    assert "__API_KEY" not in response.text


def test_render_research_ui_leaves_the_key_field_empty_without_a_key() -> None:
    # The deployed page must never carry a key, and create_app renders this
    # unprefilled variant whenever the environment is a production one.
    for page in (render_research_ui(), render_research_ui(None), RESEARCH_UI_HTML):
        assert "never stored by the page" in page
        assert "value=" not in page.split('id="api-key"')[1].split(">")[0]
        assert "__API_KEY" not in page
