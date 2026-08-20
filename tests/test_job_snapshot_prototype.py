from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup


PROTOTYPE_ROOT = (
    Path(__file__).parents[1] / "prototypes" / "job-snapshot"
)


def test_job_card_opens_a_local_snapshot_in_a_new_tab() -> None:
    page = BeautifulSoup(
        (PROTOTYPE_ROOT / "index.html").read_text(encoding="utf-8"),
        "html.parser",
    )

    snapshot_link = page.select_one("article.job-card a[data-job-snapshot]")

    assert snapshot_link is not None
    assert snapshot_link.get_text(" ", strip=True) == "Job snapshot"
    assert snapshot_link.get("href") == "original-page.html"
    assert snapshot_link.get("target") == "_blank"


def test_original_page_snapshot_is_local_and_inert() -> None:
    page = BeautifulSoup(
        (PROTOTYPE_ROOT / "original-page.html").read_text(encoding="utf-8"),
        "html.parser",
    )

    archive_notice = page.select_one("[data-original-page-snapshot]")
    policy = page.select_one('meta[http-equiv="Content-Security-Policy"]')

    assert archive_notice is not None
    assert "saved original page" in archive_notice.get_text(" ", strip=True).lower()
    assert page.find(string=lambda text: text and "Senior Software Engineer" in text)
    assert page.find(string=lambda text: text and "IDnow GmbH" in text)
    assert policy is not None and "default-src 'none'" in policy.get("content", "")
    assert page.select_one("script") is None
    assert all(
        not element.get(attribute, "").startswith(("http://", "https://", "//"))
        for selector, attribute in (("img", "src"), ("link", "href"))
        for element in page.select(selector)
    )
