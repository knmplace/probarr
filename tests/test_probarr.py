"""probarr's test suite. Standard library only, no network, no ffmpeg.

Deliberately covers the pure functions and the file formats rather than the
probing: the parts that decide what a channel IS, what gets exported, and
what is remembered are exactly the parts whose failures are silent. A
mis-ranked candidate or a dropped tvg-id does not raise -- it just quietly
produces the wrong lineup, which is the failure mode this project has
actually shipped more than once.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probarr import aliases as aliases_mod
from probarr import curate, lineups, pages, providers, wantlist as wl
from probarr.normalize import Normalizer, group_candidates, declared_quality_rank
from probarr.rank import rank
from probarr.sources import m3u
from probarr.sources.base import Stream
from probarr.store import RunStore


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="probarr-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class TestNormalize(unittest.TestCase):
    def setUp(self):
        self.n = Normalizer()

    def test_packaging_is_stripped_to_one_key(self):
        names = ["UK: Meridian Sports 1", "UKFHD | Meridian Sports 1",
                 "UKUHD: Meridian Sports 1 UHD", "HEVC FHD Meridian Sports 1",
                 "Meridian Sports 1 HD [Multi-Audio]"]
        self.assertEqual(len({self.n.key(x) for x in names}), 1)

    def test_ukraine_is_not_uk(self):
        # The regression that put a Ukrainian feed on a British channel.
        self.assertNotEqual(self.n.region_of("UKRAINE: Futbol 1"), "UK")
        self.assertIn("RAINE", self.n.key("UKRAINE: Futbol 1"))

    def test_trailing_country_tag_is_also_detected(self):
        # Region tags aren't always a leading prefix -- some providers put
        # the country at the END of the name instead ("Cartoon Network | US").
        self.assertEqual(self.n.region_of("Cartoon Network | US"), "US")
        self.assertEqual(self.n.region_of("Discovery HD - UK"), "UK")
        # Must not fire on a bare substring with no separator boundary --
        # the same class of false positive test_ukraine_is_not_uk guards
        # against on the prefix side.
        self.assertIsNone(self.n.region_of("Futbol Ukraine"))

    def test_timeshift_is_a_different_channel(self):
        # The guarantee is that a +1 never lands in the base channel's pool,
        # whichever way the provider spells it.
        self.assertTrue(self.n.is_timeshift("UK: Gold +1"))
        self.assertNotEqual(self.n.key("UK: Gold +1"), self.n.key("UK: Gold"))
        self.assertNotEqual(self.n.key("UK+1 YESTERDAY+1"),
                            self.n.key("UK: Yesterday"))

    def test_alias_connects_a_renamed_brand(self):
        n = Normalizer(aliases={"UANDDRAMA": "DRAMA"})
        self.assertEqual(n.key("U&Drama"), n.key("UK: DRAMA SD"))

    def test_declared_quality_orders_candidates(self):
        self.assertGreater(declared_quality_rank("UKUHD: Channel 4K"),
                           declared_quality_rank("UK: Channel HD"))

    def test_grouping_buckets_by_identity(self):
        streams = [Stream(id=str(i), name=n, url=f"http://x/{i}")
                   for i, n in enumerate(["UK: BBC One", "UKFHD BBC One HD",
                                          "UK: ITV1"])]
        pools = group_candidates(streams, self.n)
        self.assertEqual(sorted(len(v) for v in pools.values()), [1, 2])


class TestWantlist(unittest.TestCase):
    def test_parses_number_name_and_tvg_id(self):
        chans, _ = wl.parse_detailed(
            "101: BBC One | bbc.one.uk\nBBC Four\n# comment\n\n", Normalizer())
        self.assertEqual(chans[0].number, 101)
        self.assertEqual(chans[0].tvg_id, "bbc.one.uk")
        self.assertEqual(chans[1].name, "BBC Four")
        self.assertEqual(len(chans), 2)

    def test_token_sort_stage_is_off_by_default(self):
        # "strict" (the default) must never invent a match a person didn't
        # explicitly ask for -- every wantlist's behaviour before this stage
        # existed has to keep reporting genuinely word-reordered names as
        # missing, not start silently guessing.
        norm = Normalizer()
        wanted, _ = wl.parse_detailed("Meridian Sports 1", norm)
        pools = {norm.key("Sports 1 Meridian"):
                [Stream(id="1", name="Sports 1 Meridian", url="http://x/1")]}
        filtered, missing, fuzzy = wl.apply(wanted, pools)
        self.assertEqual(filtered, {})
        self.assertEqual(len(missing), 1)
        self.assertEqual(fuzzy, [])

    def test_token_sort_stage_catches_reordered_words_when_enabled(self):
        norm = Normalizer()
        wanted, _ = wl.parse_detailed("Meridian Sports 1", norm)
        pools = {norm.key("Sports 1 Meridian"):
                [Stream(id="1", name="Sports 1 Meridian", url="http://x/1")]}
        filtered, missing, fuzzy = wl.apply(wanted, pools, sensitivity="normal")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(missing, [])
        self.assertEqual(len(fuzzy), 1)

    def test_token_sort_stage_refuses_an_ambiguous_pair(self):
        # Two candidates too close in score to call must still be refused,
        # the same rule every other stage in this module already follows.
        # Reordered so neither candidate is a prefix/suffix of the other or
        # of the wanted key -- this has to reach token-sort itself, not get
        # resolved (or mis-resolved) by an earlier stage first.
        norm = Normalizer()
        wanted, _ = wl.parse_detailed("Alpha Bravo Charlie", norm)
        pools = {
            norm.key("Bravo Charlie Alphaa"):
                [Stream(id="1", name="Bravo Charlie Alphaa", url="http://x/1")],
            norm.key("Charlie Alpha Bravoo"):
                [Stream(id="2", name="Charlie Alpha Bravoo", url="http://x/2")],
        }
        filtered, missing, fuzzy = wl.apply(wanted, pools, sensitivity="relaxed")
        self.assertEqual(filtered, {})
        self.assertEqual(len(missing), 1)

    def test_render_round_trips_number_group_and_tvg_id(self):
        norm = Normalizer()
        text = "[News]\n101: BBC News | bbc.news.uk\n\n[Sport]\n401: Sky Sports F1\n"
        chans, _ = wl.parse_detailed(text, norm)
        rendered = wl.render(chans)
        chans2, warnings2 = wl.parse_detailed(rendered, norm)
        self.assertEqual(warnings2, [])
        self.assertEqual([c.as_dict() for c in chans], [c.as_dict() for c in chans2])

    def test_group_together_collapses_a_scattered_group_into_one_run(self):
        norm = Normalizer()
        # News/Sport/News: the second News channel is scattered away from
        # the first by a Sport channel in between -- group_together must
        # pull both News channels together into a single contiguous block,
        # not just leave file order alone.
        text = "[News]\nBBC News\n[Sport]\nSky Sports F1\n[News]\nSky News\n"
        chans, _ = wl.parse_detailed(text, norm)
        grouped = wl.group_together(chans)
        self.assertEqual([c.name for c in grouped],
                         ["BBC News", "Sky News", "Sky Sports F1"])
        rendered = wl.render(grouped)
        self.assertEqual(rendered.count("[News]"), 1)
        self.assertEqual(rendered.count("[Sport]"), 1)

    def test_group_together_keeps_relative_order_within_a_group(self):
        norm = Normalizer()
        text = "[News]\nBBC News\nSky News\nITV News\n"
        chans, _ = wl.parse_detailed(text, norm)
        grouped = wl.group_together(chans)
        self.assertEqual([c.name for c in grouped], ["BBC News", "Sky News", "ITV News"])

    def test_group_together_pushes_the_ungrouped_bucket_to_the_end(self):
        # A wide EPG import against a narrow reference lineup leaves most
        # channels with no group at all -- if that bucket happens to appear
        # early in the file, it must not bury groups discovered later in
        # a wall of blanks (this is exactly what real reference-lineup
        # enrichment looked like: Entertainment matched, then a huge
        # unmatched run, with News/Sports/etc still fully numbered further
        # down -- easy to mistake for enrichment having stopped working).
        norm = Normalizer()
        text = "[Entertainment]\n4seven\n[]\nUnmatched One\nUnmatched Two\n[News]\nBBC News\n"
        chans, _ = wl.parse_detailed(text, norm)
        grouped = wl.group_together(chans)
        self.assertEqual([c.group for c in grouped],
                         ["Entertainment", "News", None, None])

    def test_channels_from_reference_builds_a_full_wantlist(self):
        norm = Normalizer()
        data = {"categories": {"News": [{"name": "BBC News", "number": 503},
                                         {"name": "CNBC", "number": 505}],
                                "Sport": [{"name": "Sky Sports F1", "number": 407}]}}
        chans = wl.channels_from_reference(data, norm)
        self.assertEqual(len(chans), 3)
        by_name = {c.name: c for c in chans}
        self.assertEqual(by_name["BBC News"].number, 503)
        self.assertEqual(by_name["BBC News"].group, "News")
        self.assertEqual(by_name["Sky Sports F1"].group, "Sport")
        rendered = wl.render(wl.group_together(chans))
        self.assertIn("[News]", rendered)
        self.assertIn("503: BBC News", rendered)

    def test_channels_from_reference_drops_duplicate_names_across_categories(self):
        # A name appearing in two categories (real data does this) must not
        # produce two lines for the same channel -- first one wins, same
        # rule as reference_lineup_map.
        norm = Normalizer()
        data = {"categories": {"A": [{"name": "Foo", "number": 1}],
                                "B": [{"name": "Foo", "number": 2}]}}
        chans = wl.channels_from_reference(data, norm)
        self.assertEqual(len(chans), 1)
        self.assertEqual(chans[0].number, 1)

    def test_channels_from_reference_rejects_unrecognised_shape(self):
        with self.assertRaises(ValueError):
            wl.channels_from_reference({"channels": []}, Normalizer())

    def test_reference_lineup_map_flattens_categories(self):
        data = {"categories": {"News": [{"name": "BBC News", "number": 231}],
                                "Sport": [{"name": "Sky Sports F1", "number": 401}]}}
        norm = Normalizer()
        m = wl.reference_lineup_map(data, norm)
        self.assertEqual(m[norm.key("BBC News")], (231, "News"))
        self.assertEqual(m[norm.key("Sky Sports F1")], (401, "Sport"))

    def test_reference_lineup_map_rejects_unrecognised_shape(self):
        with self.assertRaises(ValueError):
            wl.reference_lineup_map({"channels": []}, Normalizer())

    def test_reference_label_splits_country_and_package(self):
        self.assertEqual(wl._reference_label("UK_SkyTV_lineup.json"),
                          ("United Kingdom", "SkyTV"))
        self.assertEqual(wl._reference_label("US_DISH-Top250_lineup.json"),
                          ("United States", "DISH Top250"))
        self.assertEqual(wl._reference_label("plugin.json")[0], "Other")

    def test_enrich_only_fills_gaps_never_overwrites(self):
        norm = Normalizer()
        # "BBC News" has no number/group and should be filled; "Sky Sports F1"
        # already has both and must be left exactly as the operator set them.
        chans, _ = wl.parse_detailed(
            "BBC News\n[Football]\n999: Sky Sports F1\n", norm)
        ref = wl.reference_lineup_map(
            {"categories": {"News": [{"name": "BBC News", "number": 231}],
                             "Sport": [{"name": "Sky Sports F1", "number": 401}]}}, norm)
        chans, matched = wl.enrich_with_reference(chans, ref)
        self.assertEqual(matched, 1)
        by_name = {c.name: c for c in chans}
        self.assertEqual(by_name["BBC News"].number, 231)
        self.assertEqual(by_name["BBC News"].group, "News")
        self.assertEqual(by_name["Sky Sports F1"].number, 999)
        self.assertEqual(by_name["Sky Sports F1"].group, "Football")


class TestProbeQueueGate(unittest.TestCase):
    def test_gate_is_called_with_the_next_jobs_lane(self):
        # Real bug: the viewer gate used to be asked with no arguments at
        # all, so it had no way to know which provider's connections a live
        # viewer was actually competing against -- it could only ever
        # assume a single shared connection, even for a lane saved with
        # its own higher concurrency. The queue must pass the lane of
        # whichever job would launch next.
        import time as time_mod
        from probarr.probequeue import ProbeQueue

        seen_lanes = []
        def gate(lane=None):
            seen_lanes.append(lane)
            return None   # never block, just observe what we were asked

        results = []
        def runner(payload):
            results.append(payload["lane"])
            return {"status": "ok"}

        q = ProbeQueue(runner, concurrency=lambda: 1, gap=lambda: 0,
                       gate=gate)
        q.submit("k1", {"lane": "mybunny"})
        for _ in range(50):
            if results:
                break
            time_mod.sleep(0.02)
        self.assertIn("mybunny", results)
        self.assertIn("mybunny", seen_lanes)

    def test_gate_without_a_lane_parameter_still_works(self):
        # Backward compatibility: a gate written before the lane argument
        # existed (just `lambda: None`) must not break the queue.
        import time as time_mod
        from probarr.probequeue import ProbeQueue

        results = []
        q = ProbeQueue(lambda payload: results.append(payload["lane"]) or {"status": "ok"},
                       concurrency=lambda: 1, gap=lambda: 0,
                       gate=lambda: None)
        q.submit("k1", {"lane": "mybunny"})
        for _ in range(50):
            if results:
                break
            time_mod.sleep(0.02)
        self.assertIn("mybunny", results)

    def test_settle_gap_applies_only_when_the_lane_was_genuinely_full(self):
        # User-specified rule: if a lane's connections (probes + live
        # viewers) are all in use, wait LANE_SETTLE_SECONDS after one frees
        # before reusing it -- a provider seen live to serve a connection
        # accepted too soon after the previous one closed as corrupted
        # (decode errors, no frame produced) rather than cleanly refuse it.
        # But a lane with genuine spare capacity must never pay this: two
        # probes running against a 4-connection lane with one viewer still
        # leaves a free slot, and a third probe should start immediately.
        import time as time_mod
        import threading as threading_mod
        from probarr import probequeue as pq_mod
        from probarr.probequeue import ProbeQueue

        orig_settle = pq_mod.LANE_SETTLE_SECONDS
        pq_mod.LANE_SETTLE_SECONDS = 0.3
        try:
            release = threading_mod.Event()
            started = []
            finished = []
            def runner(payload):
                started.append((payload["key"], time_mod.time()))
                if payload["key"] == "hold":
                    release.wait(timeout=2)
                finished.append((payload["key"], time_mod.time()))
                return {"status": "ok"}

            # limit=2, one viewer -> only ONE probe slot genuinely free.
            q = ProbeQueue(runner, concurrency=lambda: 2, gap=lambda: 0,
                           lane_limit=lambda lane: 2, viewer_count=lambda lane: 1)
            q.submit("hold", {"lane": "L", "key": "hold"})
            for _ in range(50):
                if started:
                    break
                time_mod.sleep(0.02)
            self.assertEqual(len(started), 1)   # the lane was already full (1 probe + 1 viewer = 2)

            q.submit("next", {"lane": "L", "key": "next"})
            release.set()   # "hold" finishes now -- lane was full, settle gap must apply
            for _ in range(100):
                if len(started) >= 2:
                    break
                time_mod.sleep(0.02)
            gap = started[1][1] - finished[0][1]
            self.assertGreaterEqual(gap, pq_mod.LANE_SETTLE_SECONDS * 0.8,
                                    "next probe started before the settle gap elapsed")
        finally:
            pq_mod.LANE_SETTLE_SECONDS = orig_settle

    def test_settle_gap_does_not_apply_when_the_lane_has_spare_capacity(self):
        import time as time_mod
        from probarr import probequeue as pq_mod
        from probarr.probequeue import ProbeQueue

        orig_settle = pq_mod.LANE_SETTLE_SECONDS
        pq_mod.LANE_SETTLE_SECONDS = 5   # deliberately large -- must not be waited for
        try:
            started = []
            def runner(payload):
                started.append(payload["key"])
                return {"status": "ok"}

            # limit=4, one viewer, one probe running at most -> always spare.
            q = ProbeQueue(runner, concurrency=lambda: 4, gap=lambda: 0,
                           lane_limit=lambda lane: 4, viewer_count=lambda lane: 1)
            q.submit("a", {"lane": "L", "key": "a"})
            q.submit("b", {"lane": "L", "key": "b"})
            t0 = time_mod.time()
            for _ in range(100):
                if len(started) >= 2:
                    break
                time_mod.sleep(0.02)
            self.assertLess(time_mod.time() - t0, 2,
                            "second probe waited as if the lane were full")
            self.assertEqual(set(started), {"a", "b"})
        finally:
            pq_mod.LANE_SETTLE_SECONDS = orig_settle

    def test_same_channel_candidates_never_run_simultaneously(self):
        # Real, directly-evidenced case: two quality-variant candidates of
        # ONE channel launched in the same second (genuine lane capacity
        # to spare -- this isn't the settle-gap case) and both came back
        # corrupted, while the exact same URL decoded perfectly cleanly in
        # complete isolation moments later. The provider's per-channel
        # backend relay, not the account's overall connection count, is
        # what can't be shared -- so same-channel candidates must queue
        # behind each other even when the lane itself has room.
        import time as time_mod
        import threading as threading_mod
        from probarr.probequeue import ProbeQueue

        release = threading_mod.Event()
        running_together = []
        active = set()
        lock = threading_mod.Lock()
        def runner(payload):
            with lock:
                active.add(payload["rec_key"])
                running_together.append(set(active))
            if payload["rec_key"].startswith("BBCONE|"):
                release.wait(timeout=2)
            with lock:
                active.discard(payload["rec_key"])
            return {"status": "ok"}

        # Plenty of lane capacity (4) and no viewers -- if this were purely
        # about lane capacity, both BBCONE candidates would run at once.
        q = ProbeQueue(runner, concurrency=lambda: 4, gap=lambda: 0,
                       lane_limit=lambda lane: 4, viewer_count=lambda lane: 0)
        q.submit("k1", {"lane": "L", "rec_key": "BBCONE|streamA"})
        q.submit("k2", {"lane": "L", "rec_key": "BBCONE|streamB"})
        q.submit("k3", {"lane": "L", "rec_key": "BBCTWO|streamC"})
        for _ in range(50):
            if len(running_together) >= 2:
                break
            time_mod.sleep(0.02)
        # BBCTWO (a different channel) must be able to run alongside the
        # first BBCONE candidate -- confirms this isn't just serialising
        # everything.
        self.assertTrue(any(len(s) >= 2 for s in running_together),
                        "a different channel never ran alongside the first")
        # But no snapshot may ever show BOTH BBCONE candidates active at once.
        both_bbcone = {"BBCONE|streamA", "BBCONE|streamB"}
        self.assertFalse(any(both_bbcone.issubset(s) for s in running_together),
                         "two candidates of the same channel ran simultaneously")
        release.set()


class TestVerifyStop(Temp):
    def test_should_stop_actually_cuts_a_concurrent_run_short(self):
        # Real bug: with concurrency>1, ThreadPoolExecutor.submit() only
        # queues work and returns immediately, so the submit loop raced
        # through the ENTIRE worklist (hundreds of items) before a
        # should_stop() flip from an HTTP request could ever land -- and
        # the as_completed() loop that followed had no should_stop check at
        # all, so it unconditionally waited for every queued item to
        # finish. Stop verifying was a complete no-op on any concurrency>1
        # run until it had probed everything anyway. Reproduced here with a
        # slow, mocked probe() and a should_stop that flips after the first
        # completion -- far fewer than all 40 candidates must run.
        import time as time_mod
        from probarr import verify as verify_mod
        from probarr.sources.base import Stream
        from probarr.probe import ProbeOptions
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.write_wantlist_raw(
            [{"number": i, "name": f"C{i}", "key": f"C{i}"} for i in range(40)], [])
        pools = {f"C{i}": [Stream(id=f"s{i}", name=f"C{i}", url=f"http://x/{i}")]
                for i in range(40)}

        call_count = [0]
        def fake_probe(stream, opts, thumb_path, frame_path, crop_path):
            call_count[0] += 1
            time_mod.sleep(0.05)
            return {"status": "ok"}

        stop_after_first = [False]
        def should_stop():
            return stop_after_first[0]

        with unittest.mock.patch("probarr.verify.probe", fake_probe):
            def progress_cb(*a, **k):
                if call_count[0] >= 1:
                    stop_after_first[0] = True
            verify_mod.verify(pools, store, ProbeOptions(), concurrency=4,
                              gap_seconds=0, should_stop=should_stop,
                              progress_cb=progress_cb)

        # With 4 workers and a stop flipped after the very first completion,
        # nowhere near all 40 candidates should have been probed -- the old
        # code would have run every single one regardless.
        self.assertLess(call_count[0], 40)
        meta = store.read_meta()
        self.assertTrue(meta.get("interrupted"))


class TestReferenceLineups(Temp):
    def _fake_response(self, payload):
        body = json.dumps(payload).encode()
        cm = unittest.mock.MagicMock()
        cm.__enter__.return_value.read.return_value = body
        return cm

    def test_discovers_and_caches_the_repo_listing(self):
        listing = [{"name": "UK_SkyTV_lineup.json"}, {"name": "plugin.json"}]
        with unittest.mock.patch("probarr.wantlist.urllib.request.urlopen",
                                  return_value=self._fake_response(listing)) as m:
            items = wl.known_reference_lineups(self.root)
            self.assertEqual(len(items), 1)   # plugin.json excluded
            self.assertEqual(items[0]["region"], "United Kingdom")
            m.assert_called_once()
            # Second call must hit the on-disk cache, not fetch again.
            wl.known_reference_lineups(self.root)
            m.assert_called_once()

    def test_refresh_forces_a_new_fetch(self):
        with unittest.mock.patch("probarr.wantlist.urllib.request.urlopen",
                                  return_value=self._fake_response([])) as m:
            wl.known_reference_lineups(self.root)
            wl.known_reference_lineups(self.root, force=True)
            self.assertEqual(m.call_count, 2)


class TestRank(unittest.TestCase):
    def test_a_bigger_picture_beats_a_cleaner_log(self):
        # Changed deliberately from the opposite rule. Streams logging decode
        # errors play fine -- the decoder conceals them -- so preferring a
        # 1.1 Mbps 576p stream over a 5 Mbps 1080p one because the first
        # logged nothing produced a visibly worse picture in real viewing.
        dirty_but_big = {"status": "dirty", "width": 1920, "height": 1080,
                         "fps": 50, "measured_kbps": 5000,
                         "corruption_errors": 67, "corruption_per_sec": 6.7}
        clean_but_small = {"status": "ok", "width": 720, "height": 576,
                           "fps": 50, "measured_kbps": 1144,
                           "corruption_errors": 0, "corruption_per_sec": 0}
        self.assertIs(rank([clean_but_small, dirty_but_big])[0], dirty_but_big)

    def test_bitrate_beats_frame_rate_at_the_same_size(self):
        smooth = {"status": "ok", "width": 1920, "height": 1080, "fps": 50,
                  "measured_kbps": 1588}
        detailed = {"status": "ok", "width": 1920, "height": 1080, "fps": 25,
                    "measured_kbps": 5792}
        self.assertIs(rank([smooth, detailed])[0], detailed)

    def test_frame_rate_still_decides_between_equals(self):
        slow = {"status": "ok", "width": 1920, "height": 1080, "fps": 25,
                "measured_kbps": 5000}
        fast = {"status": "ok", "width": 1920, "height": 1080, "fps": 50,
                "measured_kbps": 5000}
        self.assertIs(rank([slow, fast])[0], fast)

    def test_errors_break_a_tie_between_equals(self):
        a = {"status": "dirty", "width": 1920, "height": 1080, "fps": 25,
             "measured_kbps": 4000, "corruption_per_sec": 9.0}
        b = {"status": "ok", "width": 1920, "height": 1080, "fps": 25,
             "measured_kbps": 4000, "corruption_per_sec": 0.0}
        self.assertIs(rank([a, b])[0], b)

    def test_unplayable_still_ranks_below_everything_that_plays(self):
        for bad in ({"status": "dead"}, {"status": "no_frame"},
                    {"status": "placeholder", "width": 1920, "height": 1080,
                     "fps": 50, "measured_kbps": 9000}):
            plays = {"status": "dirty", "width": 640, "height": 360, "fps": 25,
                     "measured_kbps": 500, "corruption_per_sec": 20}
            self.assertIs(rank([bad, plays])[0], plays, bad["status"])

    def test_dead_streams_rank_last(self):
        dead = {"status": "dead", "width": 0, "height": 0}
        ok = {"status": "ok", "width": 1280, "height": 720, "fps": 25,
              "measured_kbps": 2000, "corruption_errors": 0}
        self.assertIs(rank([dead, ok])[0], ok)


class TestM3UExport(unittest.TestCase, ):
    def test_export_carries_group_logo_and_tvg_id(self):
        # The regression: exports named every channel and matched none of
        # them to a guide, with no icons either.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.m3u")
            m3u.write([(101, "BBC One", "General", "http://logo/1.png",
                        "bbc.one.uk", "http://stream/1")], path)
            with open(path) as f:
                text = f.read()
        self.assertIn('tvg-chno="101"', text)
        self.assertIn('group-title="General"', text)
        self.assertIn('tvg-logo="http://logo/1.png"', text)
        self.assertIn('tvg-id="bbc.one.uk"', text)

    def test_round_trips_through_the_parser(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.m3u")
            m3u.write([(1, "Chan One", "G", "", "", "http://s/1")], path)
            back = m3u.load(path)
        self.assertEqual(back[0].name, "Chan One")
        self.assertEqual(back[0].url, "http://s/1")


class TestExpand(unittest.TestCase):
    """_expand() -- the ordered-streams shape a push actually writes."""

    def test_native_mode_sends_the_whole_ordered_list(self):
        from probarr.dispatcharr_export import _expand
        ch = {"number": 101, "name": "Ch", "primary": {"stream_id": 1},
              "fallback": {"stream_id": 2},
              "streams": [{"stream_id": 1}, {"stream_id": 2}, {"stream_id": 3}]}
        rows = _expand([ch], "native")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], [1, 2, 3])

    def test_native_mode_falls_back_to_primary_fallback_with_no_list(self):
        from probarr.dispatcharr_export import _expand
        ch = {"number": 101, "name": "Ch", "primary": {"stream_id": 1},
              "fallback": {"stream_id": 2}}
        rows = _expand([ch], "native")
        self.assertEqual(rows[0][3], [1, 2])

    def test_separate_mode_ignores_a_third_stream(self):
        from probarr.dispatcharr_export import _expand
        ch = {"number": 101, "name": "Ch", "primary": {"stream_id": 1},
              "fallback": {"stream_id": 2},
              "streams": [{"stream_id": 1}, {"stream_id": 2}, {"stream_id": 3}]}
        rows = _expand([ch], "separate")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][3], [1])
        self.assertEqual(rows[1][3], [2])
        self.assertEqual(rows[1][2], "FALLBACK: Ch")


class TestStore(Temp):
    def _store(self):
        s = RunStore(self.root, "run1")
        s.write_wantlist_raw([{"number": 1, "name": "One", "key": "ONE"}], [])
        return s

    def test_newest_record_for_a_probe_wins(self):
        s = self._store()
        s.append({"rec_key": "ONE|a", "channel_key": "ONE", "status": "dead"})
        s.append({"rec_key": "ONE|a", "channel_key": "ONE", "status": "ok"})
        self.assertEqual([r["status"] for r in s.load()], ["ok"])
        self.assertEqual(len(s.load(dedupe=False)), 2)

    def test_drop_channel_clears_wantlist_and_results(self):
        s = self._store()
        s.append({"rec_key": "ONE|a", "channel_key": "ONE", "status": "ok"})
        self.assertEqual(s.drop_channel("ONE"), 1)
        self.assertEqual(s.load(), [])
        self.assertEqual(s.read_wantlist()["wanted"], [])

    def test_removals_are_staged_and_cleared(self):
        s = self._store()
        s.add_removal("ONE", 101, "One")
        self.assertEqual(s.read_removals()[0]["number"], 101)
        s.add_removal("ONE", 101, "One")          # idempotent, not duplicated
        self.assertEqual(len(s.read_removals()), 1)
        s.clear_removal("ONE")
        self.assertEqual(s.read_removals(), [])


class TestEpgList(Temp):
    def _guide(self, channels):
        xml = os.path.join(self.root, "guide.xml")
        body = "".join(f'<channel id="{cid}"><display-name>{name}</display-name></channel>'
                       for cid, name in channels)
        with open(xml, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0"?><tv>{body}</tv>')
        from probarr import epgsources
        epgsources.save(self.root, "test-guide", "file://" + xml)

    def test_collapses_sd_hd_pairs_to_one_row(self):
        from probarr import epgcheck
        self._guide([("1", "BBC One"), ("2", "BBC One HD")])
        out = epgcheck.list_channels(self.root, "test-guide", Normalizer())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["guide_name"], "BBC One")

    def test_groups_real_regional_variants_under_one_row_with_alts(self):
        from probarr import epgcheck
        self._guide([("1", "BBC One London"), ("2", "BBC One North West"),
                     ("3", "BBC One Scotland")])
        out = epgcheck.list_channels(self.root, "test-guide", Normalizer())
        # Nothing is discarded -- one row for the whole family, every
        # region (including the representative itself) still reachable:
        # the representative plus two alts covers all three variants.
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertIn("alts", row)
        self.assertEqual(len(row["alts"]) + 1, 3)

    def test_strips_a_glued_country_code_from_the_display_name(self):
        # Real data seen from open-epg.com's UK feed: <display-name> is
        # literally "4Seven.uk", not a real display name -- shown verbatim
        # and written into a wantlist as-is otherwise ("4Seven.uk | 4Seven.uk").
        from probarr import epgcheck
        self._guide([("1", "4Seven.uk"), ("2", "5Star.uk")])
        out = epgcheck.list_channels(self.root, "test-guide", Normalizer())
        names = sorted(c["guide_name"] for c in out)
        self.assertEqual(names, ["4Seven", "5Star"])

    def test_display_clean_leaves_ordinary_names_alone(self):
        from probarr import epgcheck
        self.assertEqual(epgcheck._display_clean("BBC One"), "BBC One")
        self.assertEqual(epgcheck._display_clean("Sky Sports F1"), "Sky Sports F1")

    def test_ordinary_names_are_not_mistaken_for_a_glued_region_suffix(self):
        # Regression: "Sky One" was being stripped to "Sky O" because "NE"
        # (North East) matched its own trailing two letters with no
        # boundary check. Glued region+quality suffixes ("EastHD") DO need
        # to strip with no space, so the fix can't just require a plain
        # word boundary -- it has to tell "glued-on tag" apart from
        # "coincidentally ends in the same letters".
        from probarr import epgcheck
        self.assertEqual(epgcheck._strip_region("Sky One"), "Sky One")
        self.assertEqual(epgcheck._strip_region("BBC One EastHD"), "BBC One")

    def test_unrelated_channels_are_never_grouped_together(self):
        from probarr import epgcheck
        self._guide([("1", "BBC One London"), ("2", "ITV1 London")])
        out = epgcheck.list_channels(self.root, "test-guide", Normalizer())
        self.assertEqual(len(out), 2)
        for row in out:
            self.assertNotIn("alts", row)


class TestBackup(Temp):
    def test_round_trips_config_and_run_state(self):
        from probarr import backup as backup_mod
        providers.save(self.root, "myprov", "http://example.com/list.m3u")
        s = RunStore(self.root, "run1")
        s.write_meta({"run_id": "run1"})  # list_runs() only sees a run via run.json
        s.write_wantlist_raw([{"number": 1, "name": "One", "key": "ONE"}], [])
        s.append({"rec_key": "ONE|a", "channel_key": "ONE", "status": "ok"})
        s.write_selection({"ONE": {"group": "Entertainment"}})

        data = backup_mod.export_tar(self.root)

        fresh = tempfile.mkdtemp(prefix="probarr-test-restore-")
        self.addCleanup(shutil.rmtree, fresh, ignore_errors=True)
        backup_mod.import_tar(fresh, data)

        self.assertEqual(providers.list_all(fresh)[0]["name"], "myprov")
        restored = RunStore(fresh, "run1")
        self.assertEqual(restored.load()[0]["status"], "ok")
        self.assertEqual(restored.read_selection()["ONE"]["group"], "Entertainment")

    def test_refuses_a_path_traversal_member(self):
        import io
        import tarfile
        from probarr import backup as backup_mod
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="../../etc/passwd")
            payload = b"pwned"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(ValueError):
            backup_mod.import_tar(self.root, buf.getvalue())
        # And nothing was written outside root as a side effect of the attempt.
        self.assertFalse(os.path.exists(os.path.join(self.root, "..", "..", "etc", "passwd")))


class TestAliases(Temp):
    def test_folds_both_sides_so_the_lookup_matches(self):
        aliases_mod.save(self.root, "U&Drama", "drama")
        self.assertEqual(aliases_mod.read(self.root), {"UANDDRAMA": "DRAMA"})

    def test_refuses_an_alias_to_itself(self):
        with self.assertRaises(ValueError):
            aliases_mod.save(self.root, "Drama", "DRAMA")

    def test_delete(self):
        aliases_mod.save(self.root, "U&Drama", "Drama")
        self.assertTrue(aliases_mod.delete(self.root, "u & drama"))
        self.assertEqual(aliases_mod.read(self.root), {})


class TestLineups(Temp):
    def test_partial_update_does_not_blank_other_fields(self):
        lineups.save(self.root, "demo", provider="mybunny", wantlist="uk-demo")
        lineups.save(self.root, "demo", epg="http://guide")
        lu = lineups.get(self.root, "demo")
        self.assertEqual(lu["provider"], "mybunny")
        self.assertEqual(lu["epg"], "http://guide")

    def test_preferences_survive_and_clear(self):
        lineups.save(self.root, "demo")
        lineups.set_preference(self.root, "demo", "BBCONE", group="General",
                               name="BBC One HD")
        self.assertEqual(lineups.preferences(self.root, "demo")["BBCONE"],
                         {"group": "General", "name": "BBC One HD"})
        lineups.set_preference(self.root, "demo", "BBCONE", group=None, name=None)
        self.assertNotIn("BBCONE", lineups.preferences(self.root, "demo"))


class TestCredentials(Temp):
    def test_redaction_hides_every_secret_form(self):
        for spec, secret in [
                ("https://p.tv/get.php?u=bob&p=hunter2", "hunter2"),  # probarr:allow-secret
                ("dispatcharr://admin:s3cret@10.0.0.1:9191", "s3cret"),  # probarr:allow-secret
                ("xtream://user:pw123@panel.tv", "pw123")]:  # probarr:allow-secret
            self.assertNotIn(secret, providers.redact(spec))


class TestPageTemplates(unittest.TestCase):
    """The bug class that broke the Curate page twice: JavaScript written
    inside a Python string, with escapes the interpreter silently ate."""

    def _pages(self):
        from probarr import web as web_mod
        return {"curate": curate.HTML, "runs_index": web_mod.INDEX,
                **{n: getattr(pages, n) for n in
                   ("WANTLIST_PAGE", "SETTINGS_PAGE", "PROVIDERS_PAGE",
                    "NEWRUN_PAGE", "BROWSE_PAGE", "LINEUPS_PAGE")}}

    def test_templates_are_raw_strings(self):
        # web.py's INDEX (the runs list) was missed here for a long time --
        # not raw, and it shipped a genuinely broken confirm() dialog as a
        # result (a single backslash before an embedded quote got eaten by
        # Python instead of reaching the browser, throwing a JS SyntaxError
        # that silently killed the whole script tag -- including the
        # unrelated Delete button's listener in the same block). Scanning
        # web.py here too is what would have caught it before it shipped.
        for path in ("probarr/curate.py", "probarr/pages.py", "probarr/web.py"):
            with open(os.path.join(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))), path)) as f:
                src = f.read()
            for m in re.finditer(r'^([A-Z_]+) = (r?)"""<!doctype', src, re.M):
                self.assertEqual(m.group(2), "r",
                                 f"{m.group(1)} must be a raw string: Python "
                                 "eats the JavaScript's escapes otherwise")

    def test_no_double_escaped_unicode_reaches_the_browser(self):
        # r"\\u2014" would render as a literal backslash-u in the page.
        for name, html in self._pages().items():
            self.assertNotIn(r"\\u", html, f"{name} has a doubled escape")

    def test_every_placeholder_is_substituted_when_rendered(self):
        rendered = [pages.wantlist_page(), pages.settings_page(),
                    pages.providers_page(), pages.new_run_page(),
                    pages.browse_page(), pages.lineups_page()]
        for html in rendered:
            leftover = re.findall(r"__[A-Z]+__", html)
            self.assertEqual(leftover, [], f"unsubstituted: {leftover}")

    def test_scripts_are_balanced(self):
        for name, html in self._pages().items():
            self.assertEqual(html.count("<script>"), html.count("</script>"),
                             f"{name} has an unbalanced script tag")


if __name__ == "__main__":
    unittest.main()
