"""Compare a channel against every saved EPG source, live.

The run-time `expected` field on a probe result answers "what did the guide
say was on AT THE MOMENT this stream was probed" -- useful for spotting a
mismatched feed, but frozen the instant the run finished. It answers nothing
about whether that guide is still the right one to trust, or whether a
different saved EPG source would have matched the channel more precisely
(a real, motivating case: a household adds a second EPG source specifically
because the run's original one turned out unreliable for some channels, and
then has no way in Curate to see the alternative's opinion side by side).

This module answers "what does each saved source say is on RIGHT NOW",
checked live rather than only at probe time, across every source at once so
a curator can pick the one that actually lines up with the picture.
"""
import datetime
import hashlib
import os
import time
import urllib.request

from .epg import Guide
from . import epgsources as epgsources_mod

# XMLTV files are typically refreshed by their publisher on the order of
# hours, not seconds -- caching each parsed Guide keeps a "check every
# channel against every source" pass from re-fetching and re-parsing a
# multi-MB file (seconds each, see runner.py's own EPG load) on every single
# click. 10 minutes balances staying current against that repeated cost.
_CACHE_TTL = 600
_cache = {}  # url -> (Guide, loaded_at)

# How long a downloaded XMLTV file is reused from disk. Deliberately hours,
# not minutes: a real aggregator rate-limits downloads (open-epg allows 20
# per file per day and returns an HTML "download limit reached" page after
# that, which parses as junk rather than failing cleanly). The in-memory
# cache alone could not protect against this -- it dies with the process,
# so every container restart re-downloaded every source, and a day of
# ordinary deploys was enough to exhaust the allowance.
_DISK_TTL = 6 * 3600
_DISK_DIR = "epg_cache"


def _disk_path(root, url):
    d = os.path.join(root, _DISK_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha256(url.encode()).hexdigest()[:16] + ".xml")


def _fetch_to_disk(root, url):
    """Download an XMLTV source to the on-disk cache, reusing a recent copy.

    Returns the local path. Written via a temp file and atomic replace so an
    interrupted download can never leave a truncated file that later parses
    as junk.
    """
    path = _disk_path(root, url)
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < _DISK_TTL:
        return path
    req = urllib.request.Request(url, headers={"User-Agent": "probarr/0.1"})
    raw = urllib.request.urlopen(req, timeout=120).read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    head = raw[:400].lstrip()
    if not head.startswith(b"<?xml") and not head.startswith(b"<tv"):
        # A rate-limit or error page, not a guide. Kept OUT of the cache and
        # reported with what the server actually said, rather than surfacing
        # later as an opaque XML parse error on every single channel.
        txt = " ".join(head.decode("utf-8", "replace").split())
        import re as _re
        txt = _re.sub(r"<[^>]+>", " ", txt)
        raise ValueError("source did not return XMLTV: "
                         + " ".join(txt.split())[:180])
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    return path


def load_cached(url, window_hours=6, root=None):
    """A small-window Guide for `url`, reused across calls within the TTL.

    window_hours is deliberately narrow (vs runner.py's 48h probing window)
    -- this only ever needs to answer "what's on right now", so there is no
    reason to parse or hold two days of programmes in memory for it.
    """
    now = time.time()
    hit = _cache.get(url)
    if hit and (now - hit[1]) < _CACHE_TTL:
        return hit[0]
    src = _fetch_to_disk(root, url) if root else url
    g = Guide.load(src, window_hours=window_hours)
    _cache[url] = (g, now)
    return g


# The Guide object itself is cached, but its name index is a normalised
# view over it (folded, aliased) and used to be rebuilt from scratch on
# every single check -- walking and normalising every channel name in the
# file, for every saved source, on every click. Real cost on a 6,000-entry
# guide, thrown away for nothing since neither the file nor the aliases had
# changed since the last click. Indexed once per (url, aliases) pair here.
_indexed = {}   # url -> (aliases signature, indexed Guide)


def _indexed_guide(url, normalizer, root):
    g = load_cached(url, root=root)
    sig = tuple(sorted(normalizer.aliases.items()))
    hit = _indexed.get(url)
    if hit and hit[0] == sig and hit[1] is g:
        return g
    g.build_name_index(normalizer)
    _indexed[url] = (sig, g)
    return g


def search_source(root, source_name, query, normalizer, limit=25):
    """Search one saved EPG source's channel names for `query`, live --
    the manual counterpart to resolve(): a person filtering a real list of
    names instead of trusting a fuzzy match. Returns
    [{"guide_id", "guide_name", "now": programme-dict-or-None}, ...].
    Raises if the source name isn't saved or its guide can't be loaded --
    the caller is expected to turn that into an error response, same as
    every other EPG lookup in this module.
    """
    src = epgsources_mod.get(root, source_name)
    if not src:
        raise ValueError(f"no such EPG source: {source_name}")
    g = _indexed_guide(src["url"], normalizer, root)
    at = datetime.datetime.now(datetime.timezone.utc)
    return [{"guide_id": cid, "guide_name": name, "now": g.now_playing(cid, at)}
            for cid, name in g.search(query, limit=limit)]


def check_all(root, name, tvg_id, normalizer, overrides=None):
    """{source_name: {"matched": bool, "now": programme-dict-or-None}} for
    every saved EPG source, plus a "matched" flag so a source that doesn't
    carry this channel at all is visibly distinct from one that does but has
    nothing scheduled at this exact moment.

    `overrides` is an optional {source_name: guide_channel_id} map -- a
    person's manual pick from Check EPG's search, made when the automatic
    resolve() guessed wrong or missed a channel filed under an odd name.
    A source named in it uses that exact id directly instead of resolving,
    as long as the id still exists in that source (a source can be
    refreshed and drop an id between the pick and this call).
    """
    sources = epgsources_mod.list_all(root)
    overrides = overrides or {}
    at = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for src in sources:
        entry = {"source": src["name"], "matched": False, "now": None, "error": None,
                "guide_id": None, "guide_name": None}
        try:
            g = _indexed_guide(src["url"], normalizer, root)
            override_id = overrides.get(src["name"])
            cid = (override_id if override_id and override_id in g.display_names
                   else g.resolve(tvg_id, name, normalizer))
            if cid:
                entry["matched"] = True
                entry["now"] = g.now_playing(cid, at)
                # WHICH channel entry a source actually matched, not just
                # what is airing on it -- a fuzzy or ambiguous match can
                # easily land on the wrong entry while still returning a
                # perfectly plausible programme, so the entry itself has to
                # be checkable, not just its schedule.
                entry["guide_id"] = cid
                names = g.display_names.get(cid) or []
                entry["guide_name"] = names[0] if names else cid
        except Exception as e:
            entry["error"] = str(e)[:200]
        out.append(entry)
    return out
