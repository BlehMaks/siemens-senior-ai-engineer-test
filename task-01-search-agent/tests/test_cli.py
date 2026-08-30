from __future__ import annotations

from dataclasses import replace

import pytest

import search_agent.cli as cli_module
from search_agent import SearchHit, SearchQuery
from search_agent.cli import _demo_runner, _parser, async_main
from search_agent.model_auth import GoogleIdTokenAuth


class _NoResults:
    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        return ()


@pytest.mark.asyncio
async def test_demo_cli_succeeds_offline(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = await async_main(["Find the Siemens sustainability report"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"status": "completed"' in captured.out
    assert captured.err == ""


@pytest.mark.asyncio
async def test_cli_returns_failure_for_terminal_failed_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await async_main(
        ["Find the Siemens sustainability report"],
        runner=replace(
            _demo_runner("Find the Siemens sustainability report"),
            searcher=_NoResults(),
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert '"failure_reason": "no_evidence"' in captured.out
    assert captured.err == ""


@pytest.mark.asyncio
async def test_cli_rejects_invalid_request_and_ids_without_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    short_exit = await async_main(["x"])
    invalid_id_exit = await async_main(
        ["Find the Siemens sustainability report", "--run-id", "../secret"]
    )

    captured = capsys.readouterr()
    assert (short_exit, invalid_id_exit) == (2, 2)
    assert captured.out == ""
    assert captured.err == (
        "request rejected by input policy\nrequest rejected by input policy\n"
    )


def test_cli_exposes_ordered_search_backends_and_transport_profile() -> None:
    args = _parser().parse_args(
        [
            "Find the Siemens sustainability report",
            "--search-backends",
            "auto,duckduckgo",
            "--model-transport-profile",
            "cloud",
            "--model-google-id-token-audience",
            "https://model.example",
        ]
    )

    assert args.search_backends == ("auto", "duckduckgo")
    assert args.model_transport_profile == "cloud"
    assert args.model_google_id_token_audience == "https://model.example"


@pytest.mark.asyncio
async def test_cloud_cli_constructs_authenticated_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Executor:
        def __init__(self, settings: object, *, model_auth: object) -> None:
            captured["settings"] = settings
            captured["auth"] = model_auth

        async def run(self, **_kwargs: object) -> object:
            raise TypeError("stop after composition")

    monkeypatch.setattr(cli_module, "OllamaResearchExecutor", _Executor)

    exit_code = await async_main(
        [
            "Find the Siemens sustainability report",
            "--mode",
            "ollama",
            "--model",
            "granite3.3:8b-q4",
            "--ollama-base-url",
            "https://model.example",
            "--model-transport-profile",
            "cloud",
            "--model-google-id-token-audience",
            "https://model.example",
        ]
    )

    assert exit_code == 2
    assert isinstance(captured["auth"], GoogleIdTokenAuth)


def test_cli_reads_runtime_contract_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SEARCH_BACKENDS", "duckduckgo,auto")
    monkeypatch.setenv("AGENT_MODEL_TRANSPORT_PROFILE", "cloud")
    monkeypatch.setenv("AGENT_MODEL_GOOGLE_ID_TOKEN_AUDIENCE", "https://model.example")

    args = _parser().parse_args(["Find the Siemens sustainability report"])

    assert args.search_backends == ("duckduckgo", "auto")
    assert args.model_transport_profile == "cloud"
    assert args.model_google_id_token_audience == "https://model.example"


def test_cli_prefers_plural_search_env_and_supports_legacy_singular_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_SEARCH_BACKENDS", raising=False)
    monkeypatch.setenv("AGENT_SEARCH_BACKEND", "duckduckgo")
    legacy = _parser().parse_args(["Find the Siemens sustainability report"])

    monkeypatch.setenv("AGENT_SEARCH_BACKENDS", "auto,duckduckgo")
    preferred = _parser().parse_args(["Find the Siemens sustainability report"])

    alias = _parser().parse_args(
        [
            "Find the Siemens sustainability report",
            "--search-backend",
            "duckduckgo,auto",
        ]
    )

    assert legacy.search_backends == ("duckduckgo",)
    assert preferred.search_backends == ("auto", "duckduckgo")
    assert alias.search_backends == ("duckduckgo", "auto")


@pytest.mark.parametrize(
    "value", ["", "auto,", "bing", "auto,auto", "auto,duckduckgo,auto"]
)
def test_cli_rejects_invalid_search_backend_lists(value: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "Find the Siemens sustainability report",
                "--search-backends",
                value,
            ]
        )
