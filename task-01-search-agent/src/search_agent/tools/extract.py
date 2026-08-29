"""Local-only main-content extraction from already fetched bytes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from trafilatura import bare_extraction

from .fetch import FetchedDocument, _validated_fetched_document


def _worker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = package_root
    return environment


class ExtractionFailureReason(StrEnum):
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LARGE = "input_too_large"
    MALFORMED_CONTENT = "malformed_content"
    NO_CONTENT = "no_content"
    OUTPUT_TOO_LARGE = "output_too_large"


class ExtractionError(RuntimeError):
    def __init__(self, reason: ExtractionFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    canonical_url: str
    title: str | None
    text: str


@dataclass(frozen=True, slots=True)
class LocalExtractor:
    max_input_bytes: int = 2 * 1024 * 1024
    max_output_chars: int = 100_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_bytes", self.max_input_bytes),
            ("max_output_chars", self.max_output_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def extract(self, document: FetchedDocument) -> ExtractedDocument:
        document = _validated_fetched_document(document)
        body = document.body
        if b"\x00" in body:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content is malformed",
            )
        if not body.strip():
            raise ExtractionError(
                ExtractionFailureReason.EMPTY_INPUT,
                "fetched content is empty",
            )
        if len(body) > self.max_input_bytes:
            raise ExtractionError(
                ExtractionFailureReason.INPUT_TOO_LARGE,
                "fetched content exceeds the extraction input limit",
            )
        if document.content_type == "text/plain":
            return self._extract_plain_text(document)
        if document.content_type not in {"application/xhtml+xml", "text/html"}:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content type is not extractable",
            )

        try:
            # bare_extraction parses supplied bytes only; no downloader is reachable.
            extracted = bare_extraction(
                body,
                url=document.canonical_url,
                output_format="python",
                include_comments=False,
                include_tables=False,
                include_links=False,
                include_images=False,
                with_metadata=True,
            )
        except Exception:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extraction failed",
            ) from None

        if extracted is None:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if isinstance(extracted, dict):
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extractor returned an invalid result",
            )
        try:
            raw_text = extracted.text
            raw_title = extracted.title
            if not isinstance(raw_text, (str, type(None))) or not isinstance(
                raw_title, (str, type(None))
            ):
                raise TypeError
            checked_text = raw_text.strip() if isinstance(raw_text, str) else ""
            checked_title = raw_title.strip() if isinstance(raw_title, str) else ""
            if not isinstance(checked_text, str) or not isinstance(checked_title, str):
                raise TypeError
            # Exercise parser hooks only to detect failures. Base operations on the
            # original value prevent a subclass from changing whitespace semantics.
            text = (
                str.strip(str.__getitem__(raw_text, slice(None)))
                if isinstance(raw_text, str)
                else ""
            )
            title = (
                str.strip(str.__getitem__(raw_title, slice(None))) or None
                if isinstance(raw_title, str)
                else None
            )
            output_chars = len(text) + (len(title) if title is not None else 0)
        except Exception:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extractor returned an invalid result",
            ) from None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if output_chars > self.max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "extracted content exceeds the output limit",
            )
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=title,
            text=text,
        )

    def _extract_plain_text(self, document: FetchedDocument) -> ExtractedDocument:
        try:
            text = document.body.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "plain text is not valid UTF-8",
            ) from None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "no main content was extracted",
            )
        if len(text) > self.max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "extracted content exceeds the output limit",
            )
        return ExtractedDocument(
            canonical_url=document.canonical_url,
            title=None,
            text=text,
        )


@dataclass(frozen=True, slots=True)
class AsyncLocalExtractor:
    """Run bounded local parsing in a process that cancellation can terminate."""

    extractor: LocalExtractor = field(default_factory=LocalExtractor)

    async def extract(self, document: FetchedDocument) -> ExtractedDocument:
        try:
            document = _validated_fetched_document(document)
        except TypeError:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "fetched content is malformed",
            ) from None
        if len(document.body) > self.extractor.max_input_bytes:
            raise ExtractionError(
                ExtractionFailureReason.INPUT_TOO_LARGE,
                "fetched content exceeds the extraction input limit",
            )
        payload = json.dumps(
            {
                "body": base64.b64encode(document.body).decode("ascii"),
                "canonical_url": document.canonical_url,
                "content_type": document.content_type,
                "max_input_bytes": self.extractor.max_input_bytes,
                "max_output_chars": self.extractor.max_output_chars,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "search_agent.tools.extract",
            "--worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_worker_environment(),
        )
        try:
            stdout, _ = await process.communicate(payload)
        except BaseException:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                reap_task = asyncio.create_task(process.wait())
                while not reap_task.done():
                    try:
                        await asyncio.shield(reap_task)
                    except asyncio.CancelledError:
                        continue
                with suppress(Exception):
                    reap_task.result()
            raise
        if process.returncode != 0:
            raise ExtractionError(
                ExtractionFailureReason.MALFORMED_CONTENT,
                "content extraction process failed",
            )
        return _decode_worker_result(
            stdout,
            expected_url=document.canonical_url,
            max_output_chars=self.extractor.max_output_chars,
        )


def _decode_worker_result(
    payload: bytes, *, expected_url: str, max_output_chars: int
) -> ExtractedDocument:
    try:
        if not isinstance(payload, bytes):
            raise ValueError
        rendered = bytes.__getitem__(payload, slice(None))
        if type(rendered) is not bytes:
            raise ValueError
        if bytes.__len__(rendered) > max_output_chars * 12 + 4096:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "content extraction process output exceeds the limit",
            )
        result = json.loads(rendered)
        if not isinstance(result, dict) or set(result) not in (
            {"ok", "reason"},
            {"ok", "canonical_url", "title", "text"},
        ):
            raise ValueError
        if result["ok"] is not True:
            reason = ExtractionFailureReason(result["reason"])
            raise ExtractionError(reason, "content extraction failed")
        canonical_url = result["canonical_url"]
        title = result["title"]
        text = result["text"]
        if (
            type(canonical_url) is not str
            or (title is not None and type(title) is not str)
            or type(text) is not str
            or canonical_url != expected_url
        ):
            raise ValueError
        text = text.strip()
        if title is not None:
            title = title.strip() or None
        if not text:
            raise ExtractionError(
                ExtractionFailureReason.NO_CONTENT,
                "content extraction process returned no content",
            )
        if len(text) + (len(title) if title is not None else 0) > max_output_chars:
            raise ExtractionError(
                ExtractionFailureReason.OUTPUT_TOO_LARGE,
                "content extraction process output exceeds the limit",
            )
        return ExtractedDocument(
            canonical_url=canonical_url,
            title=title,
            text=text,
        )
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError(
            ExtractionFailureReason.MALFORMED_CONTENT,
            "content extraction process returned an invalid result",
        ) from None


def _worker_main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read())
        if not isinstance(payload, dict) or set(payload) != {
            "body",
            "canonical_url",
            "content_type",
            "max_input_bytes",
            "max_output_chars",
        }:
            raise ValueError
        document = FetchedDocument(
            canonical_url=payload["canonical_url"],
            content_type=payload["content_type"],
            body=base64.b64decode(payload["body"], validate=True),
        )
        extracted = LocalExtractor(
            max_input_bytes=payload["max_input_bytes"],
            max_output_chars=payload["max_output_chars"],
        ).extract(document)
        result = {
            "ok": True,
            "canonical_url": extracted.canonical_url,
            "title": extracted.title,
            "text": extracted.text,
        }
    except ExtractionError as exc:
        result = {"ok": False, "reason": exc.reason.value}
    except Exception:
        result = {
            "ok": False,
            "reason": ExtractionFailureReason.MALFORMED_CONTENT.value,
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
