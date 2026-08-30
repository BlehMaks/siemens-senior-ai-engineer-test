from __future__ import annotations

import httpx
import pytest

from agent_api.app import create_app


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
    assert "textContent" in response.text
    for forbidden in ("innerHTML", "localStorage", "sessionStorage", "location.hash"):
        assert forbidden not in response.text
    assert "/" not in app.openapi()["paths"]
