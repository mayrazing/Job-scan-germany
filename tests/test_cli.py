from typer.testing import CliRunner

from job_scan.cli import app


def test_version_command_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "job-scan 0.1.0"
