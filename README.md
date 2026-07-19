# TI-AAA

**Tech Internship Autonomous Application Agent**

TI-AAA watches community-maintained GitHub internship lists, identifies newly added roles, prepares factual application packets, fills applications with a browser agent, and tracks every result in a local web dashboard.

It is purpose-built for computer science and adjacent technology internships. Discovery is restricted to these repositories:

- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)

TI-AAA does **not** search or scrape LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Workday catalogs, or other job boards. It opens a direct application URL only after that URL appears in one of the configured GitHub lists.

> Job applications are consequential. Review your profile and resume carefully. By default, the browser fills forms but stops before the final submission. Autonomous submission requires an explicit setting and an explicit CLI flag.

## What it includes

- Conditional polling with ETag and Last-Modified support
- Parsers for both Markdown tables and generated HTML tables
- Cross-repository URL normalization and duplicate prevention
- First-sync baseline protection, so existing lists do not trigger a mass application run
- Internship eligibility gates for sponsorship, citizenship, role keywords, and location
- Transparent heuristic fit scoring with optional LLM refinement
- Resume attachment and optional fact-constrained cover-letter preparation
- Isolated Chrome workers driven by Claude Code and Playwright MCP
- A Typer CLI for one-shot, scheduled, and continuous operation
- A local FastAPI dashboard with:
  - application tracker
  - total application count
  - online-assessment rate
  - interview rate
  - offers and pipeline counts
  - recently applied internships
  - editable pipeline and outcome states

## Pipeline

```text
GitHub README files
       │
       ▼
parse → normalize → deduplicate → eligibility → score
                                               │
                                               ▼
                                    prepare application packet
                                               │
                                  ┌────────────┴────────────┐
                                  ▼                         ▼
                            browser review             final submit
                                  │                         │
                                  └────────────┬────────────┘
                                               ▼
                                      SQLite + dashboard
```

The discovery boundary is intentional: TI-AAA downloads raw files from the three GitHub repositories and does not enumerate external career sites. The application worker later visits only the direct URL attached to a selected row.

## Requirements

Core discovery and dashboard:

- Python 3.11 or newer
- Internet access to `raw.githubusercontent.com`

Browser application workflow:

- Google Chrome or Chromium
- Node.js 18 or newer with `npx`
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)

Optional AI scoring and cover letters:

- Gemini, OpenAI, or an OpenAI-compatible local endpoint

## Installation

```bash
cd TI-AAA
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e .
```

For development:

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

## Quick start

Create the local data directory and editable templates:

```bash
tiaaa init
```

Then edit:

```text
~/.tiaaa/profile.json
~/.tiaaa/settings.yaml
~/.tiaaa/.env
```

Add both forms of your resume:

```text
~/.tiaaa/resume.txt   # fact source for scoring and form answers
~/.tiaaa/resume.pdf   # recruiter-ready file uploaded to applications
```

Validate the setup and establish a baseline:

```bash
tiaaa doctor
tiaaa sync
```

The first successful fetch of each source is a **baseline**. Existing rows are tracked but not queued. Later additions are queued automatically. To deliberately queue current listings too:

```bash
tiaaa sync --include-existing
```

Prepare the queue and open the dashboard:

```bash
tiaaa prepare
tiaaa dashboard
```

Run a review-only browser batch:

```bash
tiaaa apply --limit 3
```

## Autonomous watching

Watch for additions and prepare them continuously:

```bash
tiaaa watch
```

Fill each new application but stop before submission:

```bash
tiaaa watch --apply
```

To submit automatically, enable the local setting:

```yaml
automation:
  allow_submission: true
```

Then provide the second opt-in on the command line:

```bash
tiaaa watch --apply --submit
```

Both controls are required. Daily and per-cycle caps in `settings.yaml` remain active.

For cron, launchd, systemd, or GitHub Actions runners, use one bounded cycle:

```bash
tiaaa watch --once
```

## CLI reference

| Command | Purpose |
| --- | --- |
| `tiaaa init` | Create the local profile, settings, environment, and database |
| `tiaaa doctor` | Validate required files and optional browser tools |
| `tiaaa sources` | Show the fixed GitHub discovery documents and health |
| `tiaaa sync` | Poll and reconcile the repository lists |
| `tiaaa score` | Show heuristic scores |
| `tiaaa score --llm` | Refine queued scores with an optional LLM |
| `tiaaa prepare` | Attach the resume and generate optional cover letters |
| `tiaaa run` | Run one sync → score → prepare cycle |
| `tiaaa apply` | Fill a bounded browser batch, stopping before submit |
| `tiaaa apply --submit` | Submit a bounded batch if settings also allow it |
| `tiaaa watch` | Poll continuously and process only new additions |
| `tiaaa status` | Show counts and conversion rates in the terminal |
| `tiaaa jobs` | List tracker rows |
| `tiaaa mark ID --outcome interview` | Record a tracker milestone |
| `tiaaa dashboard` | Serve the local web dashboard |

## Configuration

### Profile

`profile.json` contains the facts the application agent may use:

- contact information and portfolio links
- school, degree, major, graduation date, and GPA
- work authorization, sponsorship, and citizenship
- preferred roles, locations, and internship terms
- technical skills
- common screening answers
- voluntary EEO defaults

Do not add a claim unless it is true and supported by your resume or circumstances. When a required form answer is missing, the agent routes the application to `manual_review` instead of guessing.

### Settings

Important controls in `settings.yaml`:

```yaml
poll_interval_seconds: 300
minimum_fit_score: 5

filters:
  include_role_keywords: []
  exclude_keywords: []
  allowed_locations: []
  remote_only: false

preparation:
  use_llm: false
  generate_cover_letters: true

automation:
  allow_submission: false
  max_applications_per_cycle: 5
  max_applications_per_day: 25
  max_attempts: 3
  claude_model: sonnet
  headless: false
  timeout_seconds: 600
```

Empty filter lists accept the scope already curated by the upstream repositories.

### Environment

Copy the relevant values into `~/.tiaaa/.env`:

```dotenv
GITHUB_TOKEN=
GEMINI_API_KEY=
# OPENAI_API_KEY=
# LLM_URL=http://127.0.0.1:11434/v1
# LLM_MODEL=

# Optional password for ordinary employer-owned ATS account creation.
TIAAA_APPLICATION_PASSWORD=
```

API keys are not required for discovery, scoring heuristics, the tracker, or the dashboard.

The application worker pins `@playwright/mcp` to a reviewed release and starts Claude Code with
strict MCP loading, no built-in coding tools, and an explicit browser-tool allowlist. Set
`TIAAA_PLAYWRIGHT_MCP_PACKAGE` only when deliberately testing a compatible Playwright MCP update.

## Discovery behavior

TI-AAA currently follows five active documents within the three requested repositories:

- the main list from `sndsh404/summer-2027-internships`
- the main and off-season lists from `vanshb03/Summer2027-Internships`
- the main and off-season lists from `SimplifyJobs/Summer2026-Internships`

For generated tables that include a direct Apply link and a tracking/service link, TI-AAA prefers the direct employer/ATS URL. Known campaign parameters are removed while job identifiers are preserved. Jobs are deduplicated by canonical URL and a normalized company/role/location fingerprint.

When a row disappears from every successfully fetched active source, it is marked expired unless it was already submitted or withdrawn. A temporary source failure does not expire its rows.

## Dashboard

`tiaaa dashboard` binds to `127.0.0.1:8787` by default. It exposes a local JSON API under `/api` and interactive documentation under `/api/docs`.

The dashboard has no authentication because it is local-first. If you bind it to a non-loopback address, protect it with a firewall or authenticated reverse proxy.

Outcome rates use submitted applications as the denominator:

- **OA rate** = applications with an `oa_at` milestone / submitted applications
- **Interview rate** = applications with an `interview_at` milestone / submitted applications

Milestones are retained independently, so moving an application from OA to interview does not erase the OA event.

## Safety and privacy

- All profile data, resumes, application state, and logs stay under `~/.tiaaa` unless `TIAAA_HOME` is changed.
- The dashboard listens on loopback by default.
- Debug agent output is disabled by default because form output can contain personal data.
- The browser agent refuses requests for payment, banking data, SSN, government ID, biometrics, camera, microphone, screen sharing, or device location.
- CAPTCHAs, MFA, SSO, email verification, unknown required questions, and assessments are routed to manual review.
- The prompt requires literal, truthful answers and forbids invented experience or credentials.
- Claude Code receives only an explicit subset of Playwright interaction tools; arbitrary code,
  shell, filesystem, and unsafe Playwright code-execution tools are not available to the worker.

You are responsible for complying with employer terms, applicable law, university policies, and any limits imposed by the upstream list maintainers or application systems.

## Development

The package uses a `src/` layout. The primary modules are:

```text
src/tiaaa/
├── discovery/       GitHub fetcher and table parsers
├── apply/           Chrome, Playwright MCP prompt, and worker orchestration
├── dashboard/       FastAPI API and dependency-free frontend
├── database.py      SQLite schema, queue claims, tracker, and analytics
├── eligibility.py   internship filters and heuristic scoring
├── preparation.py   optional LLM scoring and application packets
└── cli.py           command-line workflows
```

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security issues should follow [SECURITY.md](SECURITY.md).

## Attribution

TI-AAA is a new, internship-focused project adapted from architectural ideas and application workflow patterns in [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), created by [Pickle-Pixel](https://github.com/Pickle-Pixel). ApplyPilot deserves explicit credit for the original open-source pipeline concept, SQLite stage tracking, Claude Code/Playwright application approach, and worker isolation patterns that informed this project.

TI-AAA replaces ApplyPilot's broad job-board discovery with the GitHub-only internship source pipeline described above and adds a persistent web tracker and internship conversion analytics. TI-AAA is not affiliated with or endorsed by the maintainers of ApplyPilot or the three upstream internship repositories.

See [NOTICE](NOTICE) for attribution details.

## License

TI-AAA is licensed under the [GNU Affero General Public License v3.0](LICENSE), consistent with its ApplyPilot lineage. Network deployments of modified versions must make the corresponding source available under the AGPL.
