# Changelog

All notable changes are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
