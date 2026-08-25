# Per-provider Dispatcharr accounts, instead of the shared "custom" one

Status: **scoped, not started**. This is a design document, not a plan
anyone has committed to executing. It exists so the tradeoffs are written
down once, in one place, rather than re-derived from scratch (or from
Slack/Discord memory) whenever someone picks this up.

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

## Open questions, before anyone writes code

- **Step zero, and it's outside probarr's own code**: no Dispatcharr M3U
  account on this instance currently matches probarr's own live provider
  credentials (see above) -- so before any of this can even be tested, a
  real, correctly-configured account needs to exist for Dispatcharr to
  natively parse. Whether creating/fixing that account is something
  probarr's own setup flow should do automatically (via the API, same as
  everything else here) or is left as a manual Dispatcharr-side step per
  provider is itself worth deciding, not assuming.
- **How often does the URL lookup actually miss?** The verbatim-URL
  reasoning above is sound but untested against real data. Once a real
  account exists and Dispatcharr has done one native refresh, worth
  literally counting: of a provider's candidates probarr has probed, what
  fraction match an existing native stream by URL vs. need the custom-
  stream fallback? If the miss rate turns out to be high, the "fallback is
  rare" assumption this design leans on is wrong and needs revisiting
  before relying on it.
- **Migration**: every existing probarr-pushed channel today has its
  streams sitting under the shared `"custom"` account. Does a per-provider
  redesign need to migrate those (re-point existing Stream rows'
  `m3u_account`, which the live test confirms the API allows), or does it
  only apply going forward, leaving old exports as a second, understood
  legacy shape indefinitely? A silent split (some channels enforced
  per-provider, older ones still pooled in `"custom"`) is probably the
  worst outcome and worth ruling out explicitly rather than falling into.
- **Naming/collision**: what happens when a provider is renamed or deleted
  in probarr's own `providers.json` -- does its Dispatcharr account get
  renamed/orphaned/deleted too? `dispatcharr_export.py`'s own docstring
  already documents a deliberate "never delete, only create and update"
  policy for channels; whatever this becomes should say explicitly whether
  M3U accounts follow the same rule.
- **The account-creation refresh notification** (finding 2, above): even
  with a real `server_url`, is there a way to suppress or pre-empt that
  first automatic refresh attempt (an API flag, creating with the periodic
  task disabled up front, etc.), or is a first-run "M3U Processing" log
  line simply an accepted, harmless cosmetic side effect once the URL is
  real and the refresh actually succeeds? Worth a few minutes checking
  Dispatcharr's own source for the signal that triggers it before assuming
  either way.
- **Rate limits while doing this work**: Dispatcharr's own API throttles
  rapid repeated `/api/accounts/token/` calls (confirmed live while
  researching this doc -- two 429s from re-authenticating too quickly).
  probarr's own `Dispatcharr.api()` already retries 429s correctly,
  honouring the `"Expected available in N seconds"` body Dispatcharr
  returns; any exploratory scripting against a live instance while building
  this should go through that client, not raw calls, to avoid hitting it.

## Non-goals

- This does not change anything about `enforce_custom_stream_limit()`'s
  core safety property (ratchet down, never silently up) -- a per-provider
  account just gives that ratchet a correctly-scoped account to apply to
  instead of one shared by everyone.
- This is not a redesign of how probarr decides candidate *quality* or
  *matching* -- purely about which Dispatcharr account a pushed stream ends
  up filed under, and what Dispatcharr itself can therefore enforce.
