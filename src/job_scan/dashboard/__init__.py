"""Build the local, derived job-review dashboard."""

from job_scan.dashboard.render import render_dashboard
from job_scan.dashboard.view_model import (
    DashboardGroup,
    DashboardViewModel,
    JobCard,
    build_dashboard,
)

__all__ = [
    "DashboardGroup",
    "DashboardViewModel",
    "JobCard",
    "build_dashboard",
    "render_dashboard",
]
