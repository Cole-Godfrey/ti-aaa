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
- The fit score measures candidate qualifications. It does not use role or location preferences.
- Strict role, location, and authorization rules can stop an application. They do not change the fit score.
- You can use preferences as an extra automatic application rule. This rule is off by default.
- Auto mode applies to one best-fit role for each company.
- New job batches stay in a visible queue. One browser applies to them in order.
- A manual **Apply** action ignores the automatic fit limit.
- By default, a manual application stops on the completed form for confirmation in **Agent**. You can opt into auto-submit for jobs you explicitly select.
- The agent completes and audits the form before TI-AAA starts a separate final-submission turn. It does not use the final Submit control to discover missing fields.
- Auto mode does not wait for user input. It submits safe applications and records why it stops others.
- Optional Web Push alerts report new Auto-mode jobs. They require Auto mode and browser permission.
- TI-AAA does not rewrite a resume. It submits an unchanged copy named `First_Last_Resume.pdf`.
- The service continues to work when the dashboard is closed.
- The computer and Docker must stay on.

## Features

- A local web app with guided setup
- An always-on Docker service
- A latest-jobs table with search and job details
- Manual and automatic application modes
- A separate fit limit for automatic applications
- An optional preference gate for automatic applications
- One best-fit automatic application per company
- A visible serial application queue in the Agent tab
- Built-in browser alerts for new Auto-mode queue entries
- Multiple resumes with job-specific selection
- Byte-preserved application resume copies
- A live view of the browser worker
- Same-session browser control for manual CAPTCHA checkpoints—click, type, paste, and scroll without opening a second browser
- Input fields for agent questions
- A one-time-code input that continues the same open application and clears the code after use
- Final submission confirmation on the live completed form
- An application tracker that includes submitted roles and **Confirm in Agent** checkpoints
- A clean Retry action for application checkpoints
- Resume records for each submitted application
- Application, online assessment (OA), interview, and offer statistics
- A welcome-back summary of applications and stopped Auto mode attempts
- A command-line interface (CLI) and a Python API

## Quick start with Docker

Docker is the recommended setup. It includes Python, Chromium, Node.js, Claude Code, and the browser bridge.

### 1. Install Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/). Start Docker before you run TI-AAA.

### 2. Download and start TI-AAA

Run:

```bash
git clone https://github.com/Cole-Godfrey/ti-aaa.git
cd ti-aaa
docker compose up -d --build
```

The first build can take several minutes.

### 3. Open the web app

Open [http://127.0.0.1:8787](http://127.0.0.1:8787).

Complete these setup tasks:

1. Enter your profile and education facts.
2. Upload at least one PDF resume.
3. Connect your Claude account if you want browser automation.

Use **Connect Claude account** with a Claude Pro or Max account. You do not need an Anthropic API key for this method. An Anthropic API key is an optional alternative.

Claude subscription login is a Claude Code compatibility path. Anthropic may change or discontinue subscription login for this workflow in the future; if it stops working, use `ANTHROPIC_API_KEY` instead.

The first repository check creates the baseline. You can close the dashboard after setup. The Docker service continues to poll.

### 4. Set application rules

Open **Settings**. Use these controls:

- **Auto mode** turns unattended applications on or off. It is off by default.
- **Auto-submit manually selected applications** submits after an explicit **Apply** selection without requiring a second final-confirmation click. It is off by default.
- **Minimum qualification fit** controls automatic applications. Its default is 7.
- **Use my preferences as an application gate** can also require your role, location, and term preferences. It is off by default.
- **Notify this browser when new jobs enter the queue** appears only when Auto mode is on. Select it once, allow the browser prompt, then select **Save all settings**. It does not use email or SMS.
- **Daily application cap** limits submissions for one day.
- **Per-cycle cap** limits one poll cycle.

Auto mode submits one best-fit role for each company. It does not ask for user input. If a required personal fact is not available, it stops the application and adds the reason to the next welcome-back summary. Manual **Apply** actions ignore the automatic fit limit.

Browser alerts work on `localhost` and use the browser's Web Push service. TI-AAA creates and stores the required keys in its private data volume. It does not ask you to configure a notification account or key.

### 5. Apply to a job now

Open **Latest jobs**. Select a job, then select **Apply**. Open **Agent** to watch the browser and answer factual questions or paste a one-time verification code if an employer sends one. By default, review the completed form and select **Submit application**. If **Auto-submit manually selected applications** is enabled, the agent submits after completing the form without this second confirmation.

The manual action does not require automatic apply. It also does not use the automatic fit limit.

If the employer presents a CAPTCHA—or Submit stays disabled on **Submitting…** without producing a receipt—the manual application pauses with the exact Chromium tab still open. In **Agent**, use the live canvas to click the challenge, focus and type or paste into fields, and scroll. Select **Continue agent** when the challenge is clear or a receipt is visible. TI-AAA then inspects and continues that same page; it does not send you to a fresh job link or browser where the form state could be lost. Auto mode remains unattended and records CAPTCHA checkpoints as stopped attempts.

If a **Confirm in Agent** checkpoint needs to start over, open **Applications** and select **Retry**. TI-AAA closes any retained live form, clears pending checkpoint inputs, and queues a fresh browser attempt.

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
git clone https://github.com/Cole-Godfrey/ti-aaa.git
cd ti-aaa
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
npm install -g @anthropic-ai/claude-code@2.1.226
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
pip install ti-aaa
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

service:
  enabled: true
  auto_prepare: true

automation:
  auto_apply_new: false
  manual_auto_submit: false
  auto_apply_minimum_fit_score: 7
  auto_apply_use_preferences: false
  allow_submission: false
  max_applications_per_cycle: 5
  max_applications_per_day: 25

```

The dashboard does not show API key fields. Add optional secrets to the private `.env` file in the TI-AAA data directory, or pass them as environment variables.

For Docker, copy the example file in the repository. Then uncomment only the values that you need:

```bash
cp .env.example .env
```

Restart the container after you edit `.env`:

```bash
docker compose up -d
```

For a native install, add the same values to `~/.tiaaa/.env` and restart `tiaaa serve`.

These secrets are optional:

- `ANTHROPIC_API_KEY` for API-based Claude access
- `GITHUB_TOKEN` for a higher GitHub request limit
- `OPENAI_API_KEY` or `GEMINI_API_KEY` for optional scoring and cover letters

## Data and safety

- Docker stores data in the `tiaaa-data` volume.
- Native mode stores data in `~/.tiaaa`.
- TI-AAA has no developer-operated server and sends no product telemetry.
- Repository sync sends requests to GitHub. A configured `GITHUB_TOKEN` is sent only to GitHub.
- Claude browser automation receives the selected resume text, candidate profile, prepared answers, and job metadata. It interacts with the employer site, which receives the fields and files entered for that application.
- During a manual CAPTCHA checkpoint, dashboard mouse and keyboard actions travel only through the local same-origin WebSocket to the retained Chromium tab. TI-AAA does not expose a remote navigation command or send the application URL to another browser.
- A one-time code entered in **Agent** passes through the active Claude browser session to the employer's code field. TI-AAA clears the stored local answer after that browser turn.
- Optional OpenAI, Gemini, or custom LLM preparation sends resume text and job metadata to the provider you configure.
- Optional Web Push sends the company and role in an encrypted notification through your browser vendor's push service.
- The dashboard binds to `127.0.0.1` by default.
- Do not expose the dashboard to the public internet without access control.
- If you use an authenticated reverse proxy, set `TIAAA_TRUSTED_HOSTS` to a comma-separated list of its public hostnames.
- Community internship lists and their outbound links are third-party data. TI-AAA rejects listed links that directly target local/private-network addresses, but you should review the sources and employers before enabling Auto mode.
- The agent does not invent candidate facts.
- Manual applications ask for unknown, ordinary candidate facts and can pause for a one-time code sent to the candidate's configured email address or phone.
- Auto mode does not ask for input. A missing required personal fact stops that application and is recorded.
- Auto mode can answer subjective questions and compensation questions without inventing candidate facts.
- One-time verification codes can be entered in the local Agent view and are cleared after the browser turn uses them. CAPTCHAs can be completed by the candidate in the retained live browser; TI-AAA does not solve or bypass them. SSO, non-code MFA, assessments, and identity checks still require a manual handoff.
- Employer account creation and passwords require manual action; TI-AAA does not store or send an application-site password.
- The agent refuses payment, bank, government ID, and biometric requests.
- Application limits stay active in automatic mode.

Job applications have real effects. Check your facts, limits, employer rules, school rules, and local law.

## Development

```bash
git clone https://github.com/Cole-Godfrey/ti-aaa.git
cd ti-aaa
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
