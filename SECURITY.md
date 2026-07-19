# Security policy

## Supported versions

TI-AAA is pre-1.0 software. Security fixes are applied to the latest release and the default branch.

## Reporting a vulnerability

Please use GitHub's private **Report a vulnerability** flow under the repository Security tab. Do not open a public issue for vulnerabilities involving:

- exposure of profile, resume, or application data
- arbitrary file access through the dashboard or browser worker
- command execution or prompt injection
- final submission occurring without both opt-ins
- authentication bypass
- unsafe external dashboard binding

Include a minimal reproduction, affected version/commit, impact, and any suggested mitigation. Maintainers should acknowledge a report within seven days and coordinate disclosure after a fix is available.

## Operational guidance

- Bind the dashboard to loopback unless it is behind an authenticated proxy.
- Keep `~/.tiaaa` readable only by your user account.
- Do not enable `TIAAA_DEBUG_AGENT_OUTPUT` unless necessary; browser-agent output may contain personal information.
- Use restricted API keys and rotate a key after accidental logging or commit.
- Review dependencies and lock versions in production deployments.
- Keep final submission disabled until the profile and a review-only run have been verified.
