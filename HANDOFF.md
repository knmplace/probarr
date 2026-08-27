# HANDOFF

> **Source of truth:** bead `probarr-84l` (`bd show probarr-84l --long`). This file is a
> portable, git-tracked snapshot of that bead for sessions without `bd`/`beads` access
> (e.g. remote or phone-initiated sessions cloning this repo fresh). Beads remain the
> real source of truth for task/work tracking — update the bead first, then refresh
> this file to match. Do not treat this file as a parallel tracking system.
>
> Repo note: this is **knmplace/probarr** — the active-work fork. `knmplace/probarr_clone`
> is excluded from active work; if a session finds itself in a checkout of `probarr_clone`,
> it's in the wrong repo — re-clone `knmplace/probarr` instead.

Last synced: 2026-08-27 (from bead `probarr-84l`, updated same day)

## Executive Summary

✅ Theme fix (PR #3) and EPG prewarm parallelization (PR #4) merged to `knmplace/probarr`
main, images built. 7 GitHub Issues filed (one per open bead) for backlog tracking.
probarr host moved `192.168.1.243` → `192.168.1.205` (IP conflict) — access fully
confirmed (HTTP + SSH), deployed image confirmed current. Remaining blocker: bead
`probarr-oz2` (Dispatcharr account auto-create live verification) is pending on the
user finishing re-adding providers/EPG sources on the new host.

## Key Facts

- HEAD: `ced99ff` (Merge PR #4, parallelize EPG prewarm). Working tree at last sync:
  only untracked `.claude/` hookify rule file (expected, not a problem).
- PR #3 (theme.py `--dim`/`--faint` brightened) — merged, commit `a7a047f`.
- PR #4 (`epgcheck.py` `prewarm_all_sources()` → `ThreadPoolExecutor`, `min(8, len(srcs))`
  workers) — merged, commit `ced99ff`.
- probarr host IP changed: `192.168.1.243` → `192.168.1.205` (2026-08-27, IP conflict on
  LAN). Updated `b:/Claude_Apps/.ssh.env` (PROBARR SERVER entry) to the new IP; root /
  key auth unchanged, only IP + comment updated. No other project file hardcodes `.243`
  for real (only an arbitrary example IP in a same-origin test fixture in
  `tests/test_probarr.py` — not a real reference, left as-is).
- Host confirmed reachable directly over HTTP: `curl http://192.168.1.205:7799/` → HTTP
  302 → `/runs`, `Server: probarr Python/3.12.13`.
- **Image verification RESOLVED** (see Comments Log below): deployed container digest
  on host exactly matches `ghcr.io/knmplace/probarr:latest` on GHCR. No pull/restart
  needed.
- Active hookify rules:
  - Project (`b:/Claude_Apps/probarr/.claude/`): `hookify.confirm-main-merge-triggers-image-build.local.md`
    — warns before any `gh pr merge` or `git push ... main`, since it triggers
    `docker-publish.yml`.
  - Global (`~/.claude/`): `hookify.mcp-direct-over-bash-scratch.local.md`,
    `hookify.no-scratch-md-for-handoffs.local.md`,
    `hookify.require-python-compile-check.local.md`,
    `hookify.update-bead-on-close.local.md`.

## Git & Releases

- Commit: `ced99ff`, tag: none. Working tree: effectively clean (only untracked
  `.claude/` hookify rule file, intentional).
- No GitHub Release created this session (routine pushes don't require one per
  CLAUDE.md).
- No pending push/merge.

## Deployed State

- Host: `192.168.1.205:7799` (moved from `.243`). Confirmed up and serving HTTP
  (302 → `/runs`).
- Running image: confirmed current — digest matches GHCR `:latest`
  (`sha256:0785c4c1517bc648fe43b7df639487016398501e0f118542a16db0853a1a3bcc`), built from
  the PR #4 merge (commit `ced99ff`, published 2026-08-27T16:46:23Z).
- Providers/EPG sources: being re-added by the user on the new host post-move; not yet
  confirmed done as of last sync.

## Work Completed

- **PR #3** — Brighten dim/faint theme colors: `--dim:#9aa4ab → #b7c0c6`,
  `--faint:#6b757c → #8a949b` in `probarr/theme.py`. Verified, merged `a7a047f`.
- **PR #4** (bead `probarr-8wj`) — Parallelize EPG prewarm: `epgcheck.py`
  `prewarm_all_sources()` rewritten from sequential loop to
  `concurrent.futures.ThreadPoolExecutor` (`min(8, len(srcs))` workers), preserving
  per-URL locking + best-effort exception handling. Merged `ced99ff`. Bead closed.
- Hookify rule created: `.claude/hookify.confirm-main-merge-triggers-image-build.local.md`
  — ensures future sessions always ask before merge/push-to-main (image-build trigger)
  rather than relying on memory alone.
- 7 GitHub Issues filed on `knmplace/probarr` (one per open bead, each with why +
  expected-outcome), cross-linked into the bead via comment:
  - #5 → `probarr-oz2` (Dispatcharr auto-create live verification)
  - #6 → `probarr-4a6` (CONTRIBUTING.md + CHANGELOG.md)
  - #7 → `probarr-5vk` (curate.py `alert()` → inline `.mresult.bad`)
  - #8 → `probarr-96q` (flip `docker-publish.yml` test gate to blocking — re-verify
    tests are actually green before flipping, don't just trust dependency-closed status)
  - #9 → `probarr-e4p` (exercise `backup.py` restore path end-to-end)
  - #10 → `probarr-h36` (feature: cross-provider channel merge)
  - #11 → `probarr-w4h` (keyboard shortcut discoverability)
- **Workflow decision** (user-confirmed): open beads get a GitHub Issue first
  (why/expected-outcome), NOT a PR, since most have no code yet. Convert Issue → PR only
  once a branch/diff exists for that bead. Once a PR is opened: do not close it on
  merge-readiness — keep it open with running comments (what was done, why, expected
  outcome) as work proceeds; only merge/close on explicit user go-ahead (consistent with
  the merge-confirmation hookify rule).

## Beads (Roadmap & Context Trail)

- Previous: none (first handoff bead for this project)
- Created: `probarr-84l` (this handoff bead)
- Closed: `probarr-8wj` (EPG prewarm fix, PR #4), `probarr-ri0` (theme-fix workflow
  correction)
- Related open: `probarr-oz2` (P2, blocking — live verification, see Outstanding Work),
  `probarr-4a6`/`5vk`/`96q`/`e4p`/`h36`/`w4h` (P3/P4, backlog, tracked via GitHub Issues
  #6–#11)

## Outstanding Work

**`probarr-oz2` (P2) — blocking, in progress.** Verify
`get_or_create_account_for_source()` (opt-in Dispatcharr account auto-creation on export
push) against the live instance at `192.168.1.205:7799`. Test plan (priority order):

1. Push to a provider with an existing correctly-configured M3U account (e.g. "Amber
   Baby" — Dispatcharr account ids 17 "AMBER BABY" and 19 "AMBER BABY 2", both baselined
   at `max_streams=1`, status: success, Active) — confirm `max_streams` ratchets down
   after a push (bug-fix verification for the `source_spec` fix in `probarr/web.py`
   `_run_export()` ~line 3678–3807).
2. Check the new "Create a native Dispatcharr account" checkbox for a provider with NO
   existing Dispatcharr account — confirm clean creation + check whether the refresh
   notification is the reassuring or scary kind.
3. Unchecked box — confirm no regression (no account created).
4. Colliding account name — confirm clean failure, no corruption.

**Recommendation:** run test #1 first (Amber Baby, existing account) once providers/EPG
are re-added — highest-confidence, lowest-risk test, directly re-validates the
already-merged `source_spec` bug fix under live conditions. Blocked only on the user
finishing provider/EPG re-setup on the new host.

All other open beads (`4a6`, `5vk`, `96q`, `e4p`, `h36`, `w4h`) are P3/P4 backlog, now
tracked as GitHub Issues #6–#11, no immediate action expected — re-triage priority via
`bd ready` next session.

## How to Pick This Up

- **Status**: probarr running at `192.168.1.205:7799`, providers/EPG being re-added by
  user.
- **Logs**: none pulled this session; check via host access if issues arise.
- **Deploy**: push to `knmplace/probarr` main → `docker-publish.yml` →
  `ghcr.io/knmplace/probarr:latest` (auto). Always confirm with user first (hookify
  rule).
- **Auth**: SSH key (`kid_rsa`) confirmed for `192.168.1.205` in
  `b:/Claude_Apps/.ssh.env`; password also on file as fallback. Direct HTTP to port 7799
  also works — SSH may not be needed at all for the oz2 test.
- **Next**: once the user confirms providers/EPG sources are back, resume
  `probarr-oz2` test #1 (Amber Baby push, confirm `max_streams` ratchet).

## Documentation Updated

- `b:/Claude_Apps/.ssh.env` — PROBARR SERVER entry: IP `192.168.1.243` → `192.168.1.205`,
  comment updated to note the move + reason.

## Confidence Checklist

- ✅ Code committed (PR #3, #4 both merged)
- N/A Released — no version tag needed for routine pushes
- ✅ Deployed — image built via `docker-publish.yml`; running digest confirmed matching
  `:latest`
- ⏳ Verified live — `probarr-oz2` test plan not yet run against new host
- ✅ Beads updated (`probarr-oz2` commented with IP-change + image-verification findings;
  all 7 open beads cross-linked to GitHub Issues)
- ✅ Working tree clean (only untracked new hookify rule file)
- ✅ No pending git operations

## Comments Log (chronological, from bead `probarr-84l`)

**2026-08-27 17:47 — Image-verification blocker RESOLVED.**

User added a new GitHub token (`PROBARR_FORKED`, in `b:/Claude_Apps/.env.gh.token`
line 3) with `repo` + `read:packages` scope — confirmed working against both
`api.github.com/repos/knmplace/probarr` and the GHCR packages API. Use this token (not
the first one in that file) for any future GitHub/GHCR API calls on this project.

SSH access to the probarr host also now confirmed working: `root@192.168.1.205` via
`~/.ssh/kid_rsa` key, no password needed, no classifier block. `docker inspect` on host
confirms container `probarr` image digest
`sha256:0785c4c1517bc648fe43b7df639487016398501e0f118542a16db0853a1a3bcc` exactly matches
`ghcr.io/knmplace/probarr:latest`'s digest on GHCR (verified via `PROBARR_FORKED`
token). Container was up 11 min, healthy, at check time. Deployed image is confirmed
current as of the PR #4 merge build (2026-08-27T16:46:23Z) — no pull/restart was needed.

Net effect: both direct HTTP (port 7799) and SSH (port 22) access to `192.168.1.205` now
work from a dev session. The earlier "unconfirmed deployed commit" and "no host access"
caveats are RESOLVED — superseded by this comment. Only remaining blocker for
`probarr-oz2`'s live test is the user finishing re-adding providers and EPG sources on
the new host (in progress as of this comment, not yet confirmed done).

**Access summary for next session:**
- SSH: `root@192.168.1.205` (`~/.ssh/kid_rsa`; password also on file as fallback per
  `b:/Claude_Apps/.ssh.env`)
- HTTP: directly to `http://192.168.1.205:7799`
- GitHub/GHCR API: `PROBARR_FORKED` token in `b:/Claude_Apps/.env.gh.token` (line 3,
  gitignored via `*.token` pattern — **never commit or echo this token to logs**)
