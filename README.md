# TI-AAA

**Tech Internship Autonomous Application Agent**

TI-AAA is a local app for tech internship applications. It runs in the background, checks fixed GitHub lists, prepares truthful application files, and tracks submitted applications.

TI-AAA does not scrape LinkedIn, Indeed, Glassdoor, Google Jobs, or employer job catalogs. A browser worker opens a job only after a supported GitHub list provides the direct application link.

## Job sources

TI-AAA reads these repositories:

- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships)

## Main rules

- The first sync imports the current jobs as a baseline.
- Automatic apply is off by default.
- Automatic apply considers only jobs that arrive after the baseline.
- The default automatic fit limit is 7 out of 10.
- A job must pass the preparation limit and the automatic fit limit.
- A manual **Apply** action ignores both fit limits.
- Final submission is off by default. The agent stops for review.
- Resume tailoring can reorder facts. It does not add skills, work, or results.
- The service continues to work when the dashboard is closed.
- The computer and Docker must stay on.

## Features

- A local web app with guided setup
- An always-on Docker service
- A latest-jobs table with search and job details
- Manual and automatic application modes
- A separate fit limit for automatic applications
- Multiple resumes with job-specific selection
- Fact-preserving resume tailoring
- A live view of the browser worker
- Input fields for agent questions
- A spreadsheet-style application tracker
- Resume records for each submitted application
- Application, online assessment (OA), interview, and offer statistics
- Browser alerts and email alerts
- A command-line interface (CLI) and a Python API

## Quick start with Docker

Docker is the recommended setup. It includes Python, Chromium, Node.js, Claude Code, and the browser bridge.

### 1. Install Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Start Docker before you run TI-AAA.

### 2. Start TI-AAA

Open a terminal in this repository. Run:

```bash
cd TI-AAA
docker compose up -d --build
```

The first build can take several minutes.

### 3. Open the web app

Open [http://127.0.0.1:8787](http://127.0.0.1:8787).

Complete these setup tasks:

1. Enter your profile and education facts.
2. Upload at least one PDF resume.
3. Connect your Claude account if you want browser automation.
4. Select review mode or submit mode.

Use **Connect Claude account** with a Claude Pro or Max account. You do not need an Anthropic API key for this method. An Anthropic API key is an optional alternative.

The first repository check creates the baseline. You can close the dashboard after setup. The Docker service continues to poll.

### 4. Set the application limits

Open **Settings**. Use these controls:

- **Minimum fit to prepare** controls automatic file preparation. Its default is 5.
- **Minimum fit to auto-apply** controls automatic application claims. Its default is 7.
- **Automatically apply to new matching roles** turns automatic apply on or off.
- **Allow workers to click final Submit** controls final submission.
- **Daily application cap** limits submissions for one day.
- **Per-cycle cap** limits one poll cycle.

If the preparation limit is higher than the automatic limit, the preparation limit controls the result. Manual **Apply** actions ignore the two fit limits.

### 5. Set the start email alert

Open **Settings**, then find **Notifications**.

1. Select **Send email alerts**.
2. Keep **Application started** selected.
3. Enter the destination and sender addresses.
4. Enter the SMTP host, port, security type, and user name.
5. Enter the SMTP password or app password.
6. Save the settings.
7. Select **Send test email**.

The background service sends the start email after a worker claims a job. The dashboard does not need to be open.

### 6. Apply to a job now

Open **Latest jobs**. Select a job, then select **Apply**. Open **Agent** to watch the browser and answer agent questions.

The manual action does not require automatic apply. It also does not use the automatic fit limit.

### Docker commands

Check the service:

```bash
docker compose ps
```

Read the logs:

```bash
docker compose logs -f tiaaa
```

Press `Ctrl+C` to close the log view. This does not stop TI-AAA.

Stop and start the service:

```bash
docker compose stop
docker compose start
```

Update TI-AAA:

```bash
git pull
docker compose up -d --build
```

`docker compose down` keeps the named data volume. `docker compose down -v` deletes the local TI-AAA data. Do not use `-v` unless you want this deletion.

## Native website setup

Use this setup if you do not want Docker.

You need:

- Python 3.11 or later
- Chrome or Chromium
- Node.js 22 or later
- Claude Code for browser automation

Create an environment and install TI-AAA:

```bash
cd TI-AAA
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows PowerShell, use this activation command:

```powershell
.venv\Scripts\Activate.ps1
```

Install Claude Code if you want browser automation:

```bash
npm install -g @anthropic-ai/claude-code@2.1.215
```

Start the website and background service:

```bash
tiaaa serve
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). The native app stores its data in `~/.tiaaa`.

## Terminal use

Create the local files:

```bash
tiaaa init --resume-pdf ./resume.pdf --resume-txt ./resume.txt
```

Edit these files:

```text
~/.tiaaa/profile.json
~/.tiaaa/settings.yaml
~/.tiaaa/.env
```

Check the setup and import the job lists:

```bash
tiaaa doctor
tiaaa sync
```

Run common tasks:

```bash
tiaaa jobs
tiaaa status
tiaaa prepare
tiaaa apply --job-id 42
tiaaa mark 42 --outcome oa
```

Run a continuous foreground worker:

```bash
tiaaa watch --apply
```

Final submission needs the local setting and the `--submit` option:

```yaml
automation:
  allow_submission: true
```

```bash
tiaaa watch --apply --submit
```

## Python API

Install the package in your environment:

```bash
pip install -e ./TI-AAA
```

Use the public API:

```python
from tiaaa.api import TIAAA

agent = TIAAA("./private-tiaaa-data")
agent.add_resume("./resume.pdf", name="Backend", tags=["backend", "python"])
agent.sync()

jobs = agent.jobs(limit=100)
job_id = jobs[0]["id"]
agent.request(job_id)
agent.apply(job_id=job_id, submit=False)

print(agent.stats())
print(agent.analytics())
```

## Main settings

The web app manages these settings. Terminal users can edit `settings.yaml`.

```yaml
poll_interval_seconds: 300
minimum_fit_score: 5

service:
  enabled: true
  auto_prepare: true

automation:
  auto_apply_new: false
  auto_apply_minimum_fit_score: 7
  allow_submission: false
  workers: 1
  max_applications_per_cycle: 5
  max_applications_per_day: 25

notifications:
  email_enabled: false
  email_to: ""
  email_from: ""
  smtp_host: ""
  smtp_port: 587
  smtp_security: starttls
  smtp_username: ""
  events:
    application_started: true
```

The web app stores secret values in a private `.env` file. It does not return secret values through the configuration API.

These secrets are optional:

- `ANTHROPIC_API_KEY` for API-based Claude access
- `GITHUB_TOKEN` for a higher GitHub request limit
- `OPENAI_API_KEY` or `GEMINI_API_KEY` for optional scoring and cover letters
- `TIAAA_APPLICATION_PASSWORD` for an employer application account
- `TIAAA_SMTP_PASSWORD` for email alerts

## Data and safety

- Docker stores data in the `tiaaa-data` volume.
- Native mode stores data in `~/.tiaaa`.
- The dashboard binds to `127.0.0.1` by default.
- Do not expose the dashboard to the public internet without access control.
- The agent does not invent candidate facts.
- Unknown required answers stop the agent for review.
- CAPTCHA, MFA, SSO, assessments, and identity checks require manual action.
- The agent refuses payment, bank, government ID, and biometric requests.
- Application limits stay active in automatic mode.

Job applications have real effects. Check your facts, limits, employer rules, school rules, and local law.

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

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you open a pull request. Read [SECURITY.md](SECURITY.md) to report a security problem.

## Credit

TI-AAA uses ideas and application workflow patterns from [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) by [Pickle-Pixel](https://github.com/Pickle-Pixel). ApplyPilot introduced the open-source pipeline concept, SQLite tracking, and the Claude Code and Playwright application workflow that informed this project.

TI-AAA is a separate project. It is not endorsed by ApplyPilot or by the internship-list maintainers. See [NOTICE](NOTICE).

## License

TI-AAA uses the [GNU Affero General Public License v3.0](LICENSE).
