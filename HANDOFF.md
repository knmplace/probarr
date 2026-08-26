# Handoff: opt-in Dispatcharr M3U account auto-creation

Branch: `claude/handoff-continuation-ienl93`
Latest commit: `722f6e2` (already pushed, matches `origin/claude/handoff-continuation-ienl93`)
No PR opened yet.

This file exists because this session had no working `beads`/`handoff`
tooling available (checked: no `beads`-style tool in the tool list, no
`handoff` skill loaded, no matching plugin) even though the user's local
Claude Code CLI apparently has both. Treat this as the raw material to
file into whichever of those actually works from your side -- it isn't a
replacement for them, just what a tool-less session could produce.

## What this session did, in order

1. Read `git log`, `docs/design/per-provider-m3u-accounts.md`, and
   confirmed the branch head equals `main` (everything before this
   session's work was already merged) to figure out "what's next" with no
   working handoff tool available.
2. Design doc's own "Open questions" section named the real remaining
   decision: nothing auto-creates a Dispatcharr M3U account for a new
   provider. User picked this thread explicitly (asked via
   `AskUserQuestion`, not assumed).
3. Read `probarr/sources/dispatcharr.py`, `probarr/web.py`,
   `probarr/providers.py`, `probarr/curate.py` to understand the existing
   per-provider-account machinery (`find_account_for_source`,
   `enforce_provider_stream_limit`) before adding anything.
4. **Found a real bug while doing that reading, not while implementing
   the new feature**: `web.py`'s `_run_export()` was calling
   `enforce_provider_stream_limit()` with `prov["spec"]` -- the Dispatcharr
   *export target's own* connection string (`dispatcharr://...`) -- instead
   of the *original upstream provider's* spec (e.g. mybunny's playlist
   URL), which is what `find_account_for_source()` actually needs to match
   Dispatcharr accounts by `server_url`. A `dispatcharr://` string can
   never equal a real M3U account's `server_url`, so **in production this
   enforcement call was silently always a no-op** -- it looked wired up,
   ran without error, and did nothing. Existing unit tests for
   `enforce_provider_stream_limit()` never caught this because they test
   the method in isolation and pass it the right kind of spec directly;
   nothing exercised the *caller* passing the wrong one.
5. Fixed that: `_run_export()` now looks up the source provider via
   `meta.get("provider_name")` and uses *its* spec. Added
   `TestRunExportUsesTheSourceProviderSpec` in `tests/test_probarr.py`,
   which asserts the actual spec string reaching the mocked client (not
   just "a call happened") -- this is the test that would have caught the
   original bug.
6. Also hardened `find_account_for_source()`: a falsy/`None` spec used to
   fall through to the real comparison and could accidentally match the
   shared `"custom"` account (which genuinely has `server_url: None`),
   silently tightening the wrong account for a CLI-driven run with no
   saved provider behind it. Now short-circuits to "no match" first.
7. Implemented the actual feature:
   `Dispatcharr.get_or_create_account_for_source(spec, name, log=None)` in
   `probarr/sources/dispatcharr.py` -- finds an existing account by exact
   `server_url` match (reusing `find_account_for_source`), and if none
   exists, creates one via `POST /api/m3u/accounts/` with a real
   `server_url` (never a URL-less stub, deliberately -- see the design doc
   for why that shape was ruled out). Only attempted for `http://`/`https://`
   specs; `xtream://`/`dispatcharr://` specs are skipped since Dispatcharr
   has no `server_url` string that could ever match those verbatim.
8. Wired it into the export flow as an **opt-in, off-by-default** checkbox
   in Curate's "Export to Dispatcharr" modal ("Create a native Dispatcharr
   account for this run's provider") -- not something every push does
   silently, because creating an account is a real, visible change to the
   user's Dispatcharr instance. Reset to unchecked every time the modal
   opens (deliberately not persisted like the other push settings).
9. Added unit tests for the new method (`TestGetOrCreateAccountForSource`):
   creates when missing, reuses when found, skips non-URL specs, skips
   empty specs, logs (doesn't raise) on a failed POST.
10. Updated `docs/design/per-provider-m3u-accounts.md` (new "Auto-creation
    (opt-in), and a bug it surfaced" section, revised status line, updated
    the relevant open question) and `README.md` (short paragraph on the
    new checkbox) to reflect what shipped.
11. Ran the full suite (`python3 -m unittest discover -s tests -p
    "test_probarr.py"`): 110 tests, only failure is
    `TestBackup.test_refuses_a_path_traversal_member`, confirmed
    pre-existing on `main` before this session's changes (via `git stash`),
    unrelated to this work -- an environment quirk where `/etc/passwd`
    already exists at the resolved path regardless of the traversal
    attempt.
12. Committed as `722f6e2` and pushed to
    `origin/claude/handoff-continuation-ienl93`.

## What we actually know vs. what still needs a live Dispatcharr

Two different confidence levels in this change, and they should not be
conflated when reviewing or testing:

**High confidence -- proven by code alone, no live instance needed:**
the bug fix (step 4-6 above). `TestRunExportUsesTheSourceProviderSpec`
asserts the literal spec string that reaches the Dispatcharr client
before vs. after the fix. This is a pure "which value does this function
get called with" question -- unit tests are the right tool and they
answer it completely.

**Unverified -- unit tests can't answer this, only a real Dispatcharr
can:** the new `get_or_create_account_for_source()` feature. Every test
for it mocks `client.api()` -- none of them talk to real Dispatcharr.
They prove: the right endpoint gets called, with the right payload shape,
skipping the right spec types, without crashing on a failed request. They
CANNOT prove:
- that Dispatcharr's real `/api/m3u/accounts/` POST actually accepts
  `{"name", "server_url", "is_active"}` as a full account definition
  (the "custom" account recreation note in `dispatcharr.py`'s own module
  docstring mentions needing "a default M3UAccountProfile" too, for that
  *different* special-cased account -- untested whether an ordinary
  account POST needs anything analogous)
- that creating an account this way genuinely produces the theorized
  "no scary notification" result (the design doc's live finding was that
  an EMPTY `server_url` triggers a visible one-time refresh failure;
  giving it a real URL *should* avoid that, but account CREATION with a
  real URL, specifically, has never been exercised live -- only manual
  correction of an existing account's URL has)
- that the created account's subsequent refresh actually parses the
  provider's playlist the way a hand-created one does

This is exactly what your test pass against a real instance needs to
check. See "What to actually test" below.

## Docker image / build

`.github/workflows/docker-publish.yml` only triggers on push to `main` or
a `v*` tag (or manual `workflow_dispatch`) -- **not** on this feature
branch. Pushing to `claude/handoff-continuation-ienl93` does NOT produce a
new `ghcr.io` image. Options, depending on what "new image" meant:

- **Build locally from this branch**: `docker build -t probarr:test .`
  from a checkout of this branch -- works right now, no merge needed.
- **Trigger the GHCR workflow manually**: the workflow has
  `workflow_dispatch: {}`, so it can be run by hand from GitHub Actions
  against this branch's ref if you want a `ghcr.io` image without
  merging to `main` yet.
- **Merge to `main`**: fires the workflow automatically, produces
  `ghcr.io/<repo>:latest`. Only do this once you're satisfied with review
  (see below) -- this is the one path that also makes the change visible
  to anyone else pulling `latest`.

## What to actually test live

In rough priority order:

1. **The bug fix, indirectly**: push to a Dispatcharr instance where the
   source provider already has a real, correctly-configured M3U account
   (the `BunnyCustom`/`mybunny` case from the design doc is exactly this).
   Before this fix, that account's `max_streams` was never actually being
   ratcheted by a push; after, it should be. Check via the API or the
   Dispatcharr UI that `max_streams` on that account reflects the run's
   concurrency after a push (deliberately never loosens -- only checks
   downward).
2. **The new checkbox, with a provider that has NO existing Dispatcharr
   account yet**: check the box, push, and see:
   - a new M3U account appears in Dispatcharr's UI, named after the
     probarr provider, with `server_url` set to that provider's real
     playlist URL
   - whether Dispatcharr shows the one-time refresh notification, and
     whether it's the reassuring "parsed successfully" kind or the scary
     "downloading failed" kind
   - whether that account's refresh actually produces native streams
     matching this provider's catalog (spot-check a few URLs)
3. **The checkbox left unchecked**: confirm push behavior is byte-for-byte
   the same as before this branch (no account gets created, only the
   shared `"custom"` account and any pre-existing per-provider account get
   their limits enforced).
4. **A provider whose name collides with an existing, differently-sourced
   Dispatcharr account**: confirm the POST fails cleanly (Dispatcharr
   enforces unique names) and shows up in the push's log lines rather than
   silently swallowing the error or corrupting the existing account.

## PR-review expectations, once you're ready to open one

- Title/summary should separate the bug fix from the new feature clearly
  -- they're two different review questions ("is this a correct fix" vs.
  "do we want this opt-in behavior and is it safe"). Consider whether you
  want them as one PR or split; they're one commit right now
  (`722f6e2`) but that's not a hard constraint.
- The diff touches `probarr/web.py` (routing + the bug fix),
  `probarr/sources/dispatcharr.py` (the new client method + the
  `find_account_for_source` hardening), `probarr/curate.py` (the UI
  checkbox), `tests/test_probarr.py`, `docs/design/per-provider-m3u-accounts.md`,
  and `README.md`.
- Call out explicitly in the PR body: the auto-create feature is
  **unverified against a real Dispatcharr instance** (see above) --
  reviewers should not read "tests pass" as "confirmed working against
  Dispatcharr," only as "the logic does what it claims to do against a
  mocked API."
- No CI workflow currently runs the test suite on PRs (checked
  `.github/workflows/` -- only `docker-publish.yml` exists, and it doesn't
  run tests). If CI matters for merge approval here, that's a gap
  independent of this change, worth knowing before treating "PR is green"
  as a signal.
- Follow the repo's own commit-message voice (see recent `git log`) if
  amending or squashing -- terse, present-tense, explains "why" not "what."
