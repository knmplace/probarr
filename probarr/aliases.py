"""Saved name aliases: "this channel is that one, whatever it is called".

The gap this closes. The normaliser strips packaging (region prefixes,
quality tags, brackets) to decide that "UKUHD: Meridian Sports 1 UHD" and
"HEVC FHD Meridian Sports 1" are the same channel. What it cannot do is know
that a BRAND is the same channel under a different name -- UKTV renamed
Dave, Gold, Yesterday, Alibi, Drama and Eden to "U&Dave", "U&Gold" and so
on, and a provider that kept the old spelling shares not one character of
prefix with a wantlist using the new one. Measured on a real lineup: five
channels matched nothing at all while the provider carried every one of
them, nine streams deep in the case of Alibi.

An alias is the escape hatch the normaliser's docstring already promised
and the Curate page already told people to use -- but it only ever existed
as a JSON file passed to the CLI with --aliases, which is unreachable from
the browser the rest of the tool lives in. This makes it a first-class
saved thing, like providers, wantlists and EPG sources.

Shape: {folded name: canonical key}. Both sides are folded through the
same _fold() the normaliser uses, because that is exactly what
Normalizer.key() looks up -- storing the raw typed text would produce an
alias that silently never matches anything.
"""
import json
import os

from .normalize import _fold

STORE_FILE = "aliases.json"


def _path(root):
    return os.path.join(root, STORE_FILE)


def read(root):
    """{folded name: canonical key}, ready to hand to Normalizer(aliases=)."""
    try:
        with open(_path(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def list_all(root):
    """The same data as display rows, sorted for a stable page."""
    return [{"name": k, "canonical": v} for k, v in sorted(read(root).items())]


def _write(root, data):
    os.makedirs(root, exist_ok=True)
    tmp = _path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, _path(root))


def save(root, name, canonical):
    """Alias `name` to `canonical`. Both are folded on the way in.

    An alias pointing at itself is refused rather than stored: it is always
    a mistake (usually aliasing a name to the very stream name it already
    matched) and would be a no-op taking up a row forever.
    """
    n, c = _fold(name or ""), _fold(canonical or "")
    if not n or not c:
        raise ValueError("both a name and a canonical name are required")
    if n == c:
        raise ValueError("that name already normalises to the same key")
    data = read(root)
    data[n] = c
    _write(root, data)
    return {"name": n, "canonical": c}


def delete(root, name):
    n = _fold(name or "")
    data = read(root)
    if n not in data:
        return False
    data.pop(n)
    _write(root, data)
    return True
