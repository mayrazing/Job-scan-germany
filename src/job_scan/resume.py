from __future__ import annotations

import hashlib
import logging
import re
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Literal

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel
from pypdf import PdfReader

ResumeFormat = Literal["pdf", "docx"]

_PDF_EXTRACTION_TIMEOUT_SECONDS = 10.0
_PDF_PROCESS_JOIN_SECONDS = 0.5
_MAX_EXTRACTED_PDF_TEXT_BYTES = 16 * 1024 * 1024
_PDF_RESULT_OK = b"\x00"
_PDF_RESULT_ERROR = b"\x01"
_PDF_PROCESS_NAME = "job-scan-pdf-extractor"
_PDF_SENDER_THREAD_NAME = "job-scan-pdf-sender"


class ResumeError(RuntimeError):
    """Report a safe, actionable resume extraction failure."""


class UnsupportedResumeFormat(ResumeError):
    """Report a resume whose file suffix is not supported."""


class ResumeReadError(ResumeError):
    """Report a resume that cannot be read or parsed."""


class ResumeTextMissing(ResumeError):
    """Report a supported resume without enough extractable text."""


class ExtractedResume(BaseModel):
    path: Path
    sha256: str
    text: str
    format: ResumeFormat


def extract_resume(path: Path) -> ExtractedResume:
    """Extract, validate, and hash one PDF or DOCX resume."""
    format_name = _format_from_suffix(path)
    raw = _read_original_bytes(path)

    parser_failed = False
    try:
        if format_name == "pdf":
            extracted = _extract_pdf(raw)
        else:
            extracted = _extract_docx(raw)
    # Parser libraries expose inconsistent exception types; keep them behind this domain boundary.
    except Exception:  # noqa: BLE001
        parser_failed = True
        extracted = ""

    if parser_failed:
        error = ResumeReadError(f"Could not read {format_name.upper()} resume: {path}")
        raise error from None

    text = _normalize_text(extracted)
    if sum(not character.isspace() for character in text) < 100:
        if format_name == "pdf":
            raise ResumeTextMissing(
                "PDF resume has too little extractable text; "
                "OCR is not supported. Use a text-based PDF or DOCX file."
            )
        raise ResumeTextMissing(
            "DOCX resume has too little extractable text; "
            "use a document containing selectable text."
        )

    return ExtractedResume(
        path=path,
        sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        text=text,
        format=format_name,
    )


def _format_from_suffix(path: Path) -> ResumeFormat:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    raise UnsupportedResumeFormat(
        f"Unsupported resume format {suffix or '(none)'}; use a .pdf or .docx file."
    )


def _read_original_bytes(path: Path) -> bytes:
    try:
        return path.expanduser().resolve(strict=True).read_bytes()
    except FileNotFoundError as error:
        raise ResumeReadError(f"Resume file does not exist: {path}") from error
    except OSError as error:
        raise ResumeReadError(f"Could not read resume file: {path}") from error


def _extract_pdf(raw: bytes) -> str:
    deadline = monotonic() + _PDF_EXTRACTION_TIMEOUT_SECONDS
    context = get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_extract_pdf_worker,
        args=(child_connection,),
        name=_PDF_PROCESS_NAME,
    )
    response: bytes | None = None
    responses: list[bytes] = []
    exchange_completed = Event()
    sender: Thread | None = None

    def exchange_input_for_response() -> None:
        try:
            parent_connection.send_bytes(raw)
            remaining = max(0.0, deadline - monotonic())
            if parent_connection.poll(remaining):
                responses.append(
                    parent_connection.recv_bytes(
                        _MAX_EXTRACTED_PDF_TEXT_BYTES + len(_PDF_RESULT_OK)
                    )
                )
        except BaseException:  # noqa: BLE001
            # IPC and parser details stay inside the isolated extraction boundary.
            responses.clear()
        finally:
            exchange_completed.set()

    try:
        process.start()
        child_connection.close()
        sender = Thread(
            target=exchange_input_for_response,
            name=_PDF_SENDER_THREAD_NAME,
            daemon=True,
        )
        sender.start()
        exchange_completed.wait(max(0.0, deadline - monotonic()))
        if exchange_completed.is_set() and responses:
            response = responses[0]
    except (EOFError, OSError, RuntimeError, ValueError):
        response = None
    finally:
        _stop_pdf_process(process)
        if sender is not None:
            sender.join(_PDF_PROCESS_JOIN_SECONDS)
        parent_connection.close()
        child_connection.close()
        process.close()

    if response is None or not response.startswith(_PDF_RESULT_OK):
        error = RuntimeError("PDF extraction failed")
        raise error from None
    try:
        return response[len(_PDF_RESULT_OK) :].decode("utf-8")
    except UnicodeDecodeError:
        error = RuntimeError("PDF extraction failed")
    raise error from None


def _extract_pdf_worker(connection: Connection) -> None:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        logging.disable(logging.CRITICAL)
        try:
            raw = connection.recv_bytes()
            text = _extract_pdf_in_process(raw)
            encoded = text.encode("utf-8")
            if len(encoded) > _MAX_EXTRACTED_PDF_TEXT_BYTES:
                connection.send_bytes(_PDF_RESULT_ERROR)
            else:
                connection.send_bytes(_PDF_RESULT_OK + encoded)
        except BaseException:  # noqa: BLE001
            try:
                connection.send_bytes(_PDF_RESULT_ERROR)
            except (BrokenPipeError, EOFError, OSError):
                pass
        finally:
            connection.close()


def _stop_pdf_process(process: BaseProcess) -> None:
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(_PDF_PROCESS_JOIN_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_PDF_PROCESS_JOIN_SECONDS)


def _extract_pdf_in_process(raw: bytes) -> str:
    pages: list[str] = []
    for page in PdfReader(BytesIO(raw), strict=True).pages:
        text = page.extract_text()
        if text is not None and text.strip():
            pages.append(text)
    return "\n\n".join(pages)


def _extract_docx(raw: bytes) -> str:
    blocks: list[str] = []
    document = Document(BytesIO(raw))
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            _append_non_empty(blocks, item.text)
        elif isinstance(item, Table):
            for row in item.rows:
                for cell in row.cells:
                    _append_non_empty(blocks, cell.text)
    return "\n\n".join(blocks)


def _append_non_empty(blocks: list[str], text: str) -> None:
    if text.strip():
        blocks.append(text)


def _normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", normalized)
    return normalized.strip()
