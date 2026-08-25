# Per-provider Dispatcharr accounts, instead of the shared "custom" one

Status: **shipped and verified live (2026-08-25)**, for one provider on one
instance. `mybunny`'s Dispatcharr account (`BunnyCustom`) now natively
parses the real catalog, and a fresh push after clearing out the old
custom-stream channels came back **100% native (371/371 stream
references across 124 channels), 0% custom**. What remains open is
generalising this from "one provider, corrected by hand" to something
every provider gets automatically -- see Open questions, most of which
narrowed or resolved during the build but a few of which are still real.

## The problem

Every stream probarr pushes into Dispatcharr today goes through
`get_or_create_custom_stream()` (`probarr/sources/dispatcharr.py`), which
creates it under Dispatcharr's one special, hidden `"custom"` M3U account --
the only mechanism Dispatcharr exposes for attaching a stream that didn't
come from a real M3U/Xtream import.

That account is **shared across every provider probarr has ever pushed
from**, completely independent of which real upstream each stream's URL
actually belongs to. Its `max_streams` is the only concurrency limit
Dispatcharr can enforce on any of them, and it enforces it as one number
for all of them combined -- there is no way, today, to tell Dispatcharr
"3 concurrent connections for provider A, 6 for provider B."

`enforce_custom_stream_limit()` already mitigates the worst version of this
(silently having NO limit enforced at all -- confirmed live, the account
defaults to `max_streams=0`), by tightening the shared account down to
whichever pushing provider's own concurrency setting is lowest, and
**deliberately only ever tightening, never loosening** -- see its docstring
for the reasoning. That is a safe, working stopgap. It is not per-provider
enforcement, and it means the number visible in Dispatcharr's own UI
(hidden behind "locked", not even normally editable there) has no
obvious relationship to any one provider a user is looking at. For a
project other people are picking up and self-hosting, "the real limit is a
number you can only see by hitting the API directly, and it's whatever the
most conservative provider anyone has ever pushed from happened to need" is
not intuitive, and that's the actual thing being scoped a fix for here.

## What's confirmed feasible

Two things were verified live against a real Dispatcharr instance while
scoping this (2026-08-25), both destructive-but-reversible, both cleaned up
afterward:

1. **`m3u_account` on a custom stream is respected**, not silently ignored
   or forced back to the shared `"custom"` account. Created a throwaway
   locked M3U account (id 12), created a stream with
   `{"is_custom": true, "m3u_account": 12}`, and the stream came back
   genuinely attached to account 12, not `"custom"`. This is the load-bearing
   fact the whole redesign depends on, and it holds.

2. **Creating a new M3U account triggers an immediate one-time refresh
   attempt**, independent of whether its periodic refresh is enabled. The
   throwaway account above was created with an empty `server_url` (it was
   only ever meant to be a namespace for custom streams, never a real M3U
   source) -- and Dispatcharr surfaced a user-visible failure notification
   almost immediately: `M3U Processing: probarr-scope-test / downloading
   failed: No M3U source available (missing URL and file)`. Confirmed via
   `docker logs dispatcharr` that the periodic task itself was created
   `enabled=False`, so this was NOT a recurring scheduled failure -- just a
   one-shot kick-the-tyres refresh Dispatcharr does right after creating any
   account, regardless of that flag. It does not appear to repeat once
   deletion cascades the account and its task away, but it is a real,
   visible, alarming-looking error the first time, for anyone watching
   Dispatcharr's own notifications.

That second finding matters more than it looks like it should: it rules out
the "cheap" version of this design (a locked, URL-less stub account per
provider, existing purely as a namespace) as something that can ship
without a scary false-alarm notification on every new provider. It pushes
the design toward giving the per-provider account a **real, working
`server_url`** -- which, once you're doing that, changes what this feature
actually is.

## The actual target: native accounts, not custom streams at all

An earlier draft of this document treated "give the per-provider account a
real URL" and "stop using custom streams" as two separate, independently
optional moves (labelled Shape A and Shape B) and hedged between them. That
was wrong, and worth correcting explicitly rather than quietly editing
away: **only the combination is the point.**

The requirement driving this isn't "make the number nicer to look at" --
it's that a real Dispatcharr M3U/Xtream account's `max_streams` is enforced
against *everything* drawn from that account: Live TV channel playback AND
VOD (Movies/TV Shows, via the same account's Xtream catalog scanning --
see the docker-media Dispatcharr setup, which already runs exactly this for
its own paid provider). A custom stream is invisible to that accounting
**no matter which M3U account it's filed under** -- attaching it to a
per-provider account instead of the shared `"custom"` one makes the number
visible and correctly scoped, but it still wouldn't be the same pool of
connections Live TV and VOD are actually drawing from. Giving the account a
real URL without also stopping the use of custom streams gets none of the
actual benefit; it only fixes the cosmetics.

So the target shape is: Dispatcharr genuinely parses each provider's M3U as
a real, first-class account (exactly as if a user pointed Dispatcharr at it
by hand), and probarr's export **attaches channels to the streams that
parse already produced**, rather than inventing new ones. `is_custom`
streams stop being the normal path; they are, at most, a rare fallback for
a URL Dispatcharr's own parse doesn't have (see the open questions below),
not the mechanism.

Practically: `get_or_create_custom_stream()` is replaced by something that
looks up the candidate's URL in that provider's account's own
already-parsed stream table first. **This is more feasible than it might
sound**: checked live while scoping this, probarr's own M3U parser
(`sources/m3u.py`) stores each stream's URL completely verbatim off the
`#EXTINF` line (`url=line`, no rewriting, no normalisation of the URL
itself -- only the display name gets that treatment, for matching
purposes). Dispatcharr is parsing the exact same M3U text through the exact
same kind of line-by-line read, so a plain URL-string lookup against its
own stream table should hit reliably. This needs confirming against a real
account once one exists, not just asserted (see the open questions below),
but there's no structural reason to expect it to fail often.

A URL that genuinely doesn't appear in Dispatcharr's own parse -- possible
if probarr matched a redirect/alias variant, or probed between refreshes --
still needs *some* answer. Falling back to a custom stream for that one
candidate is acceptable as a rare edge case; it stops being acceptable if
it turns out to be the common case, which is exactly the kind of thing to
measure once this exists rather than assume either way.

One more thing this surfaces: **there is currently no Dispatcharr M3U
account, anywhere on this instance, that actually matches probarr's own
current provider credentials.** The three that exist (`custom`,
`BunnyCustom`, `BunnyVOD`) all carry older, superseded logins. So step
zero of building this, before any probarr code changes, is a real account
existing to natively parse against at all -- either created fresh, or one
of the existing stale ones corrected. Worth remembering this is not purely
a probarr-side change: it depends on Dispatcharr-side setup being right
first, for however many providers a given user has.

## What actually shipped

Two code changes, both in `probarr/sources/dispatcharr.py`, plus one
Dispatcharr-side correction done by hand:

1. **Step zero, done manually**: `BunnyCustom`'s `server_url` corrected to
   probarr's live `mybunny` credentials, `max_streams` set to 4, refreshed.
   `get_or_create_custom_stream()` needed **no code change at all** for
   this part -- it already looked up the full stream table by URL before
   ever creating anything new (re-read closely while starting this; the
   original problem statement above slightly overstated how hardcoded the
   custom-stream path was). Once a real account existed to find matches
   in, most candidates started resolving natively immediately.
2. **`find_account_for_source()` / `enforce_provider_stream_limit()`**:
   the actual missing piece -- `enforce_custom_stream_limit()` could only
   ever tighten the ONE shared `"custom"` account. The new pair finds a
   provider's own real account by exact `server_url` match and ratchets
   *its* `max_streams` the same way, wired into the export push in
   `web.py` alongside the existing shared-account call (which stays, as
   the fallback's own safety net).
3. **`stream_url_map()` native-vs-custom bug**, found immediately while
   verifying #2 against real data, not anticipated in the original scoping:
   a channel pushed *before* its provider had a correctly configured
   account keeps its old custom stream. Once the account is fixed and
   Dispatcharr's own refresh produces a NATIVE stream with the identical
   URL, the old code's plain `{url: id}` dict comprehension kept whichever
   row paginated last -- no preference between the two. Confirmed live: one
   real channel's four candidates split 3 custom / 1 native purely from
   pagination order, despite all four existing natively by push time.
   Fixed by scanning custom and native separately and merging native
   second, so it always wins. Not deleting the stale custom rows --
   consistent with `dispatcharr_export.py`'s documented never-delete
   policy -- they just stop being referenced.

**Measured, not estimated**: the catalog-wide URL match rate (95.9% of
43,888 URLs, see above) predicted the fallback would be rare. The real
push came back even cleaner -- 100% native across every channel actually
curated -- which makes sense in hindsight: candidates that never matched
anything natively were disproportionately the ones already excluded from
curation for other reasons (disabled provider groups, dead streams).

## Open questions, still real

- **This fixed one provider on one instance, by hand.** `BunnyCustom`'s
  URL correction was a manual API call during this session, not something
  probarr's own code did. Nothing here auto-creates or auto-corrects a
  Dispatcharr account for a NEW provider -- `find_account_for_source()`
  only finds an account that already matches; if none does, it's silently
  a no-op and that provider's candidates keep landing in the shared
  `"custom"` fallback exactly as before. Whether probarr's setup flow
  should offer to create/fix the matching account automatically (and
  trigger the refresh, and accept the one-time notification that comes
  with it -- see below) is the real remaining design decision.
- **Migration for existing custom-stream channels on other providers**:
  this session's channels self-healed because Dispatcharr had already been
  wiped clean and re-pushed from scratch. A provider whose channels were
  never wiped, only had its account corrected, relies entirely on the
  `stream_url_map()` fix plus a normal re-push to swap over -- worth
  confirming that path explicitly (push without a wipe first) rather than
  assuming the clean-slate result generalises.
- **Naming/collision**: what happens when a provider is renamed or deleted
  in probarr's own `providers.json` -- does its Dispatcharr account get
  renamed/orphaned/deleted too? Still unaddressed; `find_account_for_source`
  only ever looks up by the CURRENT spec, so a renamed provider with an
  unchanged URL keeps matching fine, but a provider whose URL itself
  changes (credential rotation, a new package) silently stops matching its
  old account until something (a human, or future code) points one at the
  other again.
- **The account-creation refresh notification**: still untested whether it
  can be suppressed or pre-empted. Not hit this time because `BunnyCustom`
  already existed -- it was corrected, not created. Relevant again the
  moment auto-creation (above) becomes real.
- **Rate limits while doing this work**: Dispatcharr's own API throttles
  rapid repeated `/api/accounts/token/` calls (confirmed live -- 429s from
  re-authenticating too quickly, twice, during this build). probarr's own
  `Dispatcharr.api()` already retries 429s correctly, honouring the
  `"Expected available in N seconds"` body Dispatcharr returns; any
  exploratory scripting against a live instance should go through that
  client, not raw calls.

## Non-goals

- This does not change anything about `enforce_custom_stream_limit()`'s
  core safety property (ratchet down, never silently up) -- a per-provider
  account just gives that ratchet a correctly-scoped account to apply to
  instead of one shared by everyone.
- This is not a redesign of how probarr decides candidate *quality* or
  *matching* -- purely about which Dispatcharr account a pushed stream ends
  up filed under, and what Dispatcharr itself can therefore enforce.
