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
    def test_collapses_sd_hd_pairs_but_keeps_real_regional_variants(self):
        from probarr import epgcheck, epgsources
        xml = os.path.join(self.root, "guide.xml")
        with open(xml, "w", encoding="utf-8") as f:
            f.write("""<?xml version="1.0"?><tv>
                <channel id="1"><display-name>BBC One</display-name></channel>
                <channel id="2"><display-name>BBC One HD</display-name></channel>
                <channel id="3"><display-name>BBC One London</display-name></channel>
                <channel id="4"><display-name>BBC One North West</display-name></channel>
            </tv>""")
        epgsources.save(self.root, "test-guide", "file://" + xml)
        out = epgcheck.list_channels(self.root, "test-guide", Normalizer())
        names = sorted(c["guide_name"] for c in out)
        # "BBC One" and "BBC One HD" are the same channel -- one row, the
        # plain (shorter, less qualified) name wins as the representative.
        # "BBC One London" and "BBC One North West" are genuinely different
        # regional feeds -- neither the wantlist parser nor this collapses
        # those, so both keep their own row.
        self.assertEqual(names, ["BBC One", "BBC One London", "BBC One North West"])


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
        return {"curate": curate.HTML,
                **{n: getattr(pages, n) for n in
                   ("WANTLIST_PAGE", "SETTINGS_PAGE", "PROVIDERS_PAGE",
                    "NEWRUN_PAGE", "BROWSE_PAGE", "LINEUPS_PAGE")}}

    def test_templates_are_raw_strings(self):
        for path in ("probarr/curate.py", "probarr/pages.py"):
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
