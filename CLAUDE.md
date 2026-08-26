# --- Claude Agent Dev Team (project) ---
# Managed by /onboard. Re-run /onboard to update this block.
This project is run by the Claude Agent Dev Team.

- **Tiers**: `COMPONENTS.md` at the repo root is the deployment-tier authority — read it before calibrating rigor.
- **Board**: beads (`bd`), stealth mode — `.beads/` is git-invisible via `.git/info/exclude`, never committed. Source of truth for all task/work tracking; never TodoWrite or markdown TODOs for this project.
- **Gates**: `python -m unittest discover tests` (stdlib unittest, no network required by design — currently 3 fail + 8 error; root causes triaged, fixes not yet applied).
- **Deploy**: push to `main` on knmplace/probarr_clone auto-publishes `ghcr.io/.../probarr:latest` via `.github/workflows/docker-publish.yml`. No test-execution gate in that workflow currently. No GitHub Release needed for routine changes — only tag `vX.Y.Z` for a deliberate version bump.
- **Upstream**: some fixes are meant to go back to probarr-dev/probarr as PRs — include description, root cause, reasoning, files touched, and test results in each.
# --- End Claude Agent Dev Team (project) ---
