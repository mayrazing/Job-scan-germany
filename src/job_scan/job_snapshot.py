from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

_SNAPSHOT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_UNSAFE_CSS = re.compile(r"@import\b|expression\s*\(|behavior\s*:", re.IGNORECASE)
_ACTIVE_TAGS = {
    "base",
    "button",
    "embed",
    "form",
    "iframe",
    "input",
    "link",
    "noscript",
    "object",
    "script",
    "select",
    "textarea",
}
_DATA_RESOURCE_ATTRIBUTES = {"data", "poster", "src"}
_LINK_ATTRIBUTES = {"href", "xlink:href"}
_MAX_SNAPSHOT_BYTES = 5_000_000


class JobSnapshotReference(BaseModel):
    """Identify one immutable locally stored job-page snapshot."""

    snapshot_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    captured_at: datetime


class JobSnapshotStore:
    """Persist and read self-contained job-page snapshots."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def save(
        self,
        *,
        source_job_key: str,
        captured_at: datetime,
        html: str,
    ) -> JobSnapshotReference:
        """Atomically save one safe local HTML page and return its identity."""
        contents = html.encode("utf-8")
        if len(contents) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("job snapshot exceeded the size limit")
        _validate_snapshot_html(html, source_job_key)
        digest = hashlib.sha256(contents).hexdigest()
        snapshot_id = f"sha256:{digest}"
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._path(snapshot_id)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix=f".{digest}.",
            suffix=".html",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != contents:
                    raise RuntimeError("job snapshot ID collision") from None
        finally:
            temporary.unlink(missing_ok=True)
        return JobSnapshotReference(
            snapshot_id=snapshot_id,
            captured_at=captured_at,
        )

    def read(self, snapshot_id: str) -> bytes:
        """Return one validated snapshot's exact HTML bytes."""
        return self._path(snapshot_id).read_bytes()

    def _path(self, snapshot_id: str) -> Path:
        if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise ValueError("invalid job snapshot ID")
        return self._directory / f"{snapshot_id.removeprefix('sha256:')}.html"


def _validate_snapshot_html(html: str, source_job_key: str) -> None:
    """Reject active, remote, or incorrectly identified snapshot documents."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("html")
    if not isinstance(root, Tag) or root.get("data-job-scan-snapshot") != source_job_key:
        raise ValueError("job snapshot marker does not match the requested job")

    for element in soup.find_all(True):
        if not isinstance(element, Tag):
            continue
        name = element.name.lower()
        if name in _ACTIVE_TAGS:
            _unsafe_snapshot(f"active <{name}> element")
        if name == "meta" and str(element.get("http-equiv", "")).lower() == "refresh":
            _unsafe_snapshot("meta refresh")
        for raw_name, raw_value in element.attrs.items():
            attribute = raw_name.lower()
            if attribute.startswith("on") or attribute in {"action", "formaction", "srcset"}:
                _unsafe_snapshot(f"active {attribute} attribute")
            value = " ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
            if attribute in _LINK_ATTRIBUTES:
                _unsafe_snapshot(f"link {attribute} attribute")
            if attribute in _DATA_RESOURCE_ATTRIBUTES and not value.strip().lower().startswith(
                "data:"
            ):
                _unsafe_snapshot(f"external {attribute} resource")
            if attribute == "style":
                _validate_css(value)

    for style in soup.find_all("style"):
        _validate_css(style.get_text())


def _validate_css(css: str) -> None:
    unsafe_rule = _UNSAFE_CSS.search(css)
    if unsafe_rule is not None:
        _unsafe_snapshot(f"active CSS rule {unsafe_rule.group(0)}")
    for match in _CSS_URL.finditer(css):
        if not match.group(2).strip().lower().startswith("data:"):
            _unsafe_snapshot("external CSS resource")


def _unsafe_snapshot(reason: str) -> None:
    raise ValueError(f"job snapshot must be a safe self-contained job page: {reason}")
