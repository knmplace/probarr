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
from .normalize import Normalizer
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


# UK regional-opt-out abbreviations, as broadcasters actually abbreviate
# them in a channel list (BBC One/Two, ITV each carry ~15 of these; Sky's
# own guide names are the confirmed source for several of these exact
# spellings). Best-effort and expandable, not exhaustive -- a family this
# doesn't recognise just falls back to one row per variant, same as before
# this existed, rather than failing.
_REGION_WORDS = [
    "CHANNEL ISLANDS", "CI", "EAST MIDLANDS", "EMID", "EAST", "LONDON", "LON",
    "NORTH EAST", "NORTHEAST", "NE", "NORTHERN IRELAND", "NI", "NORTH WEST",
    "NORTHWEST", "NW", "SCOTLAND", "SCOT", "SOUTH EAST", "SOUTHEAST", "SE",
    "SOUTH WEST", "SOUTHWEST", "SW", "SOUTH", "STH", "WALES", "WAL",
    "WEST MIDLANDS", "WM", "WEST", "WST", "YORKS AND LINCS", "Y&L",
    "YORKSHIRE", "YORKS", "YKS",
]
_REGION_RE = None   # built lazily, once, from _REGION_WORDS
_QUALITY_RE = None  # built lazily, once, from normalize.QUALITY_TAGS


def _strip_region(name):
    """`name` with a trailing quality tag and/or UK regional-variant word
    removed, if present.

    Real guides glue these onto the name with NO separator at all
    ("BBC One ScotHD", "BBC One EastHD") as often as they space them out
    ("BBC One CI HD") -- confirmed against a live source, not a
    hypothetical. A plain word-boundary match refuses "ScotHD" outright
    (there is no boundary between "Scot" and "HD", both are word
    characters), so both tag lists are matched WITHOUT a leading boundary
    requirement here, unlike Normalizer's own identity key -- the failure
    mode of over-stripping is a slightly-off grouping default a person can
    still correct via the picker, not a wrong stream landing on a channel,
    so this can afford to be looser than the matching-identity code is.

    Order matters in _REGION_WORDS -- longer, more specific phrases must be
    tried before the short abbreviations they contain (e.g. "SOUTH EAST"
    before "SE", so "BBC One South East" doesn't strip down to "BBC One
    South" and land in the wrong family).
    """
    global _REGION_RE, _QUALITY_RE
    if _REGION_RE is None:
        import re
        from .normalize import QUALITY_TAGS
        # A boundary that accepts a camelCase transition ("east" -> "HD" in
        # "EastHD") as well as a normal word boundary, but rejects matching
        # partway through an ordinary word -- "One" must never let "NE"
        # match its own trailing two letters just because they happen to
        # spell a region code. Holds when the preceding character is
        # anything but a letter (start-of-string, space, punctuation), OR
        # when it's specifically a LOWERCASE letter (the camelCase case) --
        # "One"'s trailing "ne" is preceded by an uppercase "O", so this
        # correctly refuses it. The lowercase check is wrapped in `(?-i:)`
        # to force case-sensitivity locally -- these patterns compile with
        # IGNORECASE overall (so "hd" matches "HD"), and under IGNORECASE a
        # bare [a-z] class ALSO matches uppercase, which silently turns
        # this whole check into "preceded by any letter or not" -- true
        # unconditionally, which is exactly what caused the "One" bug.
        boundary = r"(?:(?<![A-Za-z])|(?-i:(?<=[a-z])))"
        r_alt = "|".join(w.replace(" ", r"\s+") for w in
                         sorted(_REGION_WORDS, key=len, reverse=True))
        q_alt = "|".join(sorted(QUALITY_TAGS, key=len, reverse=True))
        _REGION_RE = re.compile(rf"\s*{boundary}(?:{r_alt})\s*$", re.IGNORECASE)
        _QUALITY_RE = re.compile(rf"\s*{boundary}(?:{q_alt})\s*$", re.IGNORECASE)
    # Quality first ("ScotHD" -> "Scot"), then region on what's left
    # ("Scot" -> ""), so a glued region+quality suffix strips fully either
    # order they were applied in the source name.
    stripped = _QUALITY_RE.sub("", name)
    stripped = _REGION_RE.sub("", stripped)
    return stripped.strip() or name


def list_channels(root, source_name, normalizer=None):
    """Every DISTINCT channel one saved EPG source declares, grouped by
    real identity -- {guide_id, guide_name, alts} for each, sorted by
    name. The bulk counterpart to search_source(): this exists so a
    wantlist can be BUILT from a guide's own channel list (tick what you
    want) instead of only checked against one, reusing a guide someone
    else has already kept current rather than hand-typing a text file
    from scratch.

    Two kinds of "same channel, listed twice" are collapsed here, for two
    different reasons:

    1. Quality variants ("BBC One" / "BBC One HD") -- not two channels at
       all, probarr already picks the best available quality among a
       channel's candidates once streams are matched, so offering both as
       separate tickable rows just means one gets ticked twice under two
       different keys. Folded with the SAME normaliser that already treats
       them as one key everywhere else in probarr, so this agrees with the
       rest of the tool rather than doing its own, different thing.
    2. Regional variants ("BBC One London" / "BBC One Scotland") -- these
       ARE genuinely different broadcasts, not a bug to collapse away, but
       a UK-wide guide can carry 15+ of them per channel and ticking
       through all of them one at a time to find your own region is real
       friction. These keep their real distinctness -- returned as ONE row
       (a chosen representative) with every other region listed under
       `alts`, so the picker can show a single line with a region dropdown
       instead of a wall of near-identical rows. Nothing is discarded;
       `alts` carries the rest through untouched.
    """
    src = epgsources_mod.get(root, source_name)
    if not src:
        raise ValueError(f"no such EPG source: {source_name}")
    g = load_cached(src["url"], root=root)
    norm = normalizer or Normalizer()

    # Pass 1: fold quality variants (SD/HD/etc) to one row per key, exactly
    # as before -- unrelated to regional grouping, done first so the
    # region pass below only ever sees one representative per real feed.
    by_key = {}
    for cid, names in g.display_names.items():
        if not names:
            continue
        name = names[0]
        key = norm.key(name) or name
        existing = by_key.get(key)
        if existing is None or len(name) < len(existing["guide_name"]):
            by_key[key] = {"guide_id": cid, "guide_name": name}

    # Pass 2: group what's left by "name with any trailing region word
    # removed". A family of one is just that channel; a family of more
    # than one is a genuine set of regional variants of the same channel.
    families = {}
    for entry in by_key.values():
        fam_key = norm.key(_strip_region(entry["guide_name"])) or entry["guide_name"]
        families.setdefault(fam_key, []).append(entry)

    out = []
    for members in families.values():
        members.sort(key=lambda c: len(c["guide_name"]))
        rep, rest = members[0], members[1:]
        if rest:
            rep = {**rep, "alts": rest}
        out.append(rep)
    out.sort(key=lambda c: c["guide_name"].lower())
    return out


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
