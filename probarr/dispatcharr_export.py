"""Push a curated selection back into a Dispatcharr instance.

This is the other half of the Dispatcharr adapter: sources/dispatcharr.py
reads streams to probe, this module writes the curated result back in. It is
a direct generalisation of the original channel-sync.py pipeline this
whole project grew out of -- same self-healing re-assert pattern, same
"logos don't link themselves" gotcha, same EPG re-match trigger -- just
driven by a curated selection.json instead of a hardcoded numbering scheme.

The source Dispatcharr instance and the export TARGET are deliberately
decoupled: a run's candidates might have come from a raw M3U (probe from one
place, push results into your own Dispatcharr elsewhere), or from the same
Dispatcharr instance you are pushing back into (the common case, and the one
that matters most: no separate configuration needed, same saved connection
serves as both source and target).

Deliberate limitation, not a bug: this only ever creates and updates, never
deletes. If a channel's source URL changes between runs, get_or_create_custom_
stream() creates a new custom stream and re-points the channel at it, but the
OLD stream row is left behind, now unreferenced by any channel. Likewise, a
channel dropped from the curated set on a later run is never removed from
Dispatcharr -- only what IS currently curated gets touched. Repeated exports
over time will accumulate orphaned streams and stale channels if URLs rotate
or a wantlist shrinks; nothing here prunes that automatically. Considered and
explicitly declined: an opt-in "remove anything no longer selected" cleanup
step, on the reasoning that a shrunk wantlist or a bad run silently deleting
real channels is worse than a manual tidy-up in Dispatcharr's own UI when
actually needed.
"""


def _expand(channels, fallback_mode):
    """Flatten curated channels into the concrete channel rows a push writes.

    Separated out so plan() and push() cannot disagree about what a given
    fallback_mode actually produces -- "separate" mode turning one curated
    channel into two real ones is exactly the kind of detail a preview must
    get right to be worth trusting.
    """
    rows = []
    for ch in channels:
        primary = ch.get("primary")
        if not primary:
            continue
        fallback = ch.get("fallback")
        # An ordered list of any length, falling back to the old two-slot
        # shape for callers that still send it. Dispatcharr's own model is
        # an ordered streams array with failover down it, so a curated
        # order of three or five maps onto it directly -- there was never
        # a reason to stop at two beyond probarr's own data model.
        ordered = [x["stream_id"] for x in (ch.get("streams") or [])
                   if x and x.get("stream_id") is not None]
        if fallback_mode == "native":
            rows.append((ch, ch.get("number"), ch["name"],
                        ordered or [primary["stream_id"],
                                    fallback["stream_id"] if fallback else None],
                        ch.get("logo_url"), ch.get("epg_data_id"),
                        ch.get("group")))
        else:
            # "Separate" keeps its meaning: the first stream on the channel,
            # the second as its own FALLBACK: row. A curated list of three or
            # more only makes sense natively, so anything past the second is
            # deliberately ignored here rather than silently inventing more
            # channels than the operator asked for.
            rows.append((ch, ch.get("number"), ch["name"],
                        [ordered[0] if ordered else primary["stream_id"]],
                        ch.get("logo_url"), ch.get("epg_data_id"), ch.get("group")))
            if len(ordered) > 1:
                fallback = {"stream_id": ordered[1]}
            if fallback:
                fb_number = None if ch.get("number") is None else ch["number"] + 0.5
                rows.append((ch, fb_number, f"FALLBACK: {ch['name']}",
                            [fallback["stream_id"]], ch.get("logo_url"), None,
                            ch.get("group")))
    return rows


def _decide(existing_ch, name, stream_ids, target_group_id, logo_id, epg_data_id):
    """What this one channel row needs: ("create"|"update"|"unchanged", changes, payload).

    Pure -- no client, no writes. This is the single source of truth for
    "what would change", so a previewed plan and the push that follows it
    are computed by the same code rather than two implementations that
    drift apart (the failure mode that makes dry-run features untrustworthy
    and therefore unused).
    """
    payload = {"name": name, "channel_group_id": target_group_id, "streams": stream_ids}
    if logo_id:
        payload["logo_id"] = logo_id
    if epg_data_id:
        payload["epg_data_id"] = epg_data_id

    if not existing_ch:
        return "create", [{"field": "channel", "from": None, "to": "new"}], payload

    changes = []
    if existing_ch.get("streams") != stream_ids:
        changes.append({"field": "streams",
                       "from": existing_ch.get("streams"), "to": stream_ids})
    if existing_ch.get("name") != name:
        changes.append({"field": "name",
                       "from": existing_ch.get("name"), "to": name})
    if existing_ch.get("channel_group_id") != target_group_id:
        changes.append({"field": "group",
                       "from": existing_ch.get("channel_group_id"), "to": target_group_id})
    if logo_id and existing_ch.get("logo_id") != logo_id:
        changes.append({"field": "logo",
                       "from": existing_ch.get("logo_id"), "to": logo_id})
    if epg_data_id and existing_ch.get("epg_data_id") != epg_data_id:
        changes.append({"field": "epg",
                       "from": existing_ch.get("epg_data_id"), "to": epg_data_id})
    return ("update" if changes else "unchanged"), changes, payload


def plan(client, channels, group_name=None, default_group_name="probarr",
        fallback_mode="native"):
    """What a push WOULD do, computed without writing anything.

    Exists because every silent-success bug this exporter has had shared a
    shape: the push reported "done" while having applied something other
    than what the operator expected (a channel relocated to a new group, an
    EPG link set with no programme data behind it, a whole run applying
    nothing at all). A reviewable diff turns that entire class of problem
    from "discovered afterwards, in Dispatcharr" into "seen before it
    happens".

    Read-only in the strict sense: it never creates a group and never
    creates a custom stream. A stream that does not exist in the target yet
    is reported as such rather than being brought into existence just to
    describe it -- planning must not have side effects, or previewing
    becomes as consequential as pushing.
    """
    existing = client.channels()
    by_number = {c["channel_number"]: c for c in existing
                 if c.get("channel_number") is not None}
    logo_by_url = client.logo_map()
    groups_by_name = {g.get("name", "").strip().lower(): g["id"] for g in client.groups()}
    group_names = {g["id"]: g.get("name") for g in client.groups()}

    actions = []
    for (ch, number, name, stream_ids, logo_url, epg_data_id,
         ch_group) in _expand(channels, fallback_mode):
        stream_ids = [s for s in stream_ids if s is not None]
        if not stream_ids:
            continue
        existing_ch = by_number.get(float(number)) if number is not None else None
        if ch_group:
            wanted = ch_group
            target_group_id = groups_by_name.get(wanted.strip().lower())
            if target_group_id is None:
                group_names[f"new:{wanted}"] = wanted
                target_group_id = f"new:{wanted}"
        elif existing_ch and group_name is None:
            target_group_id = existing_ch.get("channel_group_id")
        else:
            wanted = group_name or default_group_name
            target_group_id = groups_by_name.get(wanted.strip().lower())
            if target_group_id is None:
                group_names[f"new:{wanted}"] = wanted
                target_group_id = f"new:{wanted}"
        logo_id = logo_by_url.get(logo_url) if logo_url else None
        kind, changes, _ = _decide(existing_ch, name, stream_ids,
                                   target_group_id, logo_id, epg_data_id)
        for c in changes:
            if c["field"] == "group":
                c["from_name"] = group_names.get(c["from"])
                c["to_name"] = group_names.get(c["to"], str(c["to"]))
        actions.append({"number": number, "name": name, "kind": kind,
                       "changes": changes})

    counts = {"create": 0, "update": 0, "unchanged": 0}
    for a in actions:
        counts[a["kind"]] += 1
    return {"actions": actions, "counts": counts}


def push(client, channels, group_name=None, default_group_name="probarr",
        fallback_mode="native", log=None, progress_cb=None,
        prune_empty_groups=True):
    """Push curated channels into Dispatcharr.

    `channels`: list of dicts, each {number, name, primary: {stream_id...},
    fallback: {stream_id...} or None, logo_url}. This is exactly the shape
    curate.build_payload()'s channel entries + the curator's selection reduce
    to -- see web.py's _export_dispatcharr for how they are assembled.

    `group_name`: None (the default) means "don't move anything that
    already exists" -- an EXISTING channel (matched by number) KEEPS
    whatever group it is already in, exactly like updating any other field
    only touches what actually changed. Only explicitly setting this moves
    an existing channel to a different group. Earlier versions of this
    function always resolved one target group up front and moved every
    existing channel into it regardless -- real bug, confirmed live twice:
    a blank group field (the common case, especially for a single-channel
    push) silently relocated a channel out of its real lineup group and
    into a fresh "probarr (<run>)" group of its own.

    `prune_empty_groups`: after moving channels, delete any group THIS PUSH
    emptied. Deliberately scoped to groups the push itself vacated, never
    "every empty group in Dispatcharr" -- an instance fed from a large M3U
    legitimately carries hundreds of empty groups mirroring the provider's
    own group-titles, and deleting those would be destroying something this
    tool does not own. A group that still holds channels belonging to
    anything else is left alone.

    `default_group_name`: used ONLY for a channel that does not exist yet
    (nothing to preserve, so a real default is needed) when `group_name`
    was not explicitly given. Irrelevant to any channel that already
    exists, by design.

    `fallback_mode`:
      "native"   -- one channel, streams=[primary, fallback]. Dispatcharr's
                    own failover tries the next stream in the list when the
                    current one dies. No lineup clutter, but the fallback is
                    invisible in the channel list itself.
      "separate" -- a second channel, name-prefixed "FALLBACK: ", streams
                    only the fallback. Doubles the lineup but makes the
                    fallback visible and individually selectable.
      There is deliberately no default in the UI -- this is a real behavioural
      choice with different trade-offs, not a technical detail to bury.

    Returns a summary dict: {created, updated, errors: [...]}.

    progress_cb(done, total, channel_name): called after each channel is
    processed (success or error), so a caller can report live push progress
    rather than only a single result at the very end.
    """
    log = log or (lambda msg: None)
    progress_cb = progress_cb or (lambda done, total, name: None)
    # Each resolved lazily, at most once, and only if actually needed -- a
    # pure "update everything in place, no explicit group" push over an
    # already-fully-existing lineup may never need to touch the groups
    # endpoint at all.
    _group_ids = {}
    def group_id_for(name):
        if name not in _group_ids:
            gid = client.get_or_create_group(name)
            log(f"group '{name}' (id {gid})")
            _group_ids[name] = gid
        return _group_ids[name]

    existing = client.channels()
    by_number = {c["channel_number"]: c for c in existing if c.get("channel_number") is not None}
    logo_by_url = client.logo_map()

    created, updated, errors, changed_ids = 0, 0, [], []
    # Channels a curator explicitly picked an EPG source for (see epg_data_id
    # below) -- excluded from the generic match_epg() bulk auto-match at the
    # end, because that call re-matches against WHATEVER Dispatcharr's own
    # algorithm prefers and would silently overwrite an explicit choice right
    # back to the default the curator was specifically trying to move away
    # from.
    explicit_epg_ids = set()

    unchanged = 0
    # Groups a channel was moved OUT of by this push -- candidates for
    # pruning once everything has been applied.
    vacated = set()
    rows = _expand(channels, fallback_mode)
    for i, (ch, number, name, stream_ids, logo_url, epg_data_id,
            ch_group) in enumerate(rows):
        try:
            stream_ids = [s for s in stream_ids if s is not None]
            if not stream_ids:
                continue
            existing_ch = by_number.get(float(number)) if number is not None else None
            if ch_group:
                # An explicit per-channel group beats everything: it is a
                # decision about THIS channel, more specific than the
                # export form's blanket group and than whatever group the
                # channel happens to sit in today.
                target_group_id = group_id_for(ch_group)
            elif existing_ch and group_name is None:
                # Nothing to preserve for a channel that doesn't exist yet,
                # but an existing one keeps its current group unless the
                # caller explicitly asked to move it.
                target_group_id = existing_ch.get("channel_group_id")
            else:
                target_group_id = group_id_for(group_name or default_group_name)
            logo_id = logo_by_url.get(logo_url) if logo_url else None

            # Same _decide() the preview used -- a plan shown to the operator
            # and the push that follows it are computed by one implementation,
            # not two that can drift.
            kind, changes, payload = _decide(existing_ch, name, stream_ids,
                                             target_group_id, logo_id, epg_data_id)
            if kind == "update":
                was = existing_ch.get("channel_group_id")
                if was is not None and was != target_group_id:
                    vacated.add(was)
                client.update_channel(existing_ch["id"], payload)
                updated += 1
                changed_ids.append(existing_ch["id"])
                log(f"  {name}: updated ("
                   + ", ".join(c["field"] for c in changes) + ")")
            elif kind == "create":
                if number is not None:
                    payload["channel_number"] = number
                new_ch = client.create_channel(payload)
                created += 1
                changed_ids.append(new_ch["id"])
                log(f"  {name}: created")
            else:
                unchanged += 1
                log(f"  {name}: unchanged")
            if epg_data_id:
                explicit_epg_ids.add(
                    existing_ch["id"] if existing_ch else changed_ids[-1])
        except Exception as e:
            errors.append({"channel": name, "error": str(e)[:200]})
            log(f"  {name}: ERROR {e}")
        finally:
            progress_cb(i + 1, len(rows), name)

    auto_match_ids = [cid for cid in changed_ids if cid not in explicit_epg_ids]
    if auto_match_ids:
        client.match_epg(auto_match_ids)
        log(f"EPG auto-match triggered for {len(auto_match_ids)} changed channel(s)")
    if explicit_epg_ids:
        log(f"{len(explicit_epg_ids)} channel(s) kept their explicitly-chosen "
           f"EPG source, not auto-matched")

    pruned = []
    if prune_empty_groups and vacated:
        try:
            # Re-fetched AFTER the writes: channel_count is computed
            # server-side, so counting from the pre-push snapshot would
            # both miss groups just emptied and wrongly condemn groups
            # just filled.
            for g in client.groups():
                if g["id"] in vacated and (g.get("channel_count") or 0) == 0:
                    client.api("DELETE", f"/api/channels/groups/{g['id']}/")
                    pruned.append(g.get("name"))
            if pruned:
                log("removed now-empty group(s): " + ", ".join(map(str, pruned)))
        except Exception as e:
            log(f"  group prune skipped: {e}")

    return {"created": created, "updated": updated, "unchanged": unchanged,
           "errors": errors, "changed": len(changed_ids), "pruned": pruned}
