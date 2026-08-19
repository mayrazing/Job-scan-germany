from __future__ import annotations

import json
import os

import pytest

from job_scan.claude_process import ClaudeProcess, ClaudeRequest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOB_SCAN_LIVE_CLAUDE") != "1",
    reason="Set JOB_SCAN_LIVE_CLAUDE=1 to run the live Claude smoke test.",
)


def test_live_claude_maps_one_synthetic_job() -> None:
    request = ClaudeRequest(
        prompt=(
            "Synthetic candidate profile:\n"
            "- Backend Python engineer.\n"
            "- Requires German visa sponsorship.\n\n"
            "Synthetic full job description:\n"
            "Example GmbH seeks a Python backend engineer in Berlin. "
            "English is the working language and German is optional. "
            "The role builds HTTP services and offers visa sponsorship.\n\n"
            "Score this single synthetic job from 0 to 100."
        ),
        json_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100}
            },
            "required": ["score"],
        },
        model="sonnet",
        effort="low",
        timeout_seconds=60,
        max_output_bytes=64 * 1024,
    )

    invocation = ClaudeProcess().invoke(request)

    assert invocation.exit_code == 0
    payload = json.loads(invocation.stdout)
    structured = payload["structured_output"]
    assert type(structured["score"]) is int
    assert 0 <= structured["score"] <= 100
