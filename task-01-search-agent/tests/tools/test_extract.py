from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import trafilatura
import trafilatura.downloads
from pypdf import PdfWriter

from search_agent import AsyncLocalExtractor
from search_agent.tools import extract as extract_module
from search_agent.tools.extract import (
    ExtractedBlock,
    ExtractionError,
    ExtractionFailureReason,
    LocalExtractor,
)
from search_agent.tools.fetch import FetchedDocument


class ExplodingString(str):
    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError("parser-secret-must-not-escape")


class BenignString(str):
    pass


class LyingStripString(str):
    def strip(self, chars: str | None = None) -> str:
        return self


class LyingBytes(bytes):
    def __len__(self) -> int:
        return 1


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "retrieval"


def _document(body: bytes, *, content_type: str = "text/html") -> FetchedDocument:
    return FetchedDocument(
        canonical_url="https://example.com/report",
        content_type=content_type,
        body=body,
    )


def test_extracts_title_and_main_text_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network downloader must not be called")

    monkeypatch.setattr(trafilatura, "fetch_url", network_forbidden)
    monkeypatch.setattr(trafilatura.downloads, "fetch_url", network_forbidden)
    html = b"""
        <html><head><title>Ignored browser title</title></head><body>
        <nav>Navigation</nav><article><h1>Siemens annual report</h1>
        <p>Siemens reported a meaningful result in this sufficiently long fixture
        paragraph used to exercise local main-content extraction.</p></article>
        </body></html>
    """

    extracted = LocalExtractor().extract(_document(html))

    assert extracted.canonical_url == "https://example.com/report"
    assert extracted.title == "Siemens annual report"
    assert "Siemens reported a meaningful result" in extracted.text
    with pytest.raises(FrozenInstanceError):
        extracted.text = "changed"  # type: ignore[misc]


def test_html_charset_is_detected_from_fetched_bytes() -> None:
    html = """
        <html><head><meta charset="iso-8859-1"><title>Überblick</title></head>
        <body><article><h1>Überblick</h1><p>Siemens veröffentlichte einen
        ausreichend langen Nachhaltigkeitsbericht für die lokale Extraktion.
        </p></article></body></html>
    """.encode("iso-8859-1")

    extracted = LocalExtractor().extract(_document(html))

    assert extracted.title == "Überblick"
    assert "veröffentlichte" in extracted.text


def test_preserves_tables_while_disabling_links_and_network_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_bare_extraction(body: bytes, **kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(title="Title", text="Main text")

    monkeypatch.setattr(extract_module, "bare_extraction", fake_bare_extraction)

    LocalExtractor().extract(_document(b"<html><p>Main text</p></html>"))

    assert captured["url"] == "https://example.com/report"
    assert captured["include_comments"] is False
    assert captured["include_tables"] is True
    assert captured["include_links"] is False
    assert captured["include_images"] is False
    assert "download" not in captured


def test_html_table_text_and_metadata_are_preserved() -> None:
    html = (_FIXTURES / "report_table.html").read_bytes()

    extracted = LocalExtractor().extract(_document(html))

    assert "Metric | Value | Unit" in extracted.text
    assert "Scope 3 | 14.7 | million tonnes CO2e" in extracted.text
    assert (
        ExtractedBlock(
            text=("Metric | Value | Unit\nScope 3 | 14.7 | million tonnes CO2e"),
            table_index=1,
        )
        in extracted.blocks
    )


def test_pdf_extracts_page_and_table_provenance_from_frozen_fixture() -> None:
    body = base64.b64decode(
        (_FIXTURES / "late_fact_report.pdf.b64").read_text().strip(),
        validate=True,
    )

    extracted = LocalExtractor().extract(
        _document(body, content_type="application/pdf")
    )

    assert extracted.title == "Siemens Sustainability Report"
    assert "The 2025 Scope 3 emissions were 14.7 million tonnes CO2e." in extracted.text
    assert {block.page_number for block in extracted.blocks} == {1, 2}
    table_blocks = [
        block for block in extracted.blocks if block.table_index is not None
    ]
    assert table_blocks
    assert table_blocks[0].page_number == 2
    assert "Metric" in table_blocks[0].text
    assert "million tonnes CO2e" in table_blocks[0].text


@pytest.mark.parametrize(
    ("extractor", "fixture_name", "reason"),
    [
        (
            LocalExtractor(max_input_bytes=8),
            "late_fact_report.pdf.b64",
            ExtractionFailureReason.INPUT_TOO_LARGE,
        ),
        (
            LocalExtractor(),
            "malformed.pdf.b64",
            ExtractionFailureReason.MALFORMED_CONTENT,
        ),
    ],
)
def test_malformed_and_oversized_pdfs_fail_safely(
    extractor: LocalExtractor,
    fixture_name: str,
    reason: ExtractionFailureReason,
) -> None:
    body = base64.b64decode(
        (_FIXTURES / fixture_name).read_text().strip(),
        validate=True,
    )
    with pytest.raises(ExtractionError) as error:
        extractor.extract(_document(body, content_type="application/pdf"))

    assert error.value.reason is reason
    assert error.value.__cause__ is None
    assert body.decode("latin-1") not in str(error.value)


@pytest.mark.parametrize(
    ("extractor", "reason"),
    [
        (LocalExtractor(max_pdf_pages=1), ExtractionFailureReason.PAGE_LIMIT),
        (
            LocalExtractor(max_pdf_content_stream_bytes=16),
            ExtractionFailureReason.CONTENT_STREAM_TOO_LARGE,
        ),
    ],
)
def test_pdf_page_and_content_stream_limits_are_typed(
    extractor: LocalExtractor, reason: ExtractionFailureReason
) -> None:
    body = base64.b64decode(
        (_FIXTURES / "late_fact_report.pdf.b64").read_text().strip(),
        validate=True,
    )

    with pytest.raises(ExtractionError) as error:
        extractor.extract(_document(body, content_type="application/pdf"))

    assert error.value.reason is reason


def test_encrypted_pdf_is_rejected_without_attempting_decryption() -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    writer.write(output)

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(
            _document(output.getvalue(), content_type="application/pdf")
        )

    assert error.value.reason is ExtractionFailureReason.ENCRYPTED_DOCUMENT
    assert "secret" not in str(error.value)


def test_extracts_utf8_plain_text_without_html_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def parser_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTML parser must not handle text/plain")

    monkeypatch.setattr(extract_module, "bare_extraction", parser_forbidden)

    extracted = LocalExtractor().extract(
        _document("\ufeffSiemens report\nline two".encode(), content_type="text/plain")
    )

    assert extracted.title is None
    assert extracted.text == "Siemens report\nline two"


@pytest.mark.asyncio
async def test_async_local_extractor_runs_in_a_cancellable_process() -> None:
    extracted = await AsyncLocalExtractor().extract(
        _document(b"Siemens report\nline two", content_type="text/plain")
    )

    assert extracted.canonical_url == "https://example.com/report"
    assert extracted.title is None
    assert extracted.text == "Siemens report\nline two"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"four", LyingBytes(b"four")])
async def test_async_local_extractor_rejects_oversize_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    spawned = False

    async def create_process(*args: object, **kwargs: object) -> None:
        nonlocal spawned
        del args, kwargs
        spawned = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    extractor = AsyncLocalExtractor(LocalExtractor(max_input_bytes=3))

    with pytest.raises(ExtractionError) as error:
        await extractor.extract(_document(body))

    assert error.value.reason is ExtractionFailureReason.INPUT_TOO_LARGE
    assert spawned is False


@pytest.mark.asyncio
async def test_async_local_extractor_propagates_its_package_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_environment: dict[str, str] | None = None

    class Process:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            del payload
            return (
                b'{"ok":true,"canonical_url":"https://example.com/report",'
                b'"title":null,"text":"content"}',
                b"",
            )

    async def create_process(*args: object, **kwargs: object) -> Process:
        nonlocal worker_environment
        del args
        worker_environment = kwargs["env"]  # type: ignore[assignment]
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    await AsyncLocalExtractor().extract(
        _document(b"content", content_type="text/plain")
    )

    assert worker_environment is not None
    package_root = str(Path(extract_module.__file__).resolve().parents[2])
    assert package_root in worker_environment["PYTHONPATH"].split(os.pathsep)


@pytest.mark.asyncio
async def test_async_local_extractor_reaps_process_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = asyncio.Event()
    wait_started = asyncio.Event()
    wait_release = asyncio.Event()
    reaped = asyncio.Event()

    class BlockingProcess:
        returncode: int | None = None

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            del payload
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.returncode = -9
            killed.set()

        async def wait(self) -> int:
            wait_started.set()
            await wait_release.wait()
            reaped.set()
            return -9

    async def create_process(*args: object, **kwargs: object) -> BlockingProcess:
        del args, kwargs
        return BlockingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    task = asyncio.create_task(
        AsyncLocalExtractor().extract(
            _document(b"Siemens report", content_type="text/plain")
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    await wait_started.wait()
    wait_release.set()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed.is_set()
    assert reaped.is_set()


def _install_worker_result(
    monkeypatch: pytest.MonkeyPatch,
    result: object,
) -> None:
    class Process:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            del payload
            if isinstance(result, bytes):
                return result, b""
            return json.dumps(result).encode(), b""

    async def create_process(*args: object, **kwargs: object) -> Process:
        del args, kwargs
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_result", "reason"),
    [
        (
            {
                "ok": True,
                "canonical_url": "https://example.com/report",
                "title": None,
                "text": "",
            },
            ExtractionFailureReason.NO_CONTENT,
        ),
        (
            {
                "ok": True,
                "canonical_url": "https://example.com/report",
                "title": None,
                "text": " \n ",
            },
            ExtractionFailureReason.NO_CONTENT,
        ),
        (
            {
                "ok": True,
                "canonical_url": "https://example.com/report",
                "title": None,
                "text": "four",
            },
            ExtractionFailureReason.OUTPUT_TOO_LARGE,
        ),
        (
            {
                "ok": True,
                "canonical_url": "https://example.com/other",
                "title": None,
                "text": "ok",
            },
            ExtractionFailureReason.MALFORMED_CONTENT,
        ),
        (
            {
                "ok": True,
                "canonical_url": "https://example.com/report",
                "title": None,
                "text": 7,
            },
            ExtractionFailureReason.MALFORMED_CONTENT,
        ),
    ],
)
async def test_async_worker_success_is_revalidated(
    monkeypatch: pytest.MonkeyPatch,
    worker_result: object,
    reason: ExtractionFailureReason,
) -> None:
    _install_worker_result(monkeypatch, worker_result)

    with pytest.raises(ExtractionError) as error:
        await AsyncLocalExtractor(LocalExtractor(max_output_chars=3)).extract(
            _document(b"ok", content_type="text/plain")
        )

    assert error.value.reason is reason


@pytest.mark.asyncio
async def test_async_worker_combined_output_limit_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = {
        "ok": True,
        "canonical_url": "https://example.com/report",
        "title": "x",
        "text": "abc",
    }
    _install_worker_result(monkeypatch, exact)

    extracted = await AsyncLocalExtractor(LocalExtractor(max_output_chars=4)).extract(
        _document(b"ok", content_type="text/plain")
    )
    assert extracted.title == "x"
    assert extracted.text == "abc"

    over = {**exact, "title": "xx"}
    _install_worker_result(monkeypatch, over)
    with pytest.raises(ExtractionError) as error:
        await AsyncLocalExtractor(LocalExtractor(max_output_chars=4)).extract(
            _document(b"ok", content_type="text/plain")
        )
    assert error.value.reason is ExtractionFailureReason.OUTPUT_TOO_LARGE


@pytest.mark.asyncio
async def test_async_worker_stdout_limit_rejects_first_byte_over_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    max_output_chars = 3
    _install_worker_result(
        monkeypatch,
        b"x" * (max_output_chars * 12 + 4097),
    )

    with pytest.raises(ExtractionError) as error:
        await AsyncLocalExtractor(
            LocalExtractor(max_output_chars=max_output_chars)
        ).extract(_document(b"ok", content_type="text/plain"))

    assert error.value.reason is ExtractionFailureReason.OUTPUT_TOO_LARGE


def test_plain_text_requires_utf8() -> None:
    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"\xff", content_type="text/plain"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"  \n", ExtractionFailureReason.EMPTY_INPUT),
        (b"bad\x00html", ExtractionFailureReason.MALFORMED_CONTENT),
        (b"<html><body></body></html>", ExtractionFailureReason.NO_CONTENT),
    ],
)
def test_empty_malformed_and_contentless_inputs_are_typed(
    body: bytes,
    reason: ExtractionFailureReason,
) -> None:
    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(body))

    assert error.value.reason is reason


def test_input_and_output_limits_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExtractionError) as input_error:
        LocalExtractor(max_input_bytes=3).extract(_document(b"four"))

    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(title="Title", text="too long"),
    )
    with pytest.raises(ExtractionError) as output_error:
        LocalExtractor(max_output_chars=5).extract(_document(b"<p>text</p>"))

    assert input_error.value.reason is ExtractionFailureReason.INPUT_TOO_LARGE
    assert output_error.value.reason is ExtractionFailureReason.OUTPUT_TOO_LARGE


def test_parser_failure_is_mapped_to_malformed_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parser(*args: object, **kwargs: object) -> None:
        raise ValueError("parser rejected input")

    monkeypatch.setattr(extract_module, "bare_extraction", fail_parser)

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>broken"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize(
    "malformed",
    [object(), SimpleNamespace(title="Title", text=object())],
)
def test_malformed_parser_result_is_typed(
    monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    monkeypatch.setattr(
        extract_module, "bare_extraction", lambda *args, **kwargs: malformed
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT


@pytest.mark.parametrize("field", ["text", "title"])
def test_hostile_string_fields_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    result = SimpleNamespace(title="Title", text="Main text")
    setattr(result, field, ExplodingString("hostile"))
    monkeypatch.setattr(
        extract_module, "bare_extraction", lambda *args, **kwargs: result
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.MALFORMED_CONTENT
    assert error.value.__cause__ is None
    assert "parser-secret" not in str(error.value)


def test_extractor_returns_plain_strings_from_subclass_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(
            title=BenignString(" Title "), text=BenignString(" Main text ")
        ),
    )

    extracted = LocalExtractor().extract(_document(b"<html>content</html>"))

    assert type(extracted.title) is str
    assert type(extracted.text) is str
    assert extracted.title == "Title"
    assert extracted.text == "Main text"


def test_string_subclass_cannot_forge_nonempty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extract_module,
        "bare_extraction",
        lambda *args, **kwargs: SimpleNamespace(
            title=None, text=LyingStripString("   ")
        ),
    )

    with pytest.raises(ExtractionError) as error:
        LocalExtractor().extract(_document(b"<html>content</html>"))

    assert error.value.reason is ExtractionFailureReason.NO_CONTENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_input_bytes": 0},
        {"max_input_bytes": True},
        {"max_output_chars": 0},
    ],
)
def test_extractor_rejects_invalid_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LocalExtractor(**kwargs)  # type: ignore[arg-type]
