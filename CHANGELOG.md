# Changelog

All notable changes are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

- initial repository contents are now an invariant protected baseline with no CLI opt-in escape hatch
- dashboard redesign uses a simple editorial interface and five app sections
- browser automation can run headlessly inside Docker while the dashboard is closed

## [0.1.0] - 2026-07-18

### Added

- GitHub-only polling for the requested Summer 2026/2027 internship repositories
- Markdown and generated-HTML table parsers
- conditional requests, source health, direct-link selection, and cross-source deduplication
- baseline protection and new-listing application queue
- internship authorization, sponsorship, citizenship, role, and location gates
- heuristic and optional LLM scoring
- factual application packet preparation
- isolated Claude Code and pinned Playwright MCP browser workers with an explicit safe-tool allowlist
- explicit review/submission modes with daily and per-cycle caps
- SQLite tracker with OA, interview, offer, rejection, and withdrawal milestones
- Typer CLI and FastAPI web dashboard
- tests, CI, contributor guidance, security policy, AGPL license, and ApplyPilot attribution
