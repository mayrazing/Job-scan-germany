from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import TYPE_CHECKING

from jinja2 import Environment, PackageLoader, select_autoescape

from job_scan.ai_config import AiProviderView
from job_scan.ai_selection import (
    AiRuntimeSelection,
    apply_ai_selection_to_claude,
)
from job_scan.ats_models import AtsCheckBundle, AtsHistoryEntry
from job_scan.config import ClaudeSettings, SchedulerSettings
from job_scan.dashboard.view_model import (
    build_current_dashboard,
    build_global_dashboard,
)
from job_scan.domain import Snapshot, StoreMeta
from job_scan.setup_service import SetupAnswers

if TYPE_CHECKING:
    from job_scan.search_history import SearchHistoryEntry


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """Create the package-backed autoescaping template environment once."""
    return Environment(
        loader=PackageLoader("job_scan.dashboard", "templates"),
        autoescape=select_autoescape(enabled_extensions=("html", "xml")),
    )


@lru_cache(maxsize=1)
def _asset_text(name: str) -> str:
    """Read one packaged dashboard asset for inline, offline rendering."""
    return files("job_scan.dashboard").joinpath("static", name).read_text(encoding="utf-8")


def render_dashboard(
    snapshot: Snapshot,
    global_snapshot: Snapshot | None = None,
) -> str:
    """Render one self-contained HTML page derived from a snapshot."""
    template = _environment().get_template("index.html")
    return template.render(
        revision=snapshot.meta.data_revision,
        current_dashboard=build_current_dashboard(snapshot),
        global_dashboard=build_global_dashboard(
            global_snapshot or Snapshot(meta=StoreMeta(data_revision=0))
        ),
        dashboard_css=_asset_text("dashboard.css"),
        dashboard_js=_asset_text("dashboard.js"),
    )


def render_console(
    snapshot: Snapshot | None = None,
    global_snapshot: Snapshot | None = None,
    setup_answers: SetupAnswers | None = None,
    ai_selection: AiRuntimeSelection | None = None,
    ai_providers: list[AiProviderView] | None = None,
    scan_history: list[SearchHistoryEntry] | None = None,
    selected_run_id: str | None = None,
    ats_history: list[AtsHistoryEntry] | None = None,
    selected_ats: AtsCheckBundle | None = None,
) -> str:
    """Render the packaged setup page served by the local review server."""
    snapshot = snapshot or Snapshot(meta=StoreMeta(data_revision=0))
    setup_answers = setup_answers or _default_setup_answers()
    if ai_selection is not None:
        setup_answers = setup_answers.model_copy(
            update={
                "ai_runtime": ai_selection.ai_runtime,
                "claude": apply_ai_selection_to_claude(
                    setup_answers.claude,
                    ai_selection,
                ),
            },
            deep=True,
        )
    ai_providers = ai_providers or []
    scan_history = scan_history or []
    ats_history = ats_history or []
    global_snapshot = global_snapshot or Snapshot(meta=StoreMeta(data_revision=0))
    current_dashboard = build_current_dashboard(snapshot)
    global_dashboard = build_global_dashboard(global_snapshot)
    template = _environment().get_template("setup.html")
    return template.render(
        revision=snapshot.meta.data_revision,
        current_dashboard=current_dashboard,
        global_dashboard=global_dashboard,
        setup=setup_answers,
        ai_providers=ai_providers,
        scan_history=scan_history,
        selected_run_id=selected_run_id,
        ats_history=ats_history,
        selected_ats=selected_ats,
        bootstrap_css=_asset_text("bootstrap.min.css"),
        tom_select_css=_asset_text("tom-select.bootstrap5.min.css"),
        dashboard_css=_asset_text("dashboard.css"),
        console_css=_asset_text("console.css"),
        bootstrap_js=_asset_text("bootstrap.bundle.min.js"),
        tom_select_js=_asset_text("tom-select.complete.min.js"),
        dashboard_js=_asset_text("dashboard.js"),
        console_js=_asset_text("console.js"),
    )


def _default_setup_answers() -> SetupAnswers:
    """Return first-run values when no valid saved configuration exists."""
    # First-run form is intentionally incomplete until the user adds a search term.
    return SetupAnswers.model_construct(
        search_terms=[],
        locations=["Berlin", "Hamburg"],
        german_level="A2",
        claude=ClaudeSettings(model="sonnet", effort="medium", batch_size=10),
        scheduler=SchedulerSettings(local_time=None),
    )
