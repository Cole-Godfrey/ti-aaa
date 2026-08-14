# Changelog

All notable changes are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- an opt-in setting to auto-submit applications that the user explicitly selects
- a local Agent input for employer one-time verification codes that continues the live form
- a Retry action for **Confirm in Agent** checkpoints in the Applications tab
- same-session human browser control for CAPTCHA and blocked-interaction checkpoints, including clicking, typing, pasting, scrolling, and returning the retained tab to the agent
- complete address settings for address line 1/2, ZIP/postal code, city, state/region, county, and country
- a **Qualified** column and filter in Latest jobs, including visible hard-gate reasons
- previous-internship employer facts for returning-intern qualification checks
- required ordinary employer account creation with a stable per-portal credential
- a **Stop session** control in the Agent tab that ends a running application attempt, including a looping CAPTCHA checkpoint, and releases the listing instead of queueing it again
- an **I applied manually** record in the Agent tab, including a list of the roles whose employers block the browser agent or whose session you stopped
- the source lists' advanced-degree marker is now imported and enforced, so master's-, PhD-, and MBA-only roles are **Not qualified** for a bachelor's profile even when the title omits the requirement

### Changed

- the Applications tab opens on submitted applications instead of every ledger row
- a hard qualification mismatch (degree, prior-intern, citizenship, sponsorship, or an agent-discovered conflict) now caps the fit score at 2 instead of leaving a mid-range score beside **Not qualified**
- graduate-only titles are recognized beyond the PhD keyword, including MBA, MFE, DPhil, postdoctoral, "graduate students", and "advanced degree" phrasing
- a stop requested before a restart is honored on the next start instead of resuming the attempt
- applications recorded outside the browser agent are marked as candidate-submitted and are excluded from the application queue
- submission-authorized runs now complete and audit the full form before a separate final-action browser turn
- advanced-degree and previous-company-intern requirements now make a listing ineligible instead of only lowering its fit score
- hard qualification conflicts discovered on an employer page remain ineligible during later sync cycles and are excluded from Auto mode

### Fixed

- an application Auto mode stopped for a missing candidate fact is queued again after the profile supplies that fact, instead of staying permanently benched
- package validation now accepts Core Metadata 2.5 emitted by current Hatchling releases
- the browser agent no longer uses the final Submit control to trigger validation or discover unfinished fields
- disabled or stuck **Submitting…** states without a receipt now become live human-interaction checkpoints instead of closing an intact application form
- address suggestion widgets are completed from the candidate's full configured address and checked field by field

### Security

- one-time verification-code answers are cleared after the browser turn consumes them; user-supplied password and other sensitive-input requests remain blocked
- browser-control messages are accepted only for the currently retained CAPTCHA checkpoint and expose no URL-navigation command
- employer account passwords are derived from a private local key, remain stable per careers portal and candidate email, and are never stored as shared plaintext credentials

## [0.7.0] - 2026-08-07

### Added

- a welcome-back summary of submissions and stopped Auto mode attempts since the prior visit
- a final in-app submission confirmation that continues on the completed live form
- a persisted application queue shown in the Agent tab
- an optional preference gate for Auto mode
- one best-fit automatic application per company
- opt-in Web Push alerts for new jobs entering the Auto-mode queue

### Changed

- all eligible queued jobs can be prepared; preparation no longer has a fit limit
- fit scores measure candidate qualifications and do not use preferences or strict eligibility filters
- Auto mode uses the configured fit limit, submits without user input, and records missing facts as errors
- upgrades leave the new Auto mode off when the old setup did not permit final submission
- repository batches use one browser and are processed one application at a time
- selected resumes are copied without PDF changes and named `First_Last_Resume.pdf`
- API keys are configured through the private `.env` file or environment variables, not the dashboard
- dependency ranges and pinned Claude Code and Playwright MCP releases were refreshed for publication
- public package metadata, install instructions, and third-party data-flow disclosures now point to the GitHub repository

### Removed

- the former application-start browser alerts and all email-alert delivery code
- the shared employer account password; account creation and login now require manual action

### Security

- state-changing dashboard requests and worker WebSockets now reject cross-origin browsers
- the dashboard now rejects untrusted host headers and disables API response caching
- discovery ignores credentialed URLs and links that directly target local or private-network addresses

## [0.6.0] - 2026-08-02

### Added

- a separate fit limit for automatic applications
- an email and browser alert when an application starts

### Changed

- manual Apply actions ignore the automatic fit limit
- the README now has shorter setup and use instructions

## [0.5.0] - 2026-07-28

### Added

- application outcome analytics by resume, role family, source repository, location, and portal
- configurable browser and SMTP email alerts for agent input, submissions, failures, OAs, interviews, and offers
- a persistent DevTools screencast delivered to the Agent view over a local WebSocket

### Changed

- the Agent view now paints frames into a stable canvas and retains the private JPEG endpoint as a fallback

## [0.4.0] - 2026-07-25

### Added

- candidate-input checkpoints on the Agent page for ordinary unanswered application fields
- safe manual-browser handoffs for employer access blocks, CAPTCHAs, verification, and review steps
- persistent availability state that distinguishes repository presence from employer-confirmed closure

### Changed

- the Applications register now contains only submitted applications and records the submitted resume
- live browser snapshots now capture and refresh every 0.5 seconds
- polling is limited to the main README in each of the three explicitly configured repositories

### Fixed

- employer-confirmed closed roles remain closed when they are still present in a repository
- retired source documents are removed from the active feed without deleting application history
- yearless posting dates can no longer be interpreted as implausible future dates

## [0.3.2] - 2026-07-25

### Fixed

- Docker browser workers now recover stale Chromium singleton locks left by a recreated container
- Chrome startup failures now include a concise stderr reason instead of only an exit code
- stale-lock recovery remains disabled for foreign-host native profiles and preserves locks with a live process or socket

## [0.3.1] - 2026-07-25

### Fixed

- browser workers now use schema-validated final statuses instead of relying only on a free-form `RESULT:` line
- missing Claude results now report whether execution, browser permissions, MCP startup, or pre-navigation termination failed
- privacy-safe worker diagnostics retain execution metadata without storing prompts, page text, or candidate data
- browser prompts explicitly require navigation as the first action and a typed result on every normal completion

## [0.3.0] - 2026-07-20

### Added

- latest-jobs repository inbox ordered by posting date
- job detail dossier with source, fit, eligibility, activity, and application boundary
- one-click manual browser-agent requests for any active repository listing

### Changed

- automatic application to newly discovered matches is opt-in and off by default
- first-import listings remain visible and can be explicitly applied to from the web app
- dashboard redesigned as a sharp-edged applications workbench with a top index, ruled ledgers, and slide-out dossiers

## [0.2.1] - 2026-07-20

### Added

- browser-based Claude Code login with an existing Claude Pro or Max subscription
- persistent Docker Claude credentials in the private `tiaaa-data` volume

### Changed

- the Anthropic API key is now an explicitly optional, advanced billing alternative
- the web UI requires either account or API-key authentication only when browser automation is enabled

## [0.2.0] - 2026-07-19

### Added

- always-on background service integrated with the local FastAPI app
- Docker image and Compose service with restart policy, Chromium, Claude Code, and persistent volume
- browser-based onboarding and complete profile/settings/API-key configuration
- multiple resume uploads, deterministic selection, and fact-preserving tailored PDFs
- selected and submitted resume attribution on every tracker row
- live browser-worker states and loopback-only screenshot previews
- public `TIAAA` Python facade and `tiaaa serve` command
- beginner Docker, native website, terminal, and package setup guides

### Changed

- initial repository contents are captured as the starting catalog (manual selection was added in 0.3.0)
- dashboard redesign uses a simple editorial interface and five app sections
- browser automation can run headlessly inside Docker while the dashboard is closed

## [0.1.0] - 2026-07-18

### Added

- GitHub-only polling for the requested Summer 2026/2027 internship repositories
- Markdown and generated-HTML table parsers
- conditional requests, source health, direct-link selection, and cross-source deduplication
- first-import catalog capture and new-listing application queue
- internship authorization, sponsorship, citizenship, role, and location gates
- heuristic and optional LLM scoring
- factual application packet preparation
- isolated Claude Code and pinned Playwright MCP browser workers with an explicit safe-tool allowlist
- explicit review/submission modes with daily and per-cycle caps
- SQLite tracker with OA, interview, offer, rejection, and withdrawal milestones
- Typer CLI and FastAPI web dashboard
- tests, CI, contributor guidance, security policy, AGPL license, and ApplyPilot attribution
