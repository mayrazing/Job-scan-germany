from __future__ import annotations

import importlib.resources
import ipaddress
import json
import os
import subprocess
import sys
import venv
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fixtures import (
    ELIGIBLE_DESCRIPTION,
    GERMAN_DESCRIPTION,
    RECRUITER_DESCRIPTION,
    UNCERTAIN_DESCRIPTION,
    AcceptanceServers,
    acceptance_servers,
)
from typer.testing import CliRunner

from job_scan import __version__
from job_scan import cli as cli_module
from job_scan.cli import app
from job_scan.config import AppConfig, load_config
from job_scan.dashboard.render import render_dashboard
from job_scan.domain import AIReview, JobRecord, MachineStatus, PrimaryView, UserStatus
from job_scan.http_client import PublicHttpClient
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths
from job_scan.repository import JsonlRepository
from job_scan.scan_service import ScanService
from job_scan.scheduler import SchedulerState
from job_scan.sources.base import SourceAdapter
from job_scan.sources.jobsuche import JobsucheAdapter

PROJECT_ROOT = Path(__file__).parents[2]
SAMPLE_PDF = PROJECT_ROOT / "tests" / "fixtures" / "resume" / "sample.pdf"
FAKE_CLAUDE_DIR = PROJECT_ROOT / "tests" / "fakes"
ORIGIN = "http://127.0.0.1:8765"


class LoopbackHttpClient(PublicHttpClient):
    """Allow fixture HTTP and fail if any source attempts a non-loopback request."""

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname is None:
            raise AssertionError(f"non-fixture source URL requested: {url}")
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise AssertionError(f"non-loopback source URL requested: {url}")


class FakeScheduler:
    """Record scheduler CLI calls without touching cron or launchd."""

    def __init__(self) -> None:
        self.installed = False
        self.local_time: str | None = None
        self.executable: Path | None = None
        self.calls: list[str] = []

    def install(
        self, config: AppConfig, _paths: AppPaths, executable: Path
    ) -> SchedulerState:
        self.calls.append("install")
        self.installed = True
        self.local_time = config.scheduler.local_time
        self.executable = executable
        return self._state()

    def status(self, _paths: AppPaths) -> SchedulerState:
        self.calls.append("status")
        return self._state()

    def remove(self, _paths: AppPaths) -> SchedulerState:
        self.calls.append("remove")
        self.installed = False
        self.local_time = None
        self.executable = None
        return self._state()

    def _state(self) -> SchedulerState:
        return SchedulerState(
            backend="cron",
            installed=self.installed,
            local_time=self.local_time,
            executable=self.executable,
            managed_location="fixture:scheduler",
        )


def _setup_input() -> str:
    values = [
        "backend engineer",
        "Berlin,Hamburg",
        "50",
        "50",
        "50",
        "50",
        "50",
        "B1",
        "10",
        "claude-acceptance",
        "high",
        "10",
        "08:30",
    ]
    return "\n".join(values) + "\n"


def _source_factory(
    paths: AppPaths, servers: AcceptanceServers
) -> Callable[[AppConfig], Sequence[SourceAdapter]]:
    def build(config: AppConfig) -> Sequence[SourceAdapter]:
        client = LoopbackHttpClient(paths.cache_dir, min_interval_seconds=0)
        client._sleep = lambda _delay: None
        return [
            JobsucheAdapter(
                config,
                client,
                request_base_url=f"{servers.jobsuche_url}/pc/v4",
            ),
        ]

    return build


def _claude_records(path: Path, kind: str) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == kind
    ]


def _jobs_by_title(repository: JsonlRepository) -> dict[str, JobRecord]:
    return {job.title: job for job in repository.load().jobs}


def test_fresh_home_meets_all_release_acceptance_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "job-scan-home"
    paths = AppPaths.from_root(home)
    claude_log = tmp_path / "claude.jsonl"
    scheduler = FakeScheduler()
    executable = (tmp_path / "bin" / "job-scan").resolve()
    monkeypatch.setenv("JOB_SCAN_HOME", str(home))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "acceptance")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD_PATH", str(claude_log))
    monkeypatch.setenv("PATH", f"{FAKE_CLAUDE_DIR}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cli_module, "_scheduler_backend_factory", lambda: scheduler)
    monkeypatch.setattr(cli_module, "_scheduler_executable_factory", lambda: executable)
    runner = CliRunner()

    with acceptance_servers() as servers:
        monkeypatch.setattr(
            cli_module,
            "_scan_service_factory",
            lambda service_paths: ScanService(
                service_paths,
                source_factory=_source_factory(service_paths, servers),
            ),
        )

        setup_result = runner.invoke(
            app,
            ["setup", "--resume", str(SAMPLE_PDF)],
            input=_setup_input(),
        )
        assert setup_result.exit_code == 0, setup_result.output
        config = load_config(paths.config_toml)
        assert config.country == "DE"
        assert config.needs_visa_sponsorship is True
        assert config.search_terms == ["backend engineer"]
        assert config.locations == ["Berlin", "Hamburg"]
        assert config.linkedin_limit == 50
        assert config.indeed_de_limit == 50
        assert config.stepstone_de_limit == 50
        assert config.simplify_de_limit == 50
        assert paths.profile_md.is_file()

        doctor_result = runner.invoke(app, ["doctor"])
        assert doctor_result.exit_code == 0, doctor_result.output
        assert "[ok] claude_version" in doctor_result.output
        assert "[ok] claude_auth" in doctor_result.output

        first_scan = runner.invoke(app, ["scan"])
        assert first_scan.exit_code == 0, first_scan.output
        assert "Source occurrences: 4" in first_scan.output
        assert "Source errors: 0" in first_scan.output

        repository = JsonlRepository(
            paths,
            FileRWLock(paths.lock_file),
            render_dashboard,
        )
        jobs = _jobs_by_title(repository)
        assert len(jobs) == 4
        assert jobs["Visa Platform Engineer"].description == ELIGIBLE_DESCRIPTION
        assert jobs["German Security Engineer"].description == GERMAN_DESCRIPTION
        assert jobs["Recruiter Backend Engineer"].description == RECRUITER_DESCRIPTION
        assert jobs["Uncertain Platform Engineer"].description == UNCERTAIN_DESCRIPTION
        assert all(
            occurrence.detail_complete
            for job in jobs.values()
            for occurrence in job.source_occurrences
        )

        eligible = jobs["Visa Platform Engineer"]
        german = jobs["German Security Engineer"]
        recruiter = jobs["Recruiter Backend Engineer"]
        uncertain = jobs["Uncertain Platform Engineer"]
        for accepted in (eligible, german, recruiter, uncertain):
            assert accepted.ai_review is not None
            AIReview.model_validate(accepted.ai_review.model_dump())
        assert eligible.machine_status is MachineStatus.ELIGIBLE
        assert "Visa support" in eligible.labels
        assert german.machine_status is MachineStatus.EXCLUDED
        assert german.exclusion_reasons == [
            "no_visa_sponsorship",
            "citizenship_required",
        ]
        assert "Security clearance" in german.labels
        assert "German required" in german.labels
        assert recruiter.machine_status is MachineStatus.ELIGIBLE
        assert recruiter.ai_review is not None
        assert recruiter.ai_review.score == 82
        assert recruiter.score == 72
        assert "Recruiter" in recruiter.labels
        assert uncertain.machine_status is MachineStatus.UNCERTAIN

        review_records = _claude_records(claude_log, "review")
        for record in review_records:
            argv = record["argv"]
            assert isinstance(argv, list)
            assert "--safe-mode" in argv
            assert "--no-session-persistence" in argv
            tools_index = argv.index("--tools")
            assert argv[tools_index + 1] == ""
        first_review_calls = len(review_records)
        assert first_review_calls == 1
        captured_app: FastAPI | None = None
        captured_kwargs: dict[str, object] = {}

        def fake_uvicorn_run(review_app: FastAPI, **kwargs: object) -> None:
            nonlocal captured_app
            captured_app = review_app
            captured_kwargs.update(kwargs)

        class FakeMdnsPublisher:
            current_ip = "192.168.3.28"

            def start(self) -> str:
                return "192.168.3.28"

            def stop(self) -> None:
                pass

        monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
        monkeypatch.setattr(cli_module, "_mdns_publisher_factory", FakeMdnsPublisher)
        review_result = runner.invoke(app, ["review", "--port", "8765"])
        assert review_result.exit_code == 0, review_result.output
        assert captured_kwargs == {
            "host": "0.0.0.0",
            "port": 8765,
            "access_log": False,
            "reload": False,
        }
        assert captured_app is not None
        with TestClient(captured_app, base_url=ORIGIN) as client:
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            dashboard_soup = BeautifulSoup(dashboard.content, "html.parser")
            for view in PrimaryView:
                assert f'id="{view.value}"'.encode() in dashboard.content
            assert b"Visa Platform Engineer" in dashboard.content
            assert b"Uncertain Platform Engineer" in dashboard.content
            assert b"German Security Engineer" in dashboard.content
            for job in jobs.values():
                assert dashboard.content.count(job.title.encode()) == 1
            assert len(
                dashboard_soup.select(
                    'article[data-job-key] form[data-job-action="status"]'
                )
            ) == len(jobs)
            eligible_card = dashboard_soup.select_one(
                f'article[data-job-key="{eligible.canonical_job_key}"]'
            )
            assert eligible_card is not None
            status_form = eligible_card.select_one(
                'form[data-job-action="status"][data-job-key]'
            )
            assert status_form is not None
            assert "applied" in {
                option.get("value")
                for option in status_form.select('select[name="status"] option')
            }
            german_card = dashboard_soup.select_one(
                f'article[data-job-key="{german.canonical_job_key}"]'
            )
            assert german_card is not None
            restore_form = german_card.select_one(
                'form[data-job-action="restore"][data-job-key]'
            )
            assert restore_form is not None
            assert eligible_card.select_one('form[data-job-action="restore"]') is None
            headers = {"Host": "127.0.0.1:8765", "Origin": ORIGIN}
            status_response = client.post(
                (
                    f"/api/jobs/{quote(str(status_form['data-job-key']), safe='')}"
                    f"/{status_form['data-job-action']}"
                ),
                json={"status": "applied"},
                headers=headers,
            )
            restore_response = client.post(
                (
                    f"/api/jobs/{quote(str(restore_form['data-job-key']), safe='')}"
                    f"/{restore_form['data-job-action']}"
                ),
                headers=headers,
            )
            assert status_response.status_code == 204
            assert restore_response.status_code == 204

        after_mutations = _jobs_by_title(repository)
        assert after_mutations["Visa Platform Engineer"].user_status is UserStatus.APPLIED
        assert after_mutations["German Security Engineer"].manual_override == "show"
        assert after_mutations["German Security Engineer"].machine_status is MachineStatus.EXCLUDED
        assert after_mutations["German Security Engineer"].exclusion_reasons

    install = runner.invoke(app, ["scheduler", "install"])
    status = runner.invoke(app, ["scheduler", "status"])
    remove = runner.invoke(app, ["scheduler", "remove"])
    assert install.exit_code == status.exit_code == remove.exit_code == 0
    assert scheduler.calls == ["install", "status", "remove"]
    assert "Installed: yes" in install.output
    assert "Installed: yes" in status.output
    assert "Installed: no" in remove.output


def test_built_wheel_runs_commands_and_contains_dashboard_assets(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    build_command = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(wheel_dir),
        str(PROJECT_ROOT),
    ]
    build_env = {
        **os.environ,
        "PIP_CACHE_DIR": str(tmp_path / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    assert "--no-isolation" in build_command
    assert build_env.get("PIP_NO_INDEX") == "1"
    build = subprocess.run(
        build_command,
        check=False,
        capture_output=True,
        text=True,
        env=build_env,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_dir)
    scripts = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    command = scripts / ("job-scan.exe" if os.name == "nt" else "job-scan")
    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=build_env,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    version = subprocess.run(
        [str(command), "version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=build_env,
    )
    help_result = subprocess.run(
        [str(command), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=build_env,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"job-scan {__version__}"
    assert help_result.returncode == 0, help_result.stderr
    assert "setup" in help_result.stdout
    assert "scheduler" in help_result.stdout

    asset_check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from importlib.resources import files; "
                "root=files('job_scan.dashboard'); "
                "required=('templates/index.html','static/dashboard.css','static/dashboard.js'); "
                "assert all(root.joinpath(item).is_file() for item in required)"
            ),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=build_env,
    )
    assert asset_check.returncode == 0, asset_check.stderr
    assert importlib.resources.files("job_scan.dashboard").joinpath(
        "templates/index.html"
    ).is_file()
