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


class TestRateLimitGuard(Temp):
    """New: probe.py flags HTTP 429/403 in ffmpeg stderr distinctly from a
    genuinely dead/corrupted stream, and verify.py's RateLimitGuard pauses
    ALL probing (not just the one channel) when the provider is actively
    refusing connections -- ported from PiratesIRC's IPTVChecker, which had
    this and probarr previously did not.
    """

    def test_probe_detects_429_in_stderr_and_labels_it_distinctly(self):
        from probarr import probe as probe_mod

        self.assertTrue(probe_mod._RATE_LIMIT_RE.search(
            "Server returned 429 Too Many Requests"))
        self.assertTrue(probe_mod._RATE_LIMIT_RE.search(
            "HTTP error 403 Forbidden"))
        self.assertFalse(probe_mod._RATE_LIMIT_RE.search(
            "Connection timed out"))
        self.assertFalse(probe_mod._RATE_LIMIT_RE.search(
            "concealing 12 DC coefficients"))

    def test_full_probe_marks_no_frame_as_rate_limited_when_stderr_says_429(self):
        # Real, evidenced shape from live investigation: metadata succeeds
        # (ffprobe sees a video stream) but the capture pass gets nothing --
        # here because the provider actively refused it, not because the
        # stream is dead. The reason string must say so, distinctly from
        # the generic "no frame could be decoded".
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        opts = probe_mod.ProbeOptions(retry_empty=False)
        stream = Stream(id="s1", name="Comedy Central", url="http://x/429")

        fake_meta = {"has_video": True, "width": 1920, "height": 1080,
                    "fps": 50.0, "video_codec": "h264", "video_profile": "",
                    "pix_fmt": "yuv420p", "audio_codec": "aac",
                    "audio_channels": 2, "video_variant_count": 1,
                    "declared_kbps": 0, "container": "mpegts"}
        fake_cap = {"decode_errors": 0, "corruption_errors": 0,
                   "corruption_startup": 0, "corruption_steady": 0,
                   "corruption_per_sec": 0.0, "decoded_seconds": 0.0,
                   "error_samples": ["Server returned 403 Forbidden"],
                   "capture_seconds": 0.1, "timed_out": False,
                   "rate_limited": True, "dhash": None, "motion": None,
                   "motion_frames": 0, "low_motion": False, "frame32": None,
                   "low_contrast": False, "measured_kbps": 0,
                   "sample_duration": 0.0, "thumb": None, "frame": None,
                   "crop": None, "clip": None}

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=fake_meta), \
             unittest.mock.patch.object(probe_mod, "capture", return_value=fake_cap):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        self.assertEqual(result["status"], probe_mod.STATUS_NO_FRAME)
        self.assertTrue(result["rate_limited"])
        self.assertIn("429/403", result["reason"])
        self.assertNotIn("no frame could be decoded", result["reason"])

    def test_guard_trips_after_threshold_hits_and_pauses_the_caller(self):
        import time as time_mod
        from probarr.verify import RateLimitGuard

        guard = RateLimitGuard()
        guard.BASE_COOLDOWN_SECONDS = 0.3   # keep the test fast
        guard.TRIP_THRESHOLD = 3

        logged = []
        for _ in range(3):
            guard.record_hit(log=logged.append)

        self.assertEqual(guard.trips, 1)
        self.assertTrue(any("tripped" in m for m in logged))

        start = time_mod.time()
        guard.wait()
        elapsed = time_mod.time() - start
        # Must genuinely have paused for close to the cooldown, not returned
        # immediately.
        self.assertGreaterEqual(elapsed, 0.3 * 0.7)

    def test_guard_does_not_trip_below_threshold(self):
        from probarr.verify import RateLimitGuard
        guard = RateLimitGuard()
        guard.record_hit()
        guard.record_hit()
        self.assertEqual(guard.trips, 0)
        # No cooldown active -- must return immediately.
        import time as time_mod
        start = time_mod.time()
        guard.wait()
        self.assertLess(time_mod.time() - start, 0.05)

    def test_verify_run_pauses_all_probing_when_provider_starts_refusing(self):
        # End-to-end through verify(): three channels' first candidates all
        # come back 429-refused in quick succession (below the real
        # per-channel/per-lane fixes' reach, since this is a DIFFERENT
        # channel each time) -- the guard must still trip and pause the
        # 4th probe, proving the pause is account-wide, not per-channel.
        import time as time_mod
        import unittest.mock
        from probarr import verify as verify_mod
        from probarr.probe import ProbeOptions, STATUS_NO_FRAME
        from probarr.sources.base import Stream
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.write_wantlist_raw(
            [{"number": i, "name": f"C{i}", "key": f"C{i}"} for i in range(4)], [])
        pools = {f"C{i}": [Stream(id=f"s{i}", name=f"C{i}", url=f"http://x/{i}")]
                for i in range(4)}

        call_times = []

        def fake_probe(stream, opts, thumb_path, frame_path=None, crop_path=None):
            call_times.append(time_mod.time())
            if len(call_times) <= 3:
                return {"status": STATUS_NO_FRAME, "rate_limited": True,
                       "reason": "provider refused the connection (429/403)"}
            return {"status": "ok", "rate_limited": False}

        with unittest.mock.patch("probarr.verify.probe", fake_probe), \
             unittest.mock.patch.object(verify_mod.RateLimitGuard,
                                        "BASE_COOLDOWN_SECONDS", 0.4), \
             unittest.mock.patch.object(verify_mod.RateLimitGuard,
                                        "TRIP_THRESHOLD", 3):
            verify_mod.verify(pools, store, ProbeOptions(), concurrency=1,
                              gap_seconds=0)

        self.assertEqual(len(call_times), 4)
        # The 4th call (past the trip threshold) must land noticeably later
        # than the first three, which fired back-to-back.
        gap_before_trip = call_times[2] - call_times[0]
        gap_after_trip = call_times[3] - call_times[2]
        self.assertGreater(gap_after_trip, gap_before_trip)
        self.assertGreaterEqual(gap_after_trip, 0.4 * 0.7)

    def test_concurrent_verify_never_runs_two_candidates_of_the_same_channel(self):
        # verify.py's concurrency>1 branch had NO per-channel single-flight
        # equivalent to probequeue.py's -- this is the gap that made the
        # earlier probequeue-only fix invisible to a real "Verify" run using
        # several provider slots. Two candidates of ONE channel plus one
        # candidate of a different channel, concurrency=3: the different
        # channel may overlap either same-channel candidate, but the two
        # same-channel candidates must never overlap each other.
        import threading
        import time as time_mod
        import unittest.mock
        from probarr import verify as verify_mod
        from probarr.probe import ProbeOptions
        from probarr.sources.base import Stream
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.write_wantlist_raw(
            [{"number": 1, "name": "SAME", "key": "SAME"},
             {"number": 2, "name": "OTHER", "key": "OTHER"}], [])
        pools = {
            "SAME": [Stream(id="a", name="SAME-a", url="http://x/a"),
                    Stream(id="b", name="SAME-b", url="http://x/b")],
            "OTHER": [Stream(id="c", name="OTHER-c", url="http://x/c")],
        }

        active = set()
        overlap_detected = [False]
        lock = threading.Lock()

        def fake_probe(stream, opts, thumb_path, frame_path=None, crop_path=None):
            with lock:
                if stream.id in ("a", "b") and any(s in ("a", "b") for s in active):
                    overlap_detected[0] = True
                active.add(stream.id)
            time_mod.sleep(0.1)
            with lock:
                active.discard(stream.id)
            return {"status": "ok"}

        with unittest.mock.patch("probarr.verify.probe", fake_probe):
            verify_mod.verify(pools, store, ProbeOptions(), concurrency=3,
                              gap_seconds=0, clean_target=None)

        self.assertFalse(overlap_detected[0],
                         "two candidates of the same channel ran simultaneously")


class TestProviderDeclined(Temp):
    """The real, measured failure this was built for.

    Signature taken from an actual 733-probe run: all 44 no_frame results
    had decoded_seconds 0.00, measured_kbps 0, decode errors present, and
    timed_out False -- the provider accepted the connection and handed back
    undecodable bytes rather than refusing. They arrived in same-channel
    bursts, and every failing URL probed clean elsewhere in the same run.
    """

    def _cap(self, **over):
        cap = {"decode_errors": 96, "corruption_errors": 40,
              "corruption_startup": 40, "corruption_steady": 0,
              "corruption_per_sec": 0.0, "decoded_seconds": 0.0,
              "error_samples": ["[h264] non-existing PPS 0 referenced"],
              "capture_seconds": 5.2, "timed_out": False,
              "rate_limited": False, "dhash": None, "motion": None,
              "motion_frames": 0, "low_motion": False, "frame32": None,
              "low_contrast": False, "measured_kbps": 0,
              "sample_duration": 0.0, "thumb": None, "frame": None,
              "crop": None, "clip": None}
        cap.update(over)
        return cap

    def test_recognises_the_measured_signature(self):
        from probarr.probe import served_nothing
        self.assertTrue(served_nothing(self._cap()))

    def test_does_not_fire_on_ambiguous_lookalikes(self):
        from probarr.probe import served_nothing
        # Decoded real video but the thumbnail selection missed -- a
        # different fault, must keep the cheap single retry.
        self.assertFalse(served_nothing(self._cap(decoded_seconds=10.9,
                                                  measured_kbps=2608)))
        # Bytes arrived even though decode reported nothing.
        self.assertFalse(served_nothing(self._cap(measured_kbps=2608)))
        # A clean capture that produced a picture is never this.
        self.assertFalse(served_nothing(self._cap(thumb="/tmp/a.jpg")))
        # No errors at all -- not the garbage-stream shape.
        self.assertFalse(served_nothing(self._cap(decode_errors=0)))

    def test_backs_off_properly_instead_of_retrying_once_immediately(self):
        # The old behaviour was ONE retry after 1.5s, which every one of the
        # 44 real failures had already used and still failed, because the
        # same-channel burst causing it was still in flight. The retry must
        # now escalate and span long enough for that burst to drain.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        opts = probe_mod.ProbeOptions(empty_backoff=(0.05, 0.1, 0.2))
        stream = Stream(id="s1", name="Comedy Central", url="http://x/cc")
        meta = {"has_video": True, "width": 1920, "height": 1080, "fps": 50.0,
               "video_codec": "h264", "video_profile": "", "pix_fmt": "yuv420p",
               "audio_codec": "aac", "audio_channels": 2,
               "video_variant_count": 1, "declared_kbps": 0,
               "container": "mpegts"}

        sleeps = []
        calls = [0]

        def fake_capture(*a, **k):
            calls[0] += 1
            return self._cap()

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=meta), \
             unittest.mock.patch.object(probe_mod, "capture", side_effect=fake_capture), \
             unittest.mock.patch.object(probe_mod.time, "sleep", sleeps.append):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        self.assertEqual(calls[0], 4)                 # initial + 3 retries
        self.assertEqual(sleeps, [0.05, 0.1, 0.2])    # escalating, not 1.5 once
        self.assertEqual(result["status"], probe_mod.STATUS_NO_FRAME)
        self.assertTrue(result["provider_declined"])
        self.assertIn("declining to serve", result["reason"])

    def test_stops_retrying_as_soon_as_a_frame_arrives(self):
        # The whole premise is that these clear on their own -- so a retry
        # that succeeds must not keep burning provider connections, and the
        # result must be a normal verdict, not no_frame.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        opts = probe_mod.ProbeOptions(empty_backoff=(0.05, 0.1, 0.2))
        stream = Stream(id="s1", name="Comedy Central", url="http://x/cc")
        meta = {"has_video": True, "width": 1920, "height": 1080, "fps": 50.0,
               "video_codec": "h264", "video_profile": "", "pix_fmt": "yuv420p",
               "audio_codec": "aac", "audio_channels": 2,
               "video_variant_count": 1, "declared_kbps": 0,
               "container": "mpegts"}
        calls = [0]

        def fake_capture(*a, **k):
            calls[0] += 1
            if calls[0] < 3:
                return self._cap()
            return self._cap(thumb="/tmp/t.jpg", decoded_seconds=10.9,
                            measured_kbps=2608, decode_errors=12,
                            corruption_startup=12)

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=meta), \
             unittest.mock.patch.object(probe_mod, "capture", side_effect=fake_capture), \
             unittest.mock.patch.object(probe_mod.time, "sleep", lambda s: None):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        self.assertEqual(calls[0], 3)                 # stopped on success
        self.assertEqual(result["status"], probe_mod.STATUS_OK)
        self.assertEqual(result["attempts"], 3)

    def test_an_ordinary_empty_capture_keeps_the_cheap_single_retry(self):
        # Not every empty capture is a provider refusal -- one that decoded
        # real video but produced no picture must not pay the long backoff.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        opts = probe_mod.ProbeOptions(empty_backoff=(3.0, 8.0, 20.0))
        stream = Stream(id="s1", name="X", url="http://x/1")
        meta = {"has_video": True, "width": 1920, "height": 1080, "fps": 50.0,
               "video_codec": "h264", "video_profile": "", "pix_fmt": "yuv420p",
               "audio_codec": "aac", "audio_channels": 2,
               "video_variant_count": 1, "declared_kbps": 0,
               "container": "mpegts"}
        sleeps = []

        def fake_capture(*a, **k):
            return self._cap(decoded_seconds=10.9, measured_kbps=2608)

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=meta), \
             unittest.mock.patch.object(probe_mod, "capture", side_effect=fake_capture), \
             unittest.mock.patch.object(probe_mod.time, "sleep", sleeps.append):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        self.assertEqual(sleeps, [1.5])
        self.assertNotIn("provider_declined", result)


class TestClipNeverCostsThePicture(Temp):
    """The real cause of a channel that played fine everywhere but failed
    every Diagnose and every re-probe.

    ffmpeg writes all of capture()'s outputs in ONE process. The clip is an
    optional extra, but an output it cannot even open aborts the entire
    command -- so an MP4 muxer rejecting the source's audio took the
    thumbnail, frame, crop and bitrate down with it, and the probe reported
    "no frame could be decoded" for a perfectly healthy stream. Verify runs
    were unaffected precisely because they capture no clip.
    """

    def test_clip_audio_is_transcoded_not_copied(self):
        # Fragmented MP4 writes its header up front, and the muxer cannot
        # describe an E-AC-3 track before parsing its packets -- so copying
        # eac3 in here fails with "Cannot write moov atom before EAC3
        # packets parsed" and kills every other output. Video must still be
        # a copy; that is where the cost would be.
        import unittest.mock
        from probarr import probe as probe_mod

        seen = []

        def fake_run(cmd, timeout):
            seen.append(cmd)
            raise RuntimeError("stop here")

        opts = probe_mod.ProbeOptions(capture_clip=True)
        with unittest.mock.patch.object(probe_mod, "_run", fake_run):
            try:
                probe_mod.capture("http://x/1", opts, "/tmp/t.jpg",
                                  "/tmp/f.jpg", "/tmp/c.jpg", "/tmp/clip.mp4")
            except RuntimeError:
                pass

        cmd = seen[0]
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        # The bare "-c copy" that used to cover BOTH tracks is what broke it.
        for i, tok in enumerate(cmd):
            if tok == "-c" and i + 1 < len(cmd) and cmd[i + 1] == "copy":
                # Only legitimate remaining use is the video-only bitrate remux.
                self.assertIn("mpegts", cmd[i:i + 6])

    def test_a_failed_clip_is_retried_without_the_clip(self):
        # General protection, independent of any one codec: if asking for a
        # clip produced no picture, try again without it before concluding
        # anything about the stream.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        meta = {"has_video": True, "width": 1920, "height": 1080, "fps": 50.0,
               "video_codec": "hevc", "video_profile": "Main",
               "pix_fmt": "yuv420p", "audio_codec": "eac3", "audio_channels": 2,
               "video_variant_count": 1, "declared_kbps": 0,
               "container": "mpegts"}

        def blank(**over):
            cap = {"decode_errors": 3, "corruption_errors": 0,
                  "corruption_startup": 0, "corruption_steady": 0,
                  "corruption_per_sec": 0.0, "decoded_seconds": 0.0,
                  "error_samples": ["Could not write header"],
                  "capture_seconds": 2.0, "timed_out": False,
                  "rate_limited": False, "dhash": None, "motion": None,
                  "motion_frames": 0, "low_motion": False, "frame32": None,
                  "low_contrast": False, "measured_kbps": 0,
                  "sample_duration": 0.0, "thumb": None, "frame": None,
                  "crop": None, "clip": None}
            cap.update(over)
            return cap

        calls = []

        def fake_capture(url, opts, thumb, frame=None, crop=None, clip=None):
            calls.append(clip)
            if clip is not None:
                return blank()            # the clip output kills the command
            return blank(thumb="/tmp/t.jpg", decoded_seconds=10.0,
                        measured_kbps=2402, decode_errors=0)

        opts = probe_mod.ProbeOptions(capture_clip=True)
        stream = Stream(id="s1", name="HEVC FHD Comedy Central", url="http://x/1")
        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=meta), \
             unittest.mock.patch.object(probe_mod, "capture", side_effect=fake_capture):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg", "/tmp/f.jpg",
                                     "/tmp/c.jpg", "/tmp/clip.mp4")

        self.assertEqual(calls, ["/tmp/clip.mp4", None])   # asked again, no clip
        self.assertEqual(result["status"], probe_mod.STATUS_OK)
        self.assertTrue(result.get("clip_skipped"))

    def test_the_clipless_retry_is_not_a_backoff_attempt(self):
        # A deterministic muxer failure fails identically every time, so
        # retrying the same broken command on a 3s/8s/20s backoff is pure
        # waste -- which is exactly what was observed in production (four
        # attempts over ~31s, all failing the same way). The clipless retry
        # must happen FIRST and immediately, with no sleep.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        meta = {"has_video": True, "width": 1920, "height": 1080, "fps": 50.0,
               "video_codec": "hevc", "video_profile": "", "pix_fmt": "yuv420p",
               "audio_codec": "eac3", "audio_channels": 2,
               "video_variant_count": 1, "declared_kbps": 0,
               "container": "mpegts"}
        good = {"decode_errors": 0, "corruption_errors": 0,
               "corruption_startup": 0, "corruption_steady": 0,
               "corruption_per_sec": 0.0, "decoded_seconds": 10.0,
               "error_samples": [], "capture_seconds": 10.0, "timed_out": False,
               "rate_limited": False, "dhash": None, "motion": 5.0,
               "motion_frames": 20, "low_motion": False, "frame32": None,
               "low_contrast": False, "measured_kbps": 2402,
               "sample_duration": 10.0, "thumb": "/tmp/t.jpg", "frame": None,
               "crop": None, "clip": None}
        bad = {**good, "thumb": None, "decoded_seconds": 0.0,
              "measured_kbps": 0, "decode_errors": 3}

        sleeps = []

        def fake_capture(url, opts, thumb, frame=None, crop=None, clip=None):
            return dict(bad) if clip is not None else dict(good)

        opts = probe_mod.ProbeOptions(capture_clip=True)
        stream = Stream(id="s1", name="X", url="http://x/1")
        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=meta), \
             unittest.mock.patch.object(probe_mod, "capture", side_effect=fake_capture), \
             unittest.mock.patch.object(probe_mod.time, "sleep", sleeps.append):
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg", "/tmp/f.jpg",
                                     "/tmp/c.jpg", "/tmp/clip.mp4")

        self.assertEqual(sleeps, [], "must not back off before dropping the clip")
        self.assertEqual(result["status"], probe_mod.STATUS_OK)


class TestFiniteFilePlaceholder(unittest.TestCase):
    """A live channel's container should never report a finite duration --
    real IPTV sources overwhelmingly omit the field or return "N/A". A
    stream that answers with a real number is looping a finite file, most
    often the provider's own "channel unavailable" card. Complementary to
    annotate_placeholders()'s cross-channel still-picture matching (which
    needs the SAME frame on more than one channel): this needs no
    corroboration at all, so it catches a lone channel looping a file with
    no twin elsewhere in the lineup.
    """

    def test_parses_a_genuine_finite_duration(self):
        from probarr.probe import _parse_container_duration
        self.assertEqual(_parse_container_duration({"duration": "42.5"}), 42.5)

    def test_a_real_live_stream_reports_none_of_the_forms_ffprobe_uses(self):
        from probarr.probe import _parse_container_duration
        for fmt in ({}, {"duration": None}, {"duration": ""},
                   {"duration": "N/A"}, {"duration": "0"}, {"duration": "-1"},
                   {"duration": "not a number"}):
            self.assertIsNone(_parse_container_duration(fmt), fmt)

    def test_probe_flags_a_finite_duration_before_ever_capturing(self):
        # The whole point of catching this in probe_metadata() rather than
        # capture(): it must never spend the second, expensive decode
        # connection on a candidate already proven to be a finite loop.
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        fake_meta = {"has_video": True, "width": 1920, "height": 1080,
                    "fps": 50.0, "video_codec": "h264", "video_profile": "",
                    "pix_fmt": "yuv420p", "audio_codec": "aac",
                    "audio_channels": 2, "video_variant_count": 1,
                    "declared_kbps": 0, "container": "mpegts",
                    "container_duration": 12.3}
        opts = probe_mod.ProbeOptions()
        stream = Stream(id="s1", name="Holding Card", url="http://x/1")

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=fake_meta), \
             unittest.mock.patch.object(probe_mod, "capture") as fake_capture:
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        fake_capture.assert_not_called()
        self.assertEqual(result["status"], probe_mod.STATUS_PLACEHOLDER)
        self.assertIn("12.3s", result["reason"])

    def test_probe_does_not_flag_a_stream_with_no_declared_duration(self):
        import unittest.mock
        from probarr import probe as probe_mod
        from probarr.sources.base import Stream

        fake_meta = {"has_video": True, "width": 1920, "height": 1080,
                    "fps": 50.0, "video_codec": "h264", "video_profile": "",
                    "pix_fmt": "yuv420p", "audio_codec": "aac",
                    "audio_channels": 2, "video_variant_count": 1,
                    "declared_kbps": 0, "container": "mpegts",
                    "container_duration": None}
        fake_cap = {"thumb": "/tmp/t.jpg", "decode_errors": 0,
                   "corruption_errors": 0, "corruption_startup": 0,
                   "corruption_steady": 0, "corruption_per_sec": 0.0,
                   "decoded_seconds": 10.0, "error_samples": [],
                   "capture_seconds": 10.0, "timed_out": False,
                   "rate_limited": False, "dhash": None, "motion": None,
                   "motion_frames": 0, "low_motion": False, "frame32": None,
                   "low_contrast": False, "measured_kbps": 1000,
                   "sample_duration": 10.0, "frame": None, "crop": None,
                   "clip": None}
        opts = probe_mod.ProbeOptions()
        stream = Stream(id="s1", name="Real Channel", url="http://x/1")

        with unittest.mock.patch.object(probe_mod, "probe_metadata", return_value=fake_meta), \
             unittest.mock.patch.object(probe_mod, "capture", return_value=fake_cap) as fake_capture:
            result = probe_mod.probe(stream, opts, "/tmp/t.jpg")

        fake_capture.assert_called_once()
        self.assertEqual(result["status"], probe_mod.STATUS_OK)


class TestReprobeSampleLength(Temp):
    """A plain single ↻ re-probe used to inherit the full unattended-Verify
    sample length (cfg["sample_seconds"], 8-10s by default) -- a leftover
    from before the standalone Preview button (short, 6s) was merged into
    it. The merge kept the clip capture but never picked up Preview's short
    window. A ↻ click is always attended -- a human is about to look at the
    resulting frame -- and the corruption-rate math a long unattended
    sample exists for isn't what that click needs.
    """

    def test_plain_reprobe_uses_the_short_window_not_the_bulk_default(self):
        import unittest.mock
        from probarr import web as web_mod
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.append({"rec_key": "C1|s1", "channel_key": "C1", "stream_id": "s1",
                      "stream_name": "X", "url": "http://x/1", "url_redacted": "",
                      "group": "", "logo": "", "tvg_id": "", "probed_at": 1})

        seen_opts = []

        def fake_probe(stream, opts, *a, **k):
            seen_opts.append(opts)
            return {"status": "ok"}

        with unittest.mock.patch.object(web_mod, "probe", fake_probe), \
             unittest.mock.patch.object(web_mod.ProbeOptions, "resolved", lambda self: self), \
             unittest.mock.patch.object(web_mod, "settings_mod") as settings_mock:
            settings_mock.read.return_value = {
                "sample_seconds": 10,   # the BULK/Verify default -- must NOT be used
                "frame_height": 720, "thumb_height": 240}
            web_mod.Handler.root = self.root
            result = web_mod.Handler._run_reprobe(
                {"run_id": "run1", "rec_key": "C1|s1"})

        self.assertNotIn("error", result)
        self.assertEqual(len(seen_opts), 1)
        self.assertEqual(seen_opts[0].sample_seconds,
                         web_mod.Handler.REPROBE_SAMPLE_SECONDS)
        self.assertEqual(seen_opts[0].sample_seconds,
                         web_mod.Handler.PREVIEW_SAMPLE_SECONDS)
        self.assertNotEqual(seen_opts[0].sample_seconds, 10,
                            "plain re-probe must not use the bulk-verify sample length")

    def test_diagnose_still_uses_its_own_longer_window(self):
        import unittest.mock
        from probarr import web as web_mod
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.append({"rec_key": "C1|s1", "channel_key": "C1", "stream_id": "s1",
                      "stream_name": "X", "url": "http://x/1", "url_redacted": "",
                      "group": "", "logo": "", "tvg_id": "", "probed_at": 1})

        seen_opts = []

        def fake_probe(stream, opts, *a, **k):
            seen_opts.append(opts)
            return {"status": "ok"}

        with unittest.mock.patch.object(web_mod, "probe", fake_probe), \
             unittest.mock.patch.object(web_mod.ProbeOptions, "resolved", lambda self: self), \
             unittest.mock.patch.object(web_mod, "settings_mod") as settings_mock:
            settings_mock.read.return_value = {
                "sample_seconds": 10, "frame_height": 720, "thumb_height": 240}
            web_mod.Handler.root = self.root
            web_mod.Handler._run_reprobe(
                {"run_id": "run1", "rec_key": "C1|s1", "diagnose": True})

        self.assertEqual(seen_opts[0].sample_seconds,
                         web_mod.Handler.DIAGNOSE_SAMPLE_SECONDS)


class TestStreamUrlMapPrefersNative(unittest.TestCase):
    """The other half of the per-provider-accounts story, found while
    verifying the enforcement fix above against real data: a channel
    pushed BEFORE its provider had a correctly configured Dispatcharr
    account keeps an old custom stream around. Once the account is fixed
    and Dispatcharr's own refresh produces a NATIVE stream with the
    identical URL, get_or_create_custom_stream()'s lookup must always
    prefer that native one -- not whichever happened to paginate last.
    Confirmed live: one real channel's four candidates split 3 custom / 1
    native despite all four existing natively by push time, purely from
    dict-comprehension pagination order.
    """

    def _client(self, streams):
        from probarr.sources.dispatcharr import Dispatcharr
        c = Dispatcharr("http://x", "u", "p")
        c.api = lambda method, path, body=None: None
        c.paged = lambda path, page_size=1000: streams
        return c

    def test_native_wins_when_native_is_paginated_first(self):
        client = self._client([
            {"id": 100, "url": "http://p/1", "is_custom": False},
            {"id": 200, "url": "http://p/1", "is_custom": True},
        ])
        self.assertEqual(client.stream_url_map()["http://p/1"], 100)

    def test_native_wins_when_custom_is_paginated_first(self):
        # The order that actually broke it live -- the stale custom row
        # happened to come later in pagination than the fresh native one.
        client = self._client([
            {"id": 200, "url": "http://p/1", "is_custom": True},
            {"id": 100, "url": "http://p/1", "is_custom": False},
        ])
        self.assertEqual(client.stream_url_map()["http://p/1"], 100)

    def test_a_url_that_only_exists_as_custom_still_resolves(self):
        client = self._client([{"id": 200, "url": "http://p/1", "is_custom": True}])
        self.assertEqual(client.stream_url_map()["http://p/1"], 200)

    def test_streams_with_no_url_are_skipped_not_crashed_on(self):
        client = self._client([
            {"id": 1, "url": None, "is_custom": False},
            {"id": 2, "url": "http://p/1", "is_custom": False},
        ])
        self.assertEqual(client.stream_url_map(), {"http://p/1": 2})


class TestPerProviderStreamLimit(unittest.TestCase):
    """docs/design/per-provider-m3u-accounts.md's first real piece: once a
    provider has a real Dispatcharr M3U account (not the shared "custom"
    one), that account's own max_streams should be kept in step too --
    it's what Dispatcharr enforces against Live TV AND VOD together, which
    the shared account's limit never could.
    """

    def _client(self, accounts, patches=None):
        from probarr.sources.dispatcharr import Dispatcharr
        c = Dispatcharr("http://x", "u", "p")
        calls = []

        def fake_api(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/api/m3u/accounts/":
                return accounts
            if method == "PATCH":
                acct_id = int(path.strip("/").split("/")[-1])
                acct = next(a for a in accounts if a["id"] == acct_id)
                acct.update(body)
                return acct
            raise AssertionError(f"unexpected call {method} {path}")

        c.api = fake_api
        return c, calls

    def test_finds_account_by_exact_server_url_only(self):
        accounts = [
            {"id": 1, "name": "custom", "server_url": None, "max_streams": 0},
            {"id": 10, "name": "BunnyCustom",
             "server_url": "https://mybunny.tv/client/download.php?u=phgegfxn&p=BmUXAWZPUaQF",  # probarr:allow-secret
             "max_streams": 4},
        ]
        client, _ = self._client(accounts)
        found = client.find_account_for_source(
            "https://mybunny.tv/client/download.php?u=phgegfxn&p=BmUXAWZPUaQF")  # probarr:allow-secret
        self.assertEqual(found["id"], 10)

        # A near-miss (different credentials) must NOT match -- exact
        # equality only, never a same-host guess.
        self.assertIsNone(client.find_account_for_source(
            "https://mybunny.tv/client/download.php?u=someoneelse&p=x"))  # probarr:allow-secret

    def test_enforce_provider_stream_limit_tightens_the_real_account(self):
        accounts = [{"id": 10, "name": "BunnyCustom",
                     "server_url": "https://p.tv/m3u", "max_streams": 8}]
        client, calls = self._client(accounts)
        client.enforce_provider_stream_limit("https://p.tv/m3u", 4)
        self.assertEqual(accounts[0]["max_streams"], 4)
        self.assertTrue(any(m == "PATCH" for m, *_ in calls))

    def test_enforce_provider_stream_limit_never_raises_it(self):
        accounts = [{"id": 10, "name": "BunnyCustom",
                     "server_url": "https://p.tv/m3u", "max_streams": 2}]
        client, calls = self._client(accounts)
        client.enforce_provider_stream_limit("https://p.tv/m3u", 6)
        self.assertEqual(accounts[0]["max_streams"], 2)   # unchanged
        self.assertFalse(any(m == "PATCH" for m, *_ in calls))

    def test_enforce_provider_stream_limit_is_a_noop_with_no_matching_account(self):
        accounts = [{"id": 1, "name": "custom", "server_url": None, "max_streams": 0}]
        client, calls = self._client(accounts)
        client.enforce_provider_stream_limit("https://p.tv/m3u", 4)
        self.assertFalse(any(m == "PATCH" for m, *_ in calls))

    def test_enforce_provider_stream_limit_ignores_a_non_positive_limit(self):
        accounts = [{"id": 10, "name": "BunnyCustom",
                     "server_url": "https://p.tv/m3u", "max_streams": 8}]
        client, calls = self._client(accounts)
        client.enforce_provider_stream_limit("https://p.tv/m3u", 0)
        client.enforce_provider_stream_limit("https://p.tv/m3u", None)
        self.assertFalse(any(m == "PATCH" for m, *_ in calls))

    def test_shared_custom_account_and_real_account_are_independent(self):
        # Both get tightened on a push, to their own separate values --
        # tightening one must not touch the other.
        accounts = [
            {"id": 1, "name": "custom", "server_url": None, "max_streams": 0},
            {"id": 10, "name": "BunnyCustom",
             "server_url": "https://p.tv/m3u", "max_streams": 8},
        ]
        client, _ = self._client(accounts)
        client.enforce_custom_stream_limit(4)
        client.enforce_provider_stream_limit("https://p.tv/m3u", 4)
        self.assertEqual(accounts[0]["max_streams"], 4)
        self.assertEqual(accounts[1]["max_streams"], 4)


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


class FakeDispatcharrClient:
    """Just enough of sources/dispatcharr.py's Dispatcharr to drive plan()
    and push() without a real server -- an in-memory Logo/Group/Channel
    table plus the handful of methods dispatcharr_export.py actually calls.
    """

    def __init__(self, existing_channels=None, existing_logos=None,
                existing_groups=None):
        self._channels = list(existing_channels or [])
        self._logos = list(existing_logos or [])
        self._groups = list(existing_groups or [])
        self._next_id = 1000
        self.created_logos = []   # (name, url) actually POSTed, in order

    def _id(self):
        self._next_id += 1
        return self._next_id

    def channels(self):
        return self._channels

    def logos(self):
        return self._logos

    def groups(self):
        return self._groups

    def get_or_create_group(self, name):
        for g in self._groups:
            if g.get("name", "").strip().lower() == name.strip().lower():
                return g["id"]
        gid = self._id()
        self._groups.append({"id": gid, "name": name})
        return gid

    def get_or_create_logo(self, name, url):
        for l in self._logos:
            if l.get("url") == url:
                return l["id"]
        lid = self._id()
        self._logos.append({"id": lid, "name": name, "url": url})
        self.created_logos.append((name, url))
        return lid

    def create_channel(self, payload):
        ch = {**payload, "id": self._id()}
        self._channels.append(ch)
        return ch

    def update_channel(self, channel_id, payload):
        for c in self._channels:
            if c["id"] == channel_id:
                c.update(payload)
                return c
        raise KeyError(channel_id)

    def match_epg(self, channel_ids):
        pass


class TestDispatcharrStreamsSkipsDisabledAccounts(unittest.TestCase):
    """Real feedback (Discord): using Dispatcharr AS A PROVIDER pulled every
    stream from every M3U account it had ever ingested, including ones
    switched off (is_active=False) in Dispatcharr's own UI -- exactly the
    streams an operator does NOT want probed or matched into a wantlist.
    Distinct from "Import from Dispatcharr" in Curate, which reads
    Dispatcharr's existing CHANNELS and matches them against the run's own
    separately-configured provider pool -- this bug was specific to
    streams(), the raw-catalogue path used when Dispatcharr itself is
    configured as a probarr provider.
    """

    def _client(self, accounts, streams):
        from probarr.sources.dispatcharr import Dispatcharr
        client = Dispatcharr("http://fake", "u", "p")

        def fake_api(method, path, body=None):
            if path.startswith("/api/m3u/accounts/"):
                return {"results": accounts}
            if path.startswith("/api/channels/streams/"):
                return {"results": streams}
            raise AssertionError(f"unexpected call: {method} {path}")
        client.api = fake_api
        return client

    def test_streams_from_a_disabled_account_are_excluded(self):
        accounts = [{"id": 1, "is_active": True}, {"id": 2, "is_active": False}]
        streams = [{"id": 10, "name": "Active One", "url": "http://x/1",
                   "m3u_account": 1},
                  {"id": 11, "name": "Disabled One", "url": "http://x/2",
                   "m3u_account": 2}]
        client = self._client(accounts, streams)
        names = [s.name for s in client.streams()]
        self.assertEqual(names, ["Active One"])

    def test_a_custom_stream_with_no_m3u_account_is_kept(self):
        accounts = [{"id": 1, "is_active": False}]
        streams = [{"id": 10, "name": "Custom", "url": "http://x/1",
                   "m3u_account": None}]
        client = self._client(accounts, streams)
        names = [s.name for s in client.streams()]
        self.assertEqual(names, ["Custom"])


class TestDispatcharrLogoPush(unittest.TestCase):
    """The bug behind the user's own suspicion: a logo picked from
    anywhere OTHER than the M3U's own tvg-logo (a saved EPG source's icon,
    a tv-logo/tv-logos search result) was never actually reaching
    Dispatcharr, because Dispatcharr only auto-creates a Logo row for a URL
    it saw itself while ingesting an M3U -- logo_by_url.get(url) silently
    returned None for anything else, and a plain `if logo_id:` guard meant
    the channel payload just never carried a logo_id at all. No error,
    no plan diff, nothing -- exactly the kind of silent miss that's hard
    to notice without checking Dispatcharr itself.
    """

    def _channel(self, url="https://raw.githubusercontent.com/tv-logo/"
                            "tv-logos/main/countries/united-kingdom/"
                            "bbc-one-uk.png"):
        return {"number": 101, "name": "BBC One",
                "primary": {"stream_id": 1}, "fallback": None,
                "logo_url": url}

    def test_push_creates_a_missing_logo_row_and_links_it(self):
        from probarr.dispatcharr_export import push
        client = FakeDispatcharrClient()
        result = push(client, [self._channel()], default_group_name="probarr")
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(client.created_logos), 1)
        self.assertEqual(client.created_logos[0][1],
                         "https://raw.githubusercontent.com/tv-logo/tv-logos/"
                         "main/countries/united-kingdom/bbc-one-uk.png")
        new_ch = client._channels[0]
        self.assertIsNotNone(new_ch.get("logo_id"))
        linked_logo = next(l for l in client._logos
                           if l["id"] == new_ch["logo_id"])
        self.assertEqual(linked_logo["url"], self._channel()["logo_url"])

    def test_push_does_not_recreate_a_logo_that_already_exists(self):
        from probarr.dispatcharr_export import push
        url = self._channel()["logo_url"]
        client = FakeDispatcharrClient(
            existing_logos=[{"id": 5, "name": "BBC One", "url": url}])
        push(client, [self._channel()], default_group_name="probarr")
        self.assertEqual(client.created_logos, [])
        self.assertEqual(client._channels[0]["logo_id"], 5)

    def test_plan_reports_a_pending_logo_change_for_an_unmatched_url_without_creating_it(self):
        from probarr.dispatcharr_export import plan
        url = self._channel()["logo_url"]
        existing_ch = {"id": 7, "channel_number": 101, "name": "BBC One",
                      "streams": [1], "channel_group_id": 9, "logo_id": None}
        client = FakeDispatcharrClient(
            existing_channels=[existing_ch],
            existing_groups=[{"id": 9, "name": "probarr"}])
        result = plan(client, [self._channel(url)], default_group_name="probarr")
        # The whole point of plan(): describe what push WOULD do without
        # doing it -- so nothing on the fake client's Logo table should
        # have moved.
        self.assertEqual(client._logos, [])
        action = next(a for a in result["actions"] if a["number"] == 101)
        self.assertEqual(action["kind"], "update")
        logo_change = next(c for c in action["changes"] if c["field"] == "logo")
        self.assertEqual(logo_change["to_name"], "(new logo)")

    def test_plan_reports_unchanged_when_the_logo_already_matches(self):
        from probarr.dispatcharr_export import plan
        url = self._channel()["logo_url"]
        existing_ch = {"id": 7, "channel_number": 101, "name": "BBC One",
                      "streams": [1], "channel_group_id": 9, "logo_id": 5}
        client = FakeDispatcharrClient(
            existing_channels=[existing_ch],
            existing_logos=[{"id": 5, "name": "BBC One", "url": url}],
            existing_groups=[{"id": 9, "name": "probarr"}])
        result = plan(client, [self._channel(url)], default_group_name="probarr")
        action = next(a for a in result["actions"] if a["number"] == 101)
        self.assertEqual(action["kind"], "unchanged")


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


class TestExpectedNowHonoursExplicitEpgSource(Temp):
    """Real bug report: after explicitly picking a different EPG source for
    a channel in Check EPG, diagnosing that channel still captured the OLD
    source's programme as `expected`. Root cause -- _expected_now() only
    ever walked every saved source in list order and took whichever matched
    first, blind to the channel's own selection.json pick. Everywhere else
    an explicit epg_source is honoured (the live Check EPG panel, the
    actual Dispatcharr push via _resolve_epg_overrides()); this is the one
    place that silently wasn't.
    """

    def _guide(self, name, channel_name, programme_title):
        import datetime
        xml = os.path.join(self.root, name + ".xml")
        now = datetime.datetime.now(datetime.timezone.utc)
        start = (now - datetime.timedelta(minutes=30)).strftime("%Y%m%d%H%M%S +0000")
        stop = (now + datetime.timedelta(minutes=30)).strftime("%Y%m%d%H%M%S +0000")
        body = (f'<channel id="c1"><display-name>{channel_name}</display-name></channel>'
               f'<programme channel="c1" start="{start}" stop="{stop}">'
               f'<title>{programme_title}</title></programme>')
        with open(xml, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0"?><tv>{body}</tv>')
        from probarr import epgsources
        epgsources.save(self.root, name, "file://" + xml)

    def _record(self):
        return {"channel_key": "NATGEO", "stream_name": "National Geographic",
                "tvg_id": ""}

    def test_falls_back_to_the_first_saved_source_with_no_explicit_pick(self):
        from probarr import web as web_mod
        from probarr.store import RunStore
        web_mod.Handler.root = self.root
        self._guide("aaa-old", "National Geographic", "Old Programme")
        self._guide("zzz-new", "National Geographic", "New Programme")
        store = RunStore(self.root, "run1")
        got = web_mod.Handler._expected_now(self._record(), store)
        self.assertEqual(got["title"], "Old Programme")

    def test_an_explicitly_picked_source_wins_even_when_listed_second(self):
        from probarr import web as web_mod
        from probarr.store import RunStore
        web_mod.Handler.root = self.root
        self._guide("aaa-old", "National Geographic", "Old Programme")
        self._guide("zzz-new", "National Geographic", "New Programme")
        store = RunStore(self.root, "run1")
        store.write_selection({"NATGEO": {"epg_source": "zzz-new"}})
        got = web_mod.Handler._expected_now(self._record(), store)
        self.assertEqual(got["title"], "New Programme")

    def test_falls_back_when_the_picked_source_does_not_match_this_channel(self):
        from probarr import web as web_mod
        from probarr.store import RunStore
        web_mod.Handler.root = self.root
        self._guide("aaa-old", "National Geographic", "Old Programme")
        self._guide("zzz-new", "Some Other Channel", "New Programme")
        store = RunStore(self.root, "run1")
        store.write_selection({"NATGEO": {"epg_source": "zzz-new"}})
        got = web_mod.Handler._expected_now(self._record(), store)
        self.assertEqual(got["title"], "Old Programme")


class TestWatermarkCrop(Temp):
    """A channel nobody has marked a watermark area for must trigger NO
    work at all, not even a fast local one -- the whole point raised when
    this was scoped. _watermark_crop() is the only place cropping ever
    happens, so its 404-with-no-box behaviour IS the entire enforcement.
    """

    def _handler(self):
        from probarr import web as web_mod
        web_mod.Handler.root = self.root
        h = web_mod.Handler.__new__(web_mod.Handler)
        sent = []
        h._send = lambda body, ctype="text/plain", code=200: sent.append(
            (code, ctype, body))
        h._file = lambda run_id, rest: sent.append(("FILE", rest)) or sent
        return h, sent

    def test_no_watermark_box_is_a_404_and_never_touches_the_frame(self):
        from probarr.store import RunStore
        import unittest.mock
        store = RunStore(self.root, "run1")
        store.write_selection({"BBCONE": {"group": "News"}})  # no watermark_box
        h, sent = self._handler()
        with unittest.mock.patch("probarr.web.subprocess") as fake_subprocess:
            h._watermark_crop("run1", "BBCONE|s1")
            fake_subprocess.run.assert_not_called()
        self.assertEqual(sent[0][0], 404)

    def test_missing_frame_file_is_a_404(self):
        from probarr.store import RunStore
        store = RunStore(self.root, "run1")
        store.write_selection({"BBCONE": {"watermark_box":
                               {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}}})
        h, sent = self._handler()
        # frame_path exists on disk for no candidate here -- 404, not a crash.
        h._watermark_crop("run1", "BBCONE|s1")
        self.assertEqual(sent[0][0], 404)

    def test_crops_the_existing_frame_and_serves_the_result(self):
        from probarr.store import RunStore
        import unittest.mock
        store = RunStore(self.root, "run1")
        store.write_selection({"BBCONE": {"watermark_box":
                               {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}}})
        frame_path = store.frame_path("BBCONE|s1")
        os.makedirs(os.path.dirname(frame_path), exist_ok=True)
        with open(frame_path, "wb") as f:
            f.write(b"not a real jpeg, ffmpeg is mocked")
        h, sent = self._handler()
        with unittest.mock.patch("probarr.web.subprocess") as fake_subprocess:
            fake_subprocess.CalledProcessError = Exception
            fake_subprocess.TimeoutExpired = Exception

            def fake_run(cmd, **kw):
                # Simulate ffmpeg actually writing the output file.
                out_path = cmd[-1]
                with open(out_path, "wb") as f:
                    f.write(b"cropped")
            fake_subprocess.run.side_effect = fake_run
            h._watermark_crop("run1", "BBCONE|s1")
            fake_subprocess.run.assert_called_once()
        self.assertEqual(sent[0][0], "FILE")
        self.assertEqual(sent[0][1][0], "watermarks")
        self.assertTrue(sent[0][1][1].startswith(
            RunStore.safe_name("BBCONE|s1")))

    def test_redrawing_the_box_produces_a_different_cached_filename(self):
        from probarr.store import RunStore
        import unittest.mock
        store = RunStore(self.root, "run1")
        frame_path = store.frame_path("BBCONE|s1")
        os.makedirs(os.path.dirname(frame_path), exist_ok=True)
        with open(frame_path, "wb") as f:
            f.write(b"x")

        def crop_with(box):
            store.write_selection({"BBCONE": {"watermark_box": box}})
            h, sent = self._handler()
            with unittest.mock.patch("probarr.web.subprocess") as fake_subprocess:
                fake_subprocess.CalledProcessError = Exception
                fake_subprocess.TimeoutExpired = Exception
                def fake_run(cmd, **kw):
                    with open(cmd[-1], "wb") as f:
                        f.write(b"x")
                fake_subprocess.run.side_effect = fake_run
                h._watermark_crop("run1", "BBCONE|s1")
            return sent[0][1][1]

        name_a = crop_with({"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1})
        name_b = crop_with({"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1})
        self.assertNotEqual(name_a, name_b)

    def test_a_re_probed_frame_invalidates_the_cached_crop_even_with_the_same_box(self):
        # Real bug, caught live: a candidate re-probed after its watermark
        # crop was already cached kept serving the OLD crop indefinitely --
        # "the file already exists" was the only check, so a completely
        # different picture underneath the same unchanged box never got
        # noticed. The box's own hash only invalidates when the MARKED
        # AREA changes; a re-probe changes the PICTURE, not the area.
        import time
        import unittest.mock
        from probarr.store import RunStore

        store = RunStore(self.root, "run1")
        store.write_selection({"BBCONE": {"watermark_box":
                               {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}}})
        frame_path = store.frame_path("BBCONE|s1")
        os.makedirs(os.path.dirname(frame_path), exist_ok=True)

        def do_crop(marker):
            with open(frame_path, "wb") as f:
                f.write(marker)
            h, sent = self._handler()
            with unittest.mock.patch("probarr.web.subprocess") as fake_subprocess:
                fake_subprocess.CalledProcessError = Exception
                fake_subprocess.TimeoutExpired = Exception
                def fake_run(cmd, **kw):
                    # A real crop's bytes are derived from the source frame;
                    # standing in for that here with the same marker so the
                    # test can tell "regenerated from the new frame" apart
                    # from "served the stale cached file" by content, not
                    # just by whether ffmpeg was invoked.
                    with open(cmd[-1], "wb") as f:
                        f.write(marker)
                fake_subprocess.run.side_effect = fake_run
                h._watermark_crop("run1", "BBCONE|s1")
            out_name = sent[0][1][1]
            with open(os.path.join(store.dir, "watermarks", out_name), "rb") as f:
                return f.read()

        first = do_crop(b"original broadcast frame")
        self.assertEqual(first, b"original broadcast frame")

        # The frame file's mtime must genuinely be newer than the cached
        # crop's for this to be a fair test of the staleness check, not an
        # accident of both happening within the same filesystem-timestamp
        # tick.
        time.sleep(1.05)
        second = do_crop(b"re-probed, totally different content")
        self.assertEqual(second, b"re-probed, totally different content")


class TestEpgSourceConsensus(Temp):
    """probarr never scored EPG matches against each other or read a
    guide's own <icon> at all -- both real gaps, not different approaches
    to something already covered. Word-overlap scoring lets a household
    running more than one EPG source prefer whichever source's own name
    for a channel actually agrees with what it's called, and the icon
    becomes a real (if last-resort) source of a channel's logo.
    """

    def _guide(self, name, channels):
        xml = os.path.join(self.root, f"{name}.xml")
        body = "".join(
            f'<channel id="{cid}">'
            f'<display-name>{dname}</display-name>'
            + (f'<icon src="{icon}"/>' if icon else "") + '</channel>'
            for cid, dname, icon in channels)
        with open(xml, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0"?><tv>{body}</tv>')
        from probarr import epgsources
        epgsources.save(self.root, name, "file://" + xml)

    def test_word_set_ignores_punctuation_and_case(self):
        from probarr.epgcheck import _word_set
        self.assertEqual(_word_set("UK: BBC Two!"), {"UK", "BBC", "TWO"})

    def test_check_all_scores_by_shared_words_not_just_match_or_not(self):
        from probarr import epgcheck
        self._guide("good-guide", [("1", "BBC Two Lon", None)])
        self._guide("vague-guide", [("2", "BBC Two Lon", None)])
        out = epgcheck.check_all(self.root, "UK: BBC Two", "", Normalizer())
        # Both matched the identical entry (same fixture content) -- the
        # point here is that a real score is attached at all, not zero
        # regardless of how well the names actually agree.
        for entry in out:
            self.assertTrue(entry["matched"])
            self.assertGreaterEqual(entry["score"], 2)   # "BBC" and "TWO"

    def test_consensus_requires_at_least_two_sources_to_actually_agree(self):
        from probarr import epgcheck
        # One source matches well, the other doesn't carry this channel at
        # all -- a single opinion, however good, is not a consensus.
        self._guide("has-it", [("1", "BBC Two Lon", None)])
        self._guide("lacks-it", [("2", "Totally Different Channel", None)])
        out = epgcheck.check_all(self.root, "UK: BBC Two", "", Normalizer())
        winner = epgcheck.consensus_winner(out)
        self.assertIsNotNone(winner)
        self.assertEqual(winner["source"], "has-it")
        self.assertFalse(winner["consensus"])

    def test_consensus_true_when_two_sources_independently_agree(self):
        from probarr import epgcheck
        self._guide("src-a", [("1", "BBC Two Lon", None)])
        self._guide("src-b", [("2", "BBC Two North", None)])
        out = epgcheck.check_all(self.root, "UK: BBC Two", "", Normalizer())
        winner = epgcheck.consensus_winner(out)
        self.assertTrue(winner["consensus"])

    def test_logo_is_read_from_the_winning_sources_icon(self):
        from probarr import epgcheck
        self._guide("with-icon", [("1", "BBC Two Lon", "https://x/bbctwo.png")])
        out = epgcheck.check_all(self.root, "UK: BBC Two", "", Normalizer())
        winner = epgcheck.consensus_winner(out)
        self.assertEqual(winner["logo"], "https://x/bbctwo.png")

    def test_trust_tiebreaks_equally_scored_sources(self):
        from probarr import epgcheck
        # Two sources score identically for this channel -- give one of
        # them a real track record of winning past consensus checks and
        # confirm it's preferred over the other, not just whichever comes
        # first alphabetically.
        self._guide("aaa-newcomer", [("1", "BBC Two Lon", None)])
        self._guide("zzz-veteran", [("2", "BBC Two Lon", None)])
        epgcheck._bump_trust(self.root, "zzz-veteran", ["aaa-newcomer", "zzz-veteran"])
        for _ in range(9):
            epgcheck._bump_trust(self.root, "zzz-veteran", ["zzz-veteran"])
        out = epgcheck.check_all(self.root, "UK: BBC Two", "", Normalizer())
        winner = epgcheck.consensus_winner(out, root=self.root)
        self.assertEqual(winner["source"], "zzz-veteran")

    def test_bump_trust_is_best_effort_and_never_raises(self):
        from probarr import epgcheck
        # A root that cannot be written to (nonexistent parent) must not
        # bring down whatever real request triggered this as a side effect.
        epgcheck._bump_trust("/no/such/directory", "x", ["x"])  # must not raise

    def test_epg_fallback_logo_only_used_when_the_m3u_gave_none(self):
        # web.py's _resolve_curated(): the M3U's own tvg-logo always wins
        # when present -- _epg_fallback_logo() is only ever CALLED for a
        # channel whose primary candidate's logo is falsy in the first
        # place, so this exercises the fallback resolver itself.
        from probarr import web as web_mod
        self._guide("only-guide", [("1", "BBC Two Lon", "https://x/bbctwo.png")])
        web_mod.Handler.root = self.root
        handler = web_mod.Handler.__new__(web_mod.Handler)
        self.assertEqual(
            handler._epg_fallback_logo("UK: BBC Two", ""),
            "https://x/bbctwo.png")

    def test_epg_fallback_logo_is_empty_string_not_none_or_error_when_nothing_matches(self):
        from probarr import web as web_mod
        web_mod.Handler.root = self.root
        handler = web_mod.Handler.__new__(web_mod.Handler)
        self.assertEqual(handler._epg_fallback_logo("Totally Unmatched Channel", ""), "")

    def test_prewarm_indexes_every_saved_source_so_the_next_check_is_fast(self):
        # Real incident this exists for: Curate's persistent EPG badge
        # fires automatically for the first channel on every page load,
        # and the in-memory guide cache does not survive a run (which
        # routinely outlasts its TTL) or a restart (cold by definition).
        # prewarm_all_sources() is meant to pay that cost ahead of time,
        # in the background, at exactly those two moments.
        from probarr import epgcheck
        self._guide("src-a", [("1", "BBC Two Lon", None)])
        self._guide("src-b", [("2", "BBC Two North", None)])
        epgcheck._cache.clear()
        epgcheck._indexed.clear()
        epgcheck.prewarm_all_sources(self.root, Normalizer())
        self.assertEqual(len(epgcheck._cache), 2)
        self.assertEqual(len(epgcheck._indexed), 2)

    def test_prewarm_is_best_effort_and_never_raises_on_a_bad_source(self):
        from probarr import epgcheck
        from probarr import epgsources
        epgsources.save(self.root, "broken", "file:///no/such/file.xml")
        epgcheck.prewarm_all_sources(self.root, Normalizer())  # must not raise


class TestEpgConsensus(Temp):
    def _guide(self, channels):
        xml = os.path.join(self.root, "guide.xml")
        body = "".join(f'<channel id="{cid}"><display-name>{name}</display-name></channel>'
                       for cid, name in channels)
        with open(xml, "w", encoding="utf-8") as f:
            f.write(f'<?xml version="1.0"?><tv>{body}</tv>')
        from probarr import epgsources
        epgsources.save(self.root, "test-guide", "file://" + xml)

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


class TestIndexRedirect(Temp):
    """"/" is a landing pad, not a page of its own -- it should drop you
    straight into whatever you were last working on (Curate for the newest
    run) rather than an intermediate list you have to click through every
    time. The list itself moved to /runs, still reachable from the Runs
    nav tab, for the times you actually want to browse or delete a run.
    """

    def _handler(self):
        from probarr import web as web_mod
        web_mod.Handler.root = self.root
        h = web_mod.Handler.__new__(web_mod.Handler)
        sent = []
        h.send_response = lambda code: sent.append(["status", code])
        h.send_header = lambda k, v: sent.append([k, v])
        h.end_headers = lambda: None
        return h, sent

    def test_redirects_to_the_newest_runs_curate_page(self):
        from probarr.store import RunStore
        RunStore(self.root, "20260101-000000").write_meta({})
        RunStore(self.root, "20260825-000000").write_meta({})
        h, sent = self._handler()
        h.path = "/"
        h.do_GET()
        status = next(v for k, v in sent if k == "status")
        location = next(v for k, v in sent if k == "Location")
        self.assertEqual(status, 302)
        self.assertEqual(location, "/run/20260825-000000/curate")

    def test_with_no_runs_at_all_redirects_to_the_runs_list(self):
        h, sent = self._handler()
        h.path = "/"
        h.do_GET()
        status = next(v for k, v in sent if k == "status")
        location = next(v for k, v in sent if k == "Location")
        self.assertEqual(status, 302)
        self.assertEqual(location, "/runs")


class TestLogos(Temp):
    """logos.py never touches image bytes -- only two small JSON listings
    (country names, per-country filenames) ever get cached. These tests
    mock the network entirely and check the caching and matching logic in
    isolation from GitHub's actual API.
    """

    def test_search_prefers_the_closer_normalized_match(self):
        from probarr import logos as logos_mod
        from probarr.normalize import Normalizer
        with unittest.mock.patch.object(
                logos_mod, "fetch_country_logos",
                return_value=["bbc-one-uk.png", "bbc-news-uk.png",
                              "itv1-uk.png"]):
            results = logos_mod.search(self.root, "UK: BBC One HD",
                                       "united-kingdom", Normalizer())
        self.assertTrue(results)
        self.assertEqual(results[0]["filename"], "bbc-one-uk.png")
        self.assertTrue(results[0]["url"].startswith(
            "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
            "countries/united-kingdom/"))

    def test_search_with_no_country_or_query_returns_nothing_and_fetches_nothing(self):
        from probarr import logos as logos_mod
        from probarr.normalize import Normalizer
        with unittest.mock.patch.object(
                logos_mod, "fetch_country_logos") as fake_fetch:
            self.assertEqual(logos_mod.search(self.root, "bbc one", "",
                                              Normalizer()), [])
            self.assertEqual(logos_mod.search(self.root, "", "united-kingdom",
                                              Normalizer()), [])
            fake_fetch.assert_not_called()

    def test_fetch_country_logos_is_cached_to_disk_not_refetched(self):
        from probarr import logos as logos_mod
        logos_mod._mem.clear()
        calls = []

        def fake_get_json(url):
            calls.append(url)
            return [{"name": "bbc-one-uk.png", "type": "file"},
                   {"name": "README.md", "type": "file"}]

        with unittest.mock.patch.object(logos_mod, "_get_json", fake_get_json):
            first = logos_mod.fetch_country_logos(self.root, "united-kingdom")
            logos_mod._mem.clear()  # force the disk tier, not the in-memory one
            second = logos_mod.fetch_country_logos(self.root, "united-kingdom")
        self.assertEqual(first, ["bbc-one-uk.png"])
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1, "second call should have hit the disk cache")

    def test_a_failed_fetch_yields_an_empty_list_not_an_exception(self):
        from probarr import logos as logos_mod
        logos_mod._mem.clear()
        with unittest.mock.patch.object(
                logos_mod, "_get_json", side_effect=OSError("network down")):
            self.assertEqual(logos_mod.fetch_countries(self.root), [])
            self.assertEqual(
                logos_mod.fetch_country_logos(self.root, "united-kingdom"), [])

    def test_resolve_curated_prefers_an_explicit_logo_override(self):
        from probarr import web as web_mod
        from probarr.store import RunStore
        store = RunStore(self.root, "run1")
        store.append({"rec_key": "BBCONE|s1", "channel_key": "BBCONE",
                      "stream_id": "s1", "stream_name": "BBC One",
                      "status": "ok", "url": "http://x/1", "url_redacted": "",
                      "group": "", "logo": "https://example.com/m3u-logo.png",
                      "tvg_id": "", "probed_at": 1})
        store.write_wantlist_raw(
            [{"key": "BBCONE", "number": "101", "name": "BBC One"}], [])
        store.write_selection({"BBCONE": {
            "logo_override": "https://raw.githubusercontent.com/tv-logo/"
                             "tv-logos/main/countries/united-kingdom/"
                             "bbc-one-uk.png"}})
        web_mod.Handler.root = self.root
        h = web_mod.Handler.__new__(web_mod.Handler)
        curated = h._resolve_curated(store)
        ch = next(c for c in curated if c["key"] == "BBCONE")
        self.assertEqual(ch["logo_url"],
                         "https://raw.githubusercontent.com/tv-logo/tv-logos/"
                         "main/countries/united-kingdom/bbc-one-uk.png")


if __name__ == "__main__":
    unittest.main()
