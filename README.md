# job-scan-germany

`job-scan` is a local, Germany-only job discovery and review tool. It reads public job sources plus LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, and Simplify through the user's connected Chrome session, deduplicates listings, asks either the locally installed Claude Code CLI or a configured Anthropic-compatible API model to create a factual profile and review complete job descriptions, then publishes a local JSONL store and HTML dashboard.

## Scope and assumptions

- Python 3.11 or newer on Linux or macOS. The data lock requires POSIX `flock`; native scheduling supports Linux cron and macOS launchd.
- Linux LAN access requires a running Avahi daemon plus `ip` and `avahi-publish-address`. `job-scan review` uses them to publish `job-scan-germany.local`.
- `country` is fixed to `DE` and `needs_visa_sponsorship` is fixed to `true` by `setup`.
- Public adapters do not log in, solve CAPTCHA, or bypass access controls. LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, and Simplify reuse the user's existing read-only Chrome sessions through OpenCLI and never receive the user's passwords.
- Every source is keyword-driven. Per-company career-page scraping was removed; there is no way to target one company's careers page.

## Install

Create and activate a virtual environment first:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Editable install from a checkout:

```bash
python -m pip install -e .
```

Install a built wheel:

```bash
python -m pip install /path/to/job_scan_germany-0.1.0-py3-none-any.whl
```

Claude Code must be installed and authenticated separately when it is the selected AI runtime. Confirm it before setup:

```bash
claude --version
claude auth status
```

LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, and Simplify discovery require OpenCLI and its Browser Bridge extension. LinkedIn must already be logged in, and the source pages must be accessible without a pending browser challenge in the connected Chrome profile. Chrome must remain open while a scan runs.

## Data directory

The default data root is `~/.job-scan`. Override it for every command and scheduler entry with `JOB_SCAN_HOME`:

```bash
export JOB_SCAN_HOME=/path/to/job-scan-data
```

Important paths under the data root:

- `config.toml`: validated scan, Claude, and scheduler settings.
- `ai-config.toml`: Anthropic-compatible API endpoints, model names, and API keys. The file is written with mode `0600`.
- `profile.md`: factual profile derived from the resume.
- `output/jobs.jsonl`: authoritative job data for the latest search only.
- `output/index.html`: dashboard derived from the matching JSONL revision.
- `history/<run-id>/`: one independent browser-search bundle containing its resume,
  profile, configuration, jobs, and history metadata.
- `cache/`: bounded HTTP cache.
- `logs/scan.jsonl`: privacy-bounded scan summaries.
- `logs/scheduler.log`: cron or launchd output.
- `logs/doctor.jsonl`: check names and statuses, written only by `doctor --log`.

Keep the data root private. It contains resume-derived profile text and complete job descriptions.

## First run

On Linux, start the local server and open the published Setup URL for the browser flow:

```text
$ job-scan review --port 8765
Setup: http://job-scan-germany.local:8765/setup
LAN fallback: http://192.168.3.28:8765
```

The displayed fallback address is the server's current LAN IPv4 address and will differ by network. The Setup page can save and activate Anthropic-compatible API configurations. Advanced settings selects either Claude Code CLI or the active API model. The page then saves the uploaded resume and validated settings, reconciles the optional daily schedule, runs the real scan, and opens the Review step after publication.

On Linux, `review` listens on the server's network interfaces and publishes `job-scan-germany.local` through mDNS. It checks the active LAN IPv4 address every two seconds and republishes the hostname after an address change. `Ctrl-C` stops both the HTTP server and the project-owned mDNS publisher. Devices must be on the same LAN broadcast domain and support mDNS; the hostname is not a public Internet address. Client isolation, separate VLANs, or a host firewall can still block access. On macOS, `review` keeps its existing loopback-only `127.0.0.1` service; this Avahi-based LAN hostname feature is not enabled.

macOS output remains:

```text
Setup: http://127.0.0.1:8765/setup
```

The CLI setup flow remains available:

`setup` accepts a text-based PDF or DOCX and prompts for all settings. This illustrative transcript uses synthetic data; digest values are abbreviated:

Leave `Locations` empty to search all of Germany.

Leave `Daily local scan time` empty for manual scans only. Add a time later with `scheduler install --time HH:MM`.

```text
$ export JOB_SCAN_HOME=/path/to/job-scan-data
$ job-scan setup --resume /path/to/cv.pdf
Search terms (comma-separated): Backend Engineer,Platform Engineer
Locations (comma-separated): Berlin,Hamburg
LinkedIn jobs per search (0 disables, max 100) [10]: 10
Indeed Deutschland jobs per search (0 disables, max 100) [10]: 10
StepStone jobs per search (0 disables, max 100) [10]: 10
Glassdoor DE jobs per search (0 disables, max 100) [10]: 10
Simplify DE jobs per search (0 disables, max 100) [10]: 10
German certificate or level: A2
Claude model: sonnet
Claude effort (low/medium/high): medium
Claude batch size: 10
Daily local scan time (HH:MM, blank for manual scans only):
Profile: /path/to/job-scan-data/profile.md
Profile hash: sha256:...
Config: /path/to/job-scan-data/config.toml
Resume hash: sha256:...
$ job-scan doctor
$ job-scan scan
$ job-scan review --port 8765
Setup: http://job-scan-germany.local:8765/setup
LAN fallback: http://192.168.3.28:8765
```

Open `http://job-scan-germany.local:8765/setup` while `review` is running.

## Commands

```bash
job-scan setup --resume /path/to/cv.pdf
job-scan doctor
job-scan doctor --log
job-scan scan
job-scan scan --force-review
job-scan review
job-scan review --port 9123
job-scan scheduler install
job-scan scheduler install --time 07:15
job-scan scheduler status
job-scan scheduler remove
job-scan version
```

- `setup` extracts the resume, sends the private prompt to the selected AI runtime, and atomically writes `profile.md` plus `config.toml`.
- `doctor` performs bounded readiness checks without fetching jobs or reading resume text. Warnings do not fail the command; errors exit with status 1. `--log` appends names and statuses only.
- `scan` starts from an empty job set, fetches all configured sources, reviews this run's jobs with complete descriptions, replaces the latest JSONL/dashboard snapshot, and appends a bounded run summary. Browser runs also archive an independent history bundle.
- `review` rebuilds the dashboard from JSONL, serves Setup plus Review at `/setup`, and maintains the LAN-only `job-scan-germany.local` mDNS publication. Review includes independent search history with resume download, result viewing, and deletion. The browser setup flow reuses the same setup, scan, scheduler, repository, and locking services as the CLI.
- `scheduler install`, `remove`, and `status` select cron on Linux and launchd on macOS. Install and removal are idempotent. `install` requires either a saved time or `--time HH:MM`; `--time` saves the value in `config.toml`. `remove` also clears the saved time. Installation records the current OpenCLI executable and runtime `PATH`, so the scheduled process can find OpenCLI's Node runtime.

## AI privacy boundary

`job-scan` starts the locally installed `claude` executable with tools disabled, safe mode, no session persistence, bounded runtime/output, and private prompt input on stdin. Setup sends extracted resume text and preferences. Review sends `profile.md` plus complete job descriptions selected for review. Prompts, raw Claude stdout/stderr, resume text, and full job descriptions are excluded from operational logs.

When an Anthropic-compatible API runtime is selected, the same prompt data is sent directly to its configured HTTPS endpoint. API keys remain in the local `ai-config.toml` and are not returned by the local HTTP API or included in operational logs.

This boundary does not make AI processing offline. Authentication, network use, and provider-side data handling remain governed by the selected Claude Code or API provider configuration and terms. Review those before supplying personal data.

## Non-goals

This project does not provide:

- searches outside Germany or profiles that do not assume visa sponsorship is needed;
- a cloud service, account system, database, or public Internet exposure;
- automatic applications, recruiter contact, or email/calendar integration;
- credential submission, CAPTCHA solving, or access-control bypass; LinkedIn, Indeed Deutschland, StepStone, Glassdoor Deutschland, and Simplify only reuse the user's existing read-only browser sessions;
- OCR for scanned PDFs;
- a second factual store in HTML. `output/index.html` is always derived from `output/jobs.jsonl`.
