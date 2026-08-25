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

## Two shapes this could take

**A. Stub account, real URL, streams still forced custom.**
Create one M3U account per probarr provider, named after it, with that
provider's real `server_url` (the same spec probarr already has configured)
so account creation doesn't misfire. Keep pushing every candidate through
`get_or_create_custom_stream()` exactly as today, just pointed at this
account's id instead of the shared `"custom"` one. Per-provider
`max_streams` becomes real and visible in Dispatcharr's normal UI (no
`locked` flag needed, since it's now a genuine account). Smallest possible
change to the actual push logic -- `enforce_custom_stream_limit()`
generalizes to "per pushing-provider account" instead of "the one shared
account," and that's most of the diff.

Leaves an oddity: Dispatcharr will now ALSO try to genuinely parse this
account's M3U on its own refresh schedule, in parallel with probarr doing
its own independent fetch/parse of the identical source for probing. Two
systems reading the same catalog, never reconciled, is exactly the kind of
thing that was flagged as a real, lived pain point earlier in this
project's history (the IPTV pipeline's own M3U-account/EPG-source
confusion). Worth an explicit decision on whether that duplication is
acceptable or needs addressing (see below), not an accident to discover
later.

**B. Real native account, prefer Dispatcharr's own parsed streams.**
Same per-provider account as A, but `get_or_create_custom_stream()` first
checks whether the candidate's URL already exists as a NATIVE stream in
that account's own table (i.e. Dispatcharr's own M3U refresh already found
it) before creating a synthetic custom-attached one. Only falls back to a
custom stream for URLs Dispatcharr's own parse doesn't have an exact match
for -- which will happen sometimes (probarr's matching/normalisation is not
byte-identical to Dispatcharr's own M3U parsing), so the fallback path
can't be removed, only made the exception rather than the rule.

This is the more "native," more intuitive end state -- a probarr-managed
channel becomes indistinguishable from one a user set up by hand pointing
Dispatcharr at the same provider, which is a real usability win for anyone
who later wants to poke at it in Dispatcharr's own UI without wondering why
half their streams are mysteriously "custom." It is also more moving parts:
timing (probarr pushing before Dispatcharr's own refresh has run yet, so
the native lookup finds nothing and creates a redundant custom stream
anyway), staleness (Dispatcharr's refresh interval vs. probarr's own probe
cadence), and now two independent parsers of the same account whose outputs
need to agree closely enough for the URL-match lookup to actually hit.

## Open questions, before anyone writes code

- **A or B?** A is safe and mechanical; B is the better end state but real
  scope. Worth deciding explicitly rather than drifting into B's complexity
  while aiming for A's timeline.
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
