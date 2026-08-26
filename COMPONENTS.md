# Components

This project's deployment-tier inventory. See `_shared/deployment-tier.md` for tier definitions.

## Components

| Component | Tier | Purpose |
|-----------|------|---------|
| web.py (web app / HTTP server) | home-lab | stdlib `BaseHTTPRequestHandler`, binds `0.0.0.0:7799` by default; single-operator IPTV curation UI |
| store.py (RunStore) | small-team | Flat-file JSONL persistence for run history/results; blast radius extends to anyone consuming exported data |
| runner.py | home-lab | Orchestrates a curation run, shared by CLI and web |
| curate.py | home-lab | Core curation logic/UI glue; uses `alert()` for error states |
| sources/ (base.py, m3u.py, xtream.py, dispatcharr.py) | home-lab | Provider source plugins (M3U, Xtream, Dispatcharr) via `register()`/`load_source()` pattern |
| dispatcharr_export.py | small-team | Pushes curated channels/streams into a real Dispatcharr instance; failures affect every viewer of that Dispatcharr deployment, not just the operator |
| epgcheck.py / epgsources.py / epg.py | home-lab | EPG source consensus checking; currently the source of the real (non-Windows-fixture) test failures |
| backup.py | home-lab | tar.gz backup/restore with path-traversal protection; restore path not yet verified exercised |
| settings.py + settings.json (config/secrets store) | small-team | Holds provider credentials (e.g. Xtream `user:pass@host` URLs); currently served back unauthenticated via `GET /api/settings` — credential-leak risk affects anyone who can reach the provider account, not just the operator |
| lineups.py | home-lab | Channel lineup handling |
| rank.py / normalize.py / decisions.py / dhash.py / verify.py / probe.py / probequeue.py | home-lab | Stream ranking, normalization, decision logic, dedup hashing, verification/probing pipeline |
| contactsheet.py | home-lab | Visual contact-sheet generation for stream verification |
| pages.py / theme.py | home-lab | Web UI page rendering and theming |
| providers.py / wantlist.py / aliases.py | home-lab | Provider config, want-list, and alias management |
| Docker/GHCR publish pipeline (Dockerfile + .github/workflows/docker-publish.yml) | small-team | Publishes `ghcr.io/.../probarr:latest` on every push to `main`, with no test-execution gate; affects anyone who pulls the public image, not just this operator |
| tests/test_probarr.py | home-lab | stdlib unittest suite; currently 3 fail + 8 error (Windows file-URL fixture bug, cp1252 encoding bug, and genuine EPG-consensus defects) |

## Notes

- **Tier is about blast radius to real humans, not the AI persona team and not headcount.** home-lab = only the operator is affected if something breaks. small-team = other real people are affected (other viewers of a shared Dispatcharr instance, anyone who pulls the public GHCR image, whoever's credentials leak via the unauthenticated settings endpoint).
- The three small-team components were flagged because their failure modes reach beyond the single operator:
  - **RunStore/store.py** and the **config/secrets store** — flat-file persistence holding data (including credentials) that could be exposed or corrupted in ways that affect more than the operator's own session.
  - **Dispatcharr export integration** — pushes live changes into a shared Dispatcharr instance; per `docs/design/per-provider-m3u-accounts.md`, this already has real, measured production impact (100% native stream resolution achieved for one provider) and known open risks (migration, naming/collision, rate limits).
  - **Docker/GHCR publish pipeline** — ships a public image with zero test gate; 3 independent onboarding personas (Project Engineer, QA Engineer, IT Architect) flagged this as the highest-leverage fix, since a broken build can ship to anyone who pulls `:latest`.
- Tier is a **calibration label**, not a technical control by itself. Concrete fixes (redact credentials from `/api/settings`, add a CI test gate, exercise the backup restore path) are separate action items that stand on their own merit — the tier only determines how strict future persona reviews (`/team-plan`, `/grooming`, etc.) should be on that component.
- Strictest-tier-wins applies when a feature spans components at different tiers (e.g. a change touching both `curate.py` and `dispatcharr_export.py` is reviewed at small-team rigor).
- This table was produced by `/onboard` (Quick mode) on 2026-08-25/26 from all 10 persona assessments, led by the IT Architect's draft, and confirmed by the PO.
