"""Persistent settings, shared by the CLI and the web UI.

Concurrency lives here rather than only on the command line because it is the
one setting that depends on something probarr cannot discover: how many
simultaneous connections your provider actually permits. A one-stream account
must probe strictly serially; a three-stream account can go roughly three times
faster. Getting it wrong in the generous direction does not fail cleanly -- an
over-limit provider returns plausible-looking garbage that reads as a dead
stream, quietly corrupting the results.

So it is set once, by the person who knows the answer, and everything obeys it.
"""
import json
import os

DEFAULTS = {
    "concurrency": 1,
    "gap_seconds": 0.4,
    "sample_seconds": 10,
    "frame_height": 720,
    "thumb_height": 240,
    "source": "",
    "epg": "",
    "wantlist": "",
    # Remembered from the last successful push, so the export dialog opens
    # ready to go instead of demanding the same three answers every time.
    # Server-side rather than in the browser: the answer is a property of
    # this probarr and its Dispatcharr, not of whichever device happens to
    # be looking at it.
    "push_provider": "",
    "push_fallback": "native",
    "push_prune": True,
    # Real playback evidence from Dispatcharr's own event log -- has a
    # stream actually failed over in production, not just in a probe --
    # deliberately a setting rather than a fixed behaviour, so it can be
    # turned off, watched, or later trusted enough to affect ranking without
    # a code change either way:
    #   off  -- never asked, never shown
    #   info -- shown on the channel card; never affects ranking (default)
    # More values (e.g. weighting it into ranking) land here later without
    # disturbing what is already saved.
    "failover_display": "info",
    # Idea borrowed from Podium (open-source, see its README): a re-verify
    # of a LINEUP (not an ad-hoc run) skips re-probing a candidate whose
    # provider-declared stream id hasn't changed since its last verdict,
    # as long as that verdict is still within this many hours -- carrying
    # the prior result forward instead of spending a connection on a
    # stream nothing has actually touched. 0 disables it, matching every
    # run's behaviour before this existed: always probe everything.
    # Defaults to a week, just under the common weekly re-verify schedule,
    # so a routine Monday run mostly skips and a real change still gets
    # caught on its own next pass.
    "freshness_hours": 24 * 6,
}

# Anything above this is almost certainly a mistake rather than a real
# subscription allowance, and the cost of being wrong is silently bad data.
MAX_CONCURRENCY = 16


def path(root):
    return os.path.join(root, "settings.json")


def read(root):
    out = dict(DEFAULTS)
    try:
        with open(path(root), encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            out.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return coerce(out)


def coerce(values):
    """Force types and clamp ranges. Values may arrive from a browser."""
    out = dict(DEFAULTS)
    out.update(values or {})
    def _int(key, lo, hi):
        try:
            out[key] = max(lo, min(hi, int(out.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    _int("concurrency", 1, MAX_CONCURRENCY)
    _int("sample_seconds", 3, 60)
    _int("frame_height", 180, 2160)
    _int("thumb_height", 90, 720)
    _int("freshness_hours", 0, 24 * 60)
    try:
        out["gap_seconds"] = max(0.0, min(10.0, float(out.get("gap_seconds", 0.4))))
    except (TypeError, ValueError):
        out["gap_seconds"] = DEFAULTS["gap_seconds"]
    for k in ("source", "epg", "wantlist", "push_provider"):
        out[k] = str(out.get(k) or "")
    if out.get("failover_display") not in ("off", "info"):
        out["failover_display"] = DEFAULTS["failover_display"]
    if out.get("push_fallback") not in ("native", "separate"):
        out["push_fallback"] = DEFAULTS["push_fallback"]
    # Arrives from a browser as a JSON bool, but also as the string "false"
    # if anything ever posts it as form data -- which is truthy, and would
    # silently turn group tidying back on for good.
    v = out.get("push_prune", True)
    out["push_prune"] = (v.lower() not in ("false", "0", "no", "")
                         if isinstance(v, str) else bool(v))
    return out


def write(root, values):
    merged = coerce({**read(root), **(values or {})})
    os.makedirs(root, exist_ok=True)
    tmp = path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, path(root))
    return merged
