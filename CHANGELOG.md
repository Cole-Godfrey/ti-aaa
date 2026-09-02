# Changelog

All notable changes are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.1] - 2026-09-01

### Added

- an **Apply: Yes** filter in Latest jobs that shows only listings the review recommends applying to
- the exact active resume recommended for every **Yes** decision, shown directly in Latest jobs and
  in the listing's review details
- a resume chooser when recording an application submitted manually, including the review's
  recommendation and its reason for reference

### Changed

- the basic Docker install is now at the top of the README
- an **Apply** review is stored only when Claude chooses and explains one of the active resumes

### Fixed

- manually recorded applications no longer silently inherit the resume prepared by the agent; the
  submitted resume is the one the user chooses, or is explicitly left unrecorded

## [0.8.0] - 2026-09-01

### Added

- an **Apply?** decision on every listing, answering Yes or No from the employer's own job posting;
  selecting the answer opens the reasoning, the blockers, the resume the agent chose, and how the
  company's applications were allocated
- employer postings are read directly from Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
  schema.org `JobPosting` markup, and ordinary careers pages
- a per-company application limit (2 by default) so a large employer's near-identical listings
  cannot absorb the whole application allowance; submitted applications and forms still awaiting your
  Submit confirmation both hold a slot, and roles handed back for you to apply to yourself do not
- an age window (2 days by default, 0 to disable) so the first sync reviews new listings instead of a
  repository's entire backlog; older rows say why they were skipped and can still be reviewed on
  demand
- one review call per company, so its open roles are ranked against each other rather than judged in
  isolation
- resume selection made by comparing every uploaded resume against the posting, replacing the keyword
  overlap score
- a **Re-check this listing** action, a `POST /api/jobs/{id}/review` endpoint, `tiaaa review`, and
  `TIAAA.review()`
- a **Retry today's reviews** action in Latest jobs that reviews only internships first discovered
  during the browser's current calendar day that still have no Yes/No answer; existing decisions are
  preserved
- a `review` settings section covering the model, the per-company budget, the refresh window, the
  per-cycle company cap, and whether postings are read at all
- an opt-in setting to auto-submit applications that the user explicitly selects
- a local Agent input for employer one-time verification codes that continues the live form
- a Retry action for **Confirm in Agent** checkpoints in the Applications tab
- same-session human browser control for CAPTCHA and blocked-interaction checkpoints, including clicking, typing, pasting, scrolling, and returning the retained tab to the agent
- complete address settings for address line 1/2, ZIP/postal code, city, state/region, county, and country
- previous-internship employer facts for returning-intern qualification checks
- required ordinary employer account creation with a stable per-portal credential
- a **Stop session** control in the Agent tab that ends a running application attempt, including a looping CAPTCHA checkpoint, and releases the listing instead of queueing it again
- an **I applied manually** record in the Agent tab, including a list of the roles whose employers block the browser agent or whose session you stopped
- a **Mark applied** action on every Latest jobs row and in the listing dossier, so a role you applied to yourself is recorded without starting the browser agent
- the source lists' advanced-degree marker is now imported and enforced, so master's-, PhD-, and MBA-only roles are filtered out for a bachelor's profile even when the title omits the requirement

### Changed

- **the 1-10 fit score and the Qualified column are gone.** Both are replaced by the Apply?
  decision, which is made from the real posting instead of the listing title. Auto mode now applies
  only to listings answered **Apply**, so `automation.auto_apply_minimum_fit_score` is retired and
  removed from saved settings
- Auto mode no longer forces one application per company. The review already spends a company's
  budget, so a second approved role there is queued instead of being skipped as a duplicate
- reviewing uses Claude Opus 5 through `ANTHROPIC_API_KEY` when one is set, and otherwise through the
  Claude account already connected in Settings
- `tiaaa score` is replaced by `tiaaa review`; the old metadata-only LLM scorer is removed
- `eligibility.evaluate_listing` is now a hard-gate check only and no longer returns a score; it runs
  before the review so an ineligible listing never costs a model call
- the Applications tab opens on submitted applications instead of every ledger row
- graduate-only titles are recognized beyond the PhD keyword, including MBA, MFE, DPhil, postdoctoral, "graduate students", and "advanced degree" phrasing
- a posting that says it is closed or filled now retires the listing during the review
- a stop requested before a restart is honored on the next start instead of resuming the attempt
- applications recorded outside the browser agent are marked as candidate-submitted and are excluded from the application queue
- submission-authorized runs now complete and audit the full form before a separate final-action browser turn
- advanced-degree and previous-company-intern requirements now make a listing ineligible outright
- hard qualification conflicts discovered on an employer page remain ineligible during later sync cycles and are excluded from Auto mode

### Fixed

- listings are no longer rated by a keyword heuristic that returned 5/10 or 6/10 for almost
  everything and called clearly unqualified roles qualified; the decision now cites the requirement it
  read on the employer's page
- the per-company count no longer reports half-finished applications as filed, so a company with one
  submitted application and one form awaiting confirmation is described that way instead of as two
  applications already sent
- attempts that can never become an application — an employer that blocked the agent, a session you
  stopped — no longer consume a company's application slot
- the review is told the per-company limit is the candidate's own setting, so it can no longer present
  it as a maximum the employer's posting imposes
- an application Auto mode stopped for a missing candidate fact is queued again after the profile supplies that fact, instead of staying permanently benched
- package validation now accepts Core Metadata 2.5 emitted by current Hatchling releases
- the browser agent no longer uses the final Submit control to trigger validation or discover unfinished fields
- disabled or stuck **Submitting…** states without a receipt now become live human-interaction checkpoints instead of closing an intact application form
- address suggestion widgets are completed from the candidate's full configured address and checked field by field

### Security

- posting reads resolve each host and refuse local, private, and unroutable addresses, including
  after a redirect
- the reviewer runs Claude with no tools and no browser bridge; it only reads text already fetched
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
