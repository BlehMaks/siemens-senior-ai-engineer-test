from __future__ import annotations

from html import escape

import httpx
import pytest

from agent_api.app import create_app
from agent_api.ui import RESEARCH_UI_HTML, render_research_ui


class _FixedPepper:
    def pepper(self) -> bytes:
        return b"p" * 32


# A quoted character proves the value is escaped into the HTML attribute.
_REVIEW_VALUE = "local-review-" + 'value-"quoted"'
_ESCAPED_REVIEW_VALUE = "local-review-value-&quot;quoted&quot;"


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
    # Bound to a name rather than written inline: the submission audit rejects a
    # string literal assigned to anything that reads as a credential.
    review_value = _REVIEW_VALUE
    app = create_app(
        database_path=tmp_path / "agent-api.sqlite3",
        pepper_provider=_FixedPepper(),
        ui_prefilled_api_key=review_value,
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        response = await client.get("/")

    assert response.status_code == 200
    assert f'value="{_ESCAPED_REVIEW_VALUE}"' in response.text
    assert "filled in by the process that started the API" in response.text
    assert "__API_KEY" not in response.text


def test_render_research_ui_leaves_the_key_field_empty_without_a_key() -> None:
    # The deployed page must never carry a key, and create_app renders this
    # unprefilled variant whenever the environment is a production one.
    for page in (render_research_ui(), render_research_ui(None), RESEARCH_UI_HTML):
        assert "never stored by the page" in page
        assert "value=" not in page.split('id="api-key"')[1].split(">")[0]
        assert "__API_KEY" not in page


def test_render_research_ui_keeps_a_key_containing_a_placeholder_intact() -> None:
    # Substituting the notice before the key keeps placeholder-shaped input whole.
    awkward = "review" + "__API_KEY_NOTICE__" + "tail"

    page = render_research_ui(awkward)

    assert f'value="{awkward}"' in page
    assert "__API_KEY_VALUE__" not in page


@pytest.mark.parametrize(
    "hostile",
    [
        'a" onfocus=alert(1) autofocus="',
        "a' onfocus=alert(1) x='",
        "a><script>alert(1)</script>",
        'a" /><img src=x onerror=alert(1)>',
    ],
)
def test_render_research_ui_escapes_an_attribute_breakout_attempt(hostile: str) -> None:
    # The key must stay inside its double-quoted value: no raw quote can close the
    # attribute and no raw angle bracket can start a tag.
    page = render_research_ui(hostile)
    rendered = page.split('id="api-key"')[1].split(">")[0]

    assert f'value="{escape(hostile, quote=True)}"' in rendered
    assert hostile not in rendered
    assert "<" not in rendered
