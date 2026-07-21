# TI-AAA

**Tech Internship Autonomous Application Agent**

TI-AAA is a local, always-on app that watches community-maintained tech-internship repositories, presents their active roles in a searchable inbox, prepares truthful application packets for jobs you select, and optionally applies to future matches within your limits.

It reads only these GitHub repositories:

- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)

It does **not** search or scrape LinkedIn, Indeed, Glassdoor, Google Jobs, or employer job catalogs. A browser worker visits an application only after its direct URL is added to one of the three repositories.

## The important behavior

- The first successful read imports the current active catalog without automatically applying.
- Every imported listing appears under **Latest jobs** and can be opened or explicitly sent to the agent.
- **Automatically apply to new matching roles** is off by default. Enabling it affects roles discovered afterward.
- Manual **Apply with agent** actions work independently of that automatic setting.
- The worker runs in the background. Closing the dashboard tab does not stop it.
- Docker restarts the app unless you explicitly stop it; the computer and Docker still need to be running.

## What is included

- An always-on repository poller with ETag/Last-Modified caching
- Parsers for Markdown and generated HTML internship tables
- Cross-repository normalization and duplicate prevention
- Sponsorship, citizenship, location, role, and fit gates
- Multiple local resumes with deterministic per-role selection
- Fact-preserving resume tailoring that reprioritizes verbatim source lines
- Optional factual cover letters and LLM score refinement
- Isolated Chrome workers driven by Claude Code and Playwright MCP
- Review-only and autonomous-submit modes with per-cycle and daily limits
- A local web app for onboarding, API keys, profile, filters, and automation settings
- A latest-jobs table with job dossiers and explicit manual application actions
- An application tracker with the exact selected/submitted resume
- Total application, OA, interview, offer, and pipeline statistics
- Live worker status and local browser snapshots
- CLI and Python APIs for scripted use

## Beginner setup: Docker + website

This is the recommended setup. You do not need to install Python, Node.js, Chrome, or Claude Code separately.

### 1. Install Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it. On Linux, Docker Engine with the Compose plugin works too.

### 2. Start TI-AAA

From a terminal in this repository:

```bash
cd TI-AAA
docker compose up -d --build
```

The initial image build installs Chromium, Python, Claude Code, and the pinned Playwright browser bridge. It can take a few minutes.

If Docker Desktop stalls at “load metadata” and later reports `error getting credentials`, restart Docker Desktop or repair/sign back into its credential helper; that error occurs before TI-AAA's Dockerfile runs.

Check that it is healthy:

```bash
docker compose ps
docker compose logs -f tiaaa
```

Press `Ctrl+C` to leave the log view. That does not stop the container.

### 3. Open the app

Visit [http://127.0.0.1:8787](http://127.0.0.1:8787).

The onboarding flow asks for:

1. truthful profile and education facts;
2. at least one PDF resume;
3. a Claude Code account connection if you want browser form filling;
4. whether manual application actions should stop for review or permit final submission.

For browser automation, click **Connect Claude account** and sign in with the Claude Pro or Max account you already use for Claude Code. Paste the one-time code from Claude back into TI-AAA. This login is saved in the private Docker volume and survives container restarts; it does **not** require an Anthropic API key or separate API billing. An API key remains available as an advanced alternative.

The first repository check starts independently of the browser and imports the current catalog. You can close the tab after onboarding; polling and enabled browser workers continue inside Docker.

### 4. Add specialized resumes and configure limits

Use **Resumes** to upload versions such as “Backend,” “Frontend,” or “ML.” Tags help selection, but TI-AAA also compares the role with factual text extracted from each PDF.

Use **Settings** for:

- API keys;
- target roles, locations, and exclusions;
- poll interval and fit threshold;
- resume tailoring and optional LLM preparation;
- browser-worker count;
- daily and per-cycle limits;
- optional automatic application to future matching roles;
- review-only versus final submission.

Keys are write-only in the UI. They are stored in the private Docker volume rather than returned by the API.

### 5. Choose a listing and start the agent

Open **Latest jobs**. This is the live catalog from the three repositories, including roles that were already present when TI-AAA first started.

1. Search or filter the table, then select a company or **Details**.
2. Review the location, fit explanation, eligibility note, source, and application boundary in the dossier.
3. Select **Apply with agent** (or **Apply** in the table) and confirm.
4. Open **Agent** to follow browser status and the latest local snapshot. The worker continues if you close the dashboard.
5. In review mode, finish the final submission yourself. In submit mode, TI-AAA may submit only after you enable that boundary in Settings.

Nothing is applied to automatically by default. If you later enable **Automatically apply to new matching roles**, it applies only to matching roles discovered after you enable it; every catalog role remains available for an explicit manual request.

### Everyday Docker commands

```bash
# See status
docker compose ps

# Follow logs
docker compose logs -f tiaaa

# Restart the process when troubleshooting (web settings apply live)
docker compose restart tiaaa

# Stop the agent
docker compose stop

# Start it again
docker compose start

# Update after pulling a new release
git pull
docker compose up -d --build
```

`docker compose down` removes the container but keeps the named `tiaaa-data` volume. **Do not run `docker compose down -v` unless you intend to delete the database, profiles, keys, resumes, and browser state.**

To make a simple backup while the container exists:

```bash
docker cp tiaaa:/data ./tiaaa-data-backup
```

## Native website setup

Use this if you prefer to run the app directly on macOS, Linux, or Windows.

### Requirements

- Python 3.11+
- Chrome or Chromium
- Node.js 22+
- Claude Code for browser application automation

Discovery, the dashboard, resume selection, and tracking do not need Claude Code. Browser form filling does.

### Install

```bash
cd TI-AAA
python3 -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
```

Install Claude Code if you want browser workers:

```bash
npm install -g @anthropic-ai/claude-code@2.1.215
```

You can use an existing Claude Code login or connect from TI-AAA's web onboarding. To authenticate directly in a terminal instead:

```bash
claude auth login --claudeai
claude auth status --text
```

Then launch the website and background service together:

```bash
tiaaa serve
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) and complete onboarding. `tiaaa serve` creates `~/.tiaaa` automatically on a fresh installation.

To run the tracker UI without polling:

```bash
tiaaa dashboard --no-background
```

## Terminal-only setup and use

The website is the easiest way to manage multiple resumes and secrets, but all core workflows remain available from the terminal.

Create editable files, optionally importing a default resume pair:

```bash
tiaaa init --resume-pdf ./resume.pdf --resume-txt ./resume.txt
```

Then edit:

```text
~/.tiaaa/profile.json
~/.tiaaa/settings.yaml
~/.tiaaa/.env
```

The legacy `resume.pdf` + `resume.txt` pair is registered as the first resume automatically. Additional versions are easiest to upload through the web app.

Validate and populate the current repository catalog:

```bash
tiaaa doctor
tiaaa sync
```

Run bounded commands:

```bash
tiaaa score                   # inspect heuristic scores
tiaaa score --llm             # optional API-backed score refinement
tiaaa prepare                 # select/tailor resumes for newly queued jobs
tiaaa apply --limit 3         # fill, but stop before final Submit
tiaaa apply --job-id 42       # request + prepare + fill one current catalog role
tiaaa status                  # conversion statistics
tiaaa jobs                    # tracker rows
tiaaa mark 42 --outcome oa    # record an OA
```

Run continuously in the foreground:

```bash
tiaaa watch                   # sync + prepare new additions
tiaaa watch --apply           # also fill for review
```

Final submission requires both the local setting and the explicit terminal flag:

```yaml
automation:
  allow_submission: true
```

```bash
tiaaa watch --apply --submit
```

For a scheduler, use one bounded cycle:

```bash
tiaaa watch --once
```

### CLI reference

| Command | Purpose |
| --- | --- |
| `tiaaa serve` | Run the website and always-on agent together |
| `tiaaa dashboard` | Run the dashboard; polling is on by default |
| `tiaaa init` | Create/import local terminal configuration |
| `tiaaa doctor` | Check profile, resumes, and browser tools |
| `tiaaa sources` | Show fixed GitHub sources and their health |
| `tiaaa sync` | Poll repositories and refresh the local catalog |
| `tiaaa run` | One sync → optional score → prepare cycle |
| `tiaaa watch` | Continuously poll and prepare later additions |
| `tiaaa prepare` | Select and optionally tailor resumes |
| `tiaaa apply` | Fill a bounded application batch |
| `tiaaa status` | Show counts and OA/interview rates |
| `tiaaa jobs` | List tracker rows |
| `tiaaa mark` | Record a pipeline or outcome update |

## Use as a Python package

Install it into your environment:

```bash
pip install -e ./TI-AAA
```

Use the bounded public facade:

```python
from tiaaa.api import TIAAA

agent = TIAAA("./private-tiaaa-data")

# Edit agent.profile / agent.settings copies, then persist with:
profile = agent.profile
profile["personal"]["full_name"] = "Avery Student"
agent.configure(profile=profile)

# Add one or more truthful resume versions.
agent.add_resume("./resume.pdf", name="Backend", tags=["backend", "python"])

# The first call imports the current catalog without automatic application.
sync_results = agent.sync()

# Inspect the catalog, then explicitly request one role by its tracker ID.
catalog = agent.jobs(limit=100)
job_id = catalog[0]["id"]
agent.request(job_id)  # selects and fact-safely tailors the best resume
application = agent.apply(job_id=job_id, submit=False)

print(agent.stats())
print(agent.jobs(status="manual_review"))
```

`TIAAA.sync()` intentionally has no bulk "apply to everything" argument. Current roles are selected individually from the web inbox.

## How resume selection and tailoring work

For each user-selected or automatically queued role, TI-AAA ranks active resumes using role/category tokens, user tags, and factual resume text. The selected resume ID and packet path are saved on the tracker row.

When tailoring is enabled, TI-AAA creates an ATS-friendly PDF that:

- copies high-relevance lines verbatim into a “Relevant highlights” section;
- retains the remaining source content;
- never creates a new skill, employer, metric, project, course, or credential.

This deliberately favors truthfulness over prose generation. Disable `preparation.tailor_resumes` if you want the original uploaded PDF submitted unchanged. Optional cover letters are separately prompt-constrained to the profile and selected resume; inspect them in review-only mode before trusting autonomous submission.

After an application is marked applied, the tracker freezes both `submitted_resume_id` and `submitted_resume_path`, so later resume-library changes do not erase what was sent.

## Live application view

Each isolated browser worker publishes:

- current state (`starting`, `applying`, review, failure, or idle);
- company and role;
- a short status message;
- a low-frequency local screenshot.

The **Agent** screen refreshes those snapshots about every 2.5 seconds. This is an observation view, not remote browser control. A snapshot can contain form answers and personal details, so files remain in the local data directory and the Docker port is bound to `127.0.0.1` by default.

## Configuration and API keys

Everything needed for normal use is editable in **Settings**. Native/terminal users can edit the matching files.

Important settings:

```yaml
poll_interval_seconds: 300
minimum_fit_score: 5

service:
  enabled: true
  auto_prepare: true

preparation:
  use_llm: false
  generate_cover_letters: true
  tailor_resumes: true

automation:
  auto_apply_new: false
  allow_submission: false
  workers: 1
  max_applications_per_cycle: 5
  max_applications_per_day: 25
  max_attempts: 3
  claude_model: sonnet
  headless: false
```

Supported secret fields in the web app:

- Claude Code account login — recommended for browser workers; works with an existing Claude Pro or Max subscription and is managed from the web app
- `ANTHROPIC_API_KEY` — optional advanced alternative that uses Anthropic API billing
- `GITHUB_TOKEN` — optional extra GitHub request allowance
- `GEMINI_API_KEY` or `OPENAI_API_KEY` — optional scoring and cover letters
- `TIAAA_APPLICATION_PASSWORD` — optional employer-owned ATS account creation

API keys are not required for GitHub discovery, heuristic scoring, resume ranking, the tracker, or dashboard analytics. An Anthropic API key is also unnecessary for browser automation when Claude Code is connected to a Claude subscription.

## Pipeline and data model

```text
three GitHub repositories
          │
          ▼
 import current catalog → poll later additions
          │
          ▼
 parse → normalize → deduplicate → eligibility → score
                                                │
                                                ▼
                         browse / manual Apply / optional auto-new
                                                │
                                                ▼
                              select + fact-safe tailor resume
                                                │
                              ┌─────────────────┴─────────────────┐
                              ▼                                   ▼
                       review-only fill                    bounded submit
                              │                                   │
                              └─────────────────┬─────────────────┘
                                                ▼
                              SQLite tracker + local web app
```

TI-AAA follows five documents across the three repositories: the main sndsh404 list, main/off-season Vansh lists, and main/off-season Simplify lists. When generated tables offer both a direct employer/ATS link and a service tracking link, the parser prefers the direct link.

If a row disappears from every successfully fetched active source, it becomes expired unless already submitted or withdrawn. A source failure does not expire its jobs.

OA and interview rates use submitted applications as the denominator. Milestones are retained independently, so moving from OA to interview does not erase the OA event.

## Privacy and safety

- Native data stays under `~/.tiaaa`; Docker data stays in the `tiaaa-data` volume.
- Uploaded PDFs, extracted text, generated packets, snapshots, the database, and `.env` use private permissions where the OS supports them.
- Secret values are never returned by the configuration API.
- The dashboard binds to loopback by default and has no user authentication. Do not expose port 8787 publicly without an authenticated reverse proxy.
- Debug agent output is off by default because form output can contain personal data.
- Browser prompts treat webpages as untrusted and prohibit invented facts.
- CAPTCHAs, MFA, SSO, email verification, assessments, and unknown required answers route to manual review.
- The worker refuses payment, banking, SSN, government-ID, biometric, camera, microphone, screen-share, and device-location requests.
- Claude Code receives only an explicit Playwright interaction allowlist—no shell, coding, or arbitrary browser-code tools.
- Daily and per-cycle caps remain active in autonomous mode.

Job applications are consequential. Review your facts and limits carefully, and comply with employer terms, applicable law, university policy, and upstream repository rules.

## Development

```bash
cd TI-AAA
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check src tests
python -m build
```

The source layout:

```text
src/tiaaa/
├── apply/           Chrome, live previews, prompt, and worker orchestration
├── dashboard/       FastAPI API and dependency-free web app
├── discovery/       GitHub-only fetcher and repository table parsers
├── api.py           public Python facade
├── config.py        fixed sources, paths, profile/settings/secrets
├── database.py      SQLite catalog, manual/automatic queue, tracker, and analytics
├── eligibility.py   internship filters and heuristic scoring
├── preparation.py   score and application-packet preparation
├── resumes.py       upload, extraction, selection, and safe tailoring
└── service.py       always-on background lifecycle
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Attribution

TI-AAA is a new internship-focused project adapted from architectural ideas and application workflow patterns in [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot), created by [Pickle-Pixel](https://github.com/Pickle-Pixel). ApplyPilot deserves explicit credit for the original open-source pipeline concept, staged SQLite tracking, and Claude Code/Playwright application approach that informed this project.

TI-AAA replaces broad job-board discovery with the fixed GitHub internship pipeline above and adds a browsable repository inbox, explicit manual and opt-in automatic application modes, a persistent background service, multi-resume selection/tailoring, and a local app dashboard. It is not affiliated with or endorsed by ApplyPilot or the three internship-repository maintainers. See [NOTICE](NOTICE).

## License

TI-AAA is licensed under the [GNU Affero General Public License v3.0](LICENSE), consistent with its ApplyPilot lineage. Network deployments of modified versions must make the corresponding source available under the AGPL.
