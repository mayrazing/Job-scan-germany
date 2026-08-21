from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from job_scan.domain import SourceKind, SourceOccurrence
from job_scan.http_client import InvalidResponse
from job_scan.sources.base import BrowserSourceError
from job_scan.sources.opencli_challenge import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    is_challenge_payload,
    wait_for_challenge_clearance,
)

_DEFAULT_TIMEOUT_SECONDS = 90
_MAX_OUTPUT_BYTES = 5_000_000


def capture_source_job_snapshot_html(
    occurrence: SourceOccurrence,
) -> str | None:
    """Capture one stored automatic-source occurrence with its verified DOM rules."""
    script, source_name = _source_snapshot_script(occurrence)
    return capture_browser_snapshot(
        url=str(occurrence.url),
        script=script,
        source_name=source_name,
    )


def _source_snapshot_script(
    occurrence: SourceOccurrence,
) -> tuple[str, str]:
    """Return the existing source-specific DOM whitelist for one occurrence."""
    external_id = occurrence.external_id
    match occurrence.source:
        case SourceKind.ARBEITSAGENTUR:
            from job_scan.sources.jobsuche import (
                _snapshot_page_js as jobsuche_snapshot_script,
            )

            return jobsuche_snapshot_script(external_id), "arbeitsagentur"
        case SourceKind.BOSCH:
            from job_scan.sources.bosch import _snapshot_script as bosch_snapshot_script

            return bosch_snapshot_script(external_id), "Bosch"
        case SourceKind.DALLMEIER:
            from job_scan.sources.dallmeier import (
                _snapshot_script as dallmeier_snapshot_script,
            )

            return dallmeier_snapshot_script(external_id), "Dallmeier"
        case SourceKind.DHL:
            from job_scan.sources.dhl import _snapshot_page_js as dhl_snapshot_script

            return dhl_snapshot_script(external_id), "dhl"
        case SourceKind.GLASSDOOR:
            from job_scan.sources.glassdoor import _SNAPSHOT_PAGE_JS

            return _SNAPSHOT_PAGE_JS, "glassdoor"
        case SourceKind.INDEED:
            from job_scan.sources.indeed import _SNAPSHOT_PAGE_JS

            return _SNAPSHOT_PAGE_JS, "indeed"
        case SourceKind.LINKEDIN:
            from job_scan.sources.linkedin import (
                _snapshot_page_js as linkedin_snapshot_script,
            )

            return linkedin_snapshot_script(external_id), "linkedin"
        case SourceKind.SIEMENS:
            from job_scan.sources.siemens import (
                _snapshot_script as siemens_snapshot_script,
            )

            return siemens_snapshot_script(external_id), "Siemens"
        case SourceKind.SIMPLIFY:
            from job_scan.sources.simplify import (
                _snapshot_script as simplify_snapshot_script,
            )

            return simplify_snapshot_script(external_id), "simplify"
        case SourceKind.SMARTRECRUITERS:
            from job_scan.sources.smartrecruiters import (
                _snapshot_script as smartrecruiters_snapshot_script,
            )

            return (
                smartrecruiters_snapshot_script(
                    occurrence.source_instance,
                    external_id,
                ),
                "smartrecruiters",
            )
        case SourceKind.STEPSTONE:
            from job_scan.sources.stepstone import _SNAPSHOT_PAGE_JS

            return _SNAPSHOT_PAGE_JS, "stepstone"
        case SourceKind.SUCCESSFACTORS:
            from job_scan.sources.rohde_schwarz import (
                _snapshot_script as successfactors_snapshot_script,
            )

            return successfactors_snapshot_script(external_id), "Rohde & Schwarz"
        case SourceKind.TELEKOM:
            from job_scan.sources.telekom import (
                _snapshot_script as telekom_snapshot_script,
            )

            return telekom_snapshot_script(external_id), "Deutsche Telekom"
        case SourceKind.THYSSENKRUPP:
            from job_scan.sources.thyssenkrupp import (
                _snapshot_script as thyssenkrupp_snapshot_script,
            )

            return thyssenkrupp_snapshot_script(external_id), "thyssenkrupp"
        case SourceKind.MANUAL:
            return _manual_snapshot_script(occurrence.source_job_key), "manual"


def _manual_snapshot_script(source_job_key: str) -> str:
    """Return one generic DOM whitelist for any manually imported job page."""
    return browser_snapshot_script(
        r"""
  const snapshotKey = __EXPECTED_SOURCE_JOB_KEY__;
  const isChallenge = () => /Just a moment|Access Denied/i.test(document.title || "") ||
    /Verify you are human|Sicherheitsüberprüfung/i.test(document.body?.innerText || "");
  if (isChallenge()) return {status: "challenge"};
  const root = document.querySelector("main") || document.body;
  const visibleText = root?.innerText?.trim() || "";
  if (!root || !visibleText) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  await new Promise((resolve) => setTimeout(resolve, 500));
  const settledRoot = document.querySelector("main") || document.body;
  const settledText = settledRoot?.innerText?.trim() || "";
  if (!settledRoot || !settledText || settledText !== visibleText) {
    return {status: "unavailable", error_code: "structure_mismatch"};
  }
  return buildJobSnapshot({
    snapshotKey,
    title: document.title || settledRoot.querySelector("h1")?.innerText?.trim() || "",
    sourceLabel: "Manual import",
    accent: "#3b5bdb",
    roots: [settledRoot],
  });
""".strip().replace("__EXPECTED_SOURCE_JOB_KEY__", json.dumps(source_job_key))
    )

_BROWSER_SNAPSHOT_HELPERS_JS = r"""
  const removeResourceUrls = (value) => value.replace(
    /url\(\s*([^)]*?)\s*\)/gi,
    (match, target) => /^['"]?data:/i.test(target.trim()) ? match : "none"
  );
  const sanitizeCss = (value) => removeResourceUrls(value
    .replace(/behavior\s*:[^;}]*;?/gi, "")
    .replace(/[-\w]+\s*:[^;}]*expression\s*\([^;}]*;?/gi, ""))
    .replace(/@import\s+[^;]+;/gi, "")
    .replace(/<\/style/gi, "<\\/style");
  const sanitizeHtml = (value) => {
    let html = value;
    for (const tag of [
      "script", "noscript", "iframe", "object", "embed", "form", "button",
      "select", "textarea", "picture", "video", "audio",
    ]) {
      html = html.replace(
        new RegExp(`<${tag}\\b[^>]*>[\\s\\S]*?<\\/${tag}\\s*>`, "gi"),
        ""
      );
    }
    html = html.replace(
      /<([a-z][\w:-]*)\b(?=[^>]*(?:role\s*=\s*["']button["']|(?:data-at|data-test|data-testid|id)\s*=\s*["'][^"']*(?:apply|save|bewerb|vormerk|action-lane|header-company-logo-img)[^"']*["']))[^>]*>[\s\S]*?<\/\1\s*>/gi,
      ""
    );
    html = html.replace(
      /<(?:img|input|source|track|link|meta|base|use)\b[^>]*\/?\s*>/gi,
      ""
    );
    html = html.replace(
      /\s(?:href|src|srcset|poster|action|formaction|target|integrity|crossorigin)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi,
      ""
    );
    html = html.replace(
      /\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi,
      ""
    );
    return removeResourceUrls(html);
  };
  const escapeHtml = (value) => String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
  const buildJobSnapshot = async ({
    snapshotKey,
    title,
    sourceLabel,
    accent,
    roots,
    stylesheetOrigins = [],
  }) => {
    const cssParts = [...document.querySelectorAll("style")]
      .map((style) => style.textContent || "")
      .filter(Boolean);
    let cssBytes = cssParts.reduce((total, value) => total + value.length, 0);
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
      const href = link.href || "";
      let stylesheetOrigin = "";
      try {
        stylesheetOrigin = new URL(href).origin;
      } catch (_error) {
        continue;
      }
      if (
        (stylesheetOrigin !== location.origin &&
          !stylesheetOrigins.includes(stylesheetOrigin)) ||
        cssBytes >= 2_000_000
      ) continue;
      try {
        const css = await (await fetch(href, {credentials: "same-origin"})).text();
        if (cssBytes + css.length <= 2_000_000) {
          cssParts.push(css);
          cssBytes += css.length;
        }
      } catch (_error) {
        // Inline application styles still preserve the page when one bundle is blocked.
      }
    }
    const sourceStyle = sanitizeCss(cssParts.join("\n"));
    const content = roots
      .filter(Boolean)
      .map((root) => sanitizeHtml(root.outerHTML || ""))
      .join("");
    if (!content) {
      return {status: "unavailable", error_code: "structure_mismatch"};
    }
    const shellStyle = `html{background:#f4f6f8}body{margin:0}.job-snapshot-banner{box-sizing:border-box;padding:10px 20px;background:${accent};color:#fff;font:600 14px/1.4 Arial,sans-serif}.job-snapshot-content{box-sizing:border-box;max-width:1180px;margin:0 auto;padding:24px 16px 48px}.job-snapshot-content>*{margin-left:auto!important;margin-right:auto!important}`;
    return {
      status: "ok",
      html: `<!doctype html>\n<html lang="de" data-job-scan-snapshot="${escapeHtml(snapshotKey)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; form-action 'none'; base-uri 'none'"><title>${escapeHtml(title)}</title><style>${sourceStyle}\n${shellStyle}</style></head><body><div class="job-snapshot-banner">Gespeicherte Stellenanzeige · ${escapeHtml(sourceLabel)}</div><main class="job-snapshot-content" data-original-page-snapshot>${content}</main></body></html>`,
    };
  };
""".strip()


def browser_snapshot_script(source_body: str) -> str:
    """Wrap one source-specific DOM whitelist in the shared inert-page builder."""
    return (
        f"(async () => {{\n{_BROWSER_SNAPSHOT_HELPERS_JS}\n"
        f"const readJobSnapshot = async () => {{\n{source_body}\n}};\n"
        "let result = await readJobSnapshot();\n"
        "for (let attempt = 0; attempt < 10 && "
        'result?.error_code === "structure_mismatch"; attempt += 1) {\n'
        "  await new Promise((resolve) => setTimeout(resolve, 500));\n"
        "  result = await readJobSnapshot();\n"
        "}\nreturn result;\n})()"
    )


def capture_browser_snapshot(
    *,
    url: str,
    script: str,
    source_name: str,
    opencli_executable: str | Path | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    challenge_wait_seconds: float = DEFAULT_CHALLENGE_WAIT_SECONDS,
) -> str | None:
    """Capture one public page in an isolated OpenCLI browser session."""
    executable = str(opencli_executable or _find_opencli())
    safe_source = re.sub(r"[^a-z0-9]+", "-", source_name.casefold()).strip("-")
    session = f"job-scan-snapshot-{safe_source}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        _run_opencli(
            executable,
            ["browser", session, "open", url, "--window", "background"],
            timeout_seconds,
        )

        def read_page(timeout_override: float | None = None) -> object:
            effective_timeout = (
                timeout_seconds
                if timeout_override is None
                else min(timeout_seconds, timeout_override)
            )
            stdout = _run_opencli(
                executable,
                ["browser", session, "eval", script],
                effective_timeout,
            )
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                raise InvalidResponse(
                    f"OpenCLI {source_name} snapshot output was not valid JSON"
                ) from None

        payload = wait_for_challenge_clearance(
            read_page,
            is_challenge_payload,
            wait_seconds=challenge_wait_seconds,
            read_with_timeout=read_page,
        )
    finally:
        _close_opencli_session(executable, session, timeout_seconds)
    if isinstance(payload, dict) and payload.get("status") == "ok":
        html = payload.get("html")
        if isinstance(html, str) and html.strip():
            return html
    return None


def _find_opencli() -> str:
    scheduled = os.environ.get("JOB_SCAN_OPENCLI", "").strip()
    if scheduled:
        return scheduled
    executable = shutil.which("opencli")
    if executable is not None:
        return executable
    return str(Path(sys.executable).with_name("opencli"))


def _run_opencli(
    executable: str,
    arguments: list[str],
    timeout_seconds: float,
) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        raise BrowserSourceError(
            "OpenCLI executable was not found.", error_code="opencli_missing"
        ) from None
    except subprocess.TimeoutExpired:
        raise BrowserSourceError(
            "Job snapshot capture timed out.", error_code="opencli_timeout"
        ) from None
    except OSError:
        raise BrowserSourceError(
            "OpenCLI could not be started.", error_code="opencli_start_failed"
        ) from None
    if result.returncode != 0:
        raise BrowserSourceError(
            "OpenCLI could not read the job snapshot page.",
            error_code=f"opencli_exit_{result.returncode}",
        )
    if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise InvalidResponse("OpenCLI snapshot output exceeded the size limit")
    return result.stdout


def _close_opencli_session(
    executable: str,
    session: str,
    timeout_seconds: int,
) -> None:
    try:
        subprocess.run(
            [executable, "browser", session, "close"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
