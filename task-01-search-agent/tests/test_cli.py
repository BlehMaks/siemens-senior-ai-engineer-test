from __future__ import annotations

from dataclasses import replace

import pytest

from search_agent import SearchHit, SearchQuery
from search_agent.cli import _demo_runner, async_main


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
