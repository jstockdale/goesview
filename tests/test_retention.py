"""Retention: what GOESview may delete, and above all what it may not.

These pin the promises in the README's "Retention" section. SatDump's received
data cannot be recovered once deleted, so most of these tests build a folder
with tempting things in it, run a sweep, and check that exactly the expected
paths are gone and every other byte is where it was.

Nothing here starts the Pruner thread: sweep_cache() and sweep_satdump() are
called directly.
"""
import argparse
import collections
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, FULL_DISK, HOUR, OLD_RUN, RUN  # noqa: E402

goesview = fx.goesview
Limits = goesview.Limits

Usage = collections.namedtuple("Usage", "total used free")
DISK = 2 ** 40  # a pretend 1 TiB disk; with a 50% cap the arithmetic is exact


def disk_over_cap_by(excess):
    """Pretend the disk is `excess` bytes over a 50% cap (negative: under it)."""
    used = DISK // 2 + excess
    return mock.patch.object(goesview.shutil, "disk_usage", return_value=Usage(DISK, used, DISK - used))


# ------------------------------------------------------------- option parsing

class ParsingTests(unittest.TestCase):
    def test_parse_age_units(self):
        """Ages are given as 36h, 14d, 2w, 3mo, 1y; a bare number means days."""
        for text, seconds in [("36h", 36 * HOUR), ("14d", 14 * DAY), ("2w", 14 * DAY), ("3mo", 90 * DAY),
                              ("1y", 365 * DAY), ("30", 30 * DAY), ("1.5d", 1.5 * DAY), ("3h", 3 * HOUR),
                              (" 14 D ", 14 * DAY), ("2weeks", 14 * DAY), ("3months", 90 * DAY),
                              ("5hours", 5 * HOUR), ("1year", 365 * DAY)]:
            with self.subTest(text=text):
                self.assertEqual(goesview.parse_age(text), seconds)

    def test_parse_age_refuses_less_than_three_hours(self):
        """No age limit can reach inside the 3 hours that are never deleted."""
        for text in ("2h", "2.9h", "0.1d", "0", "0d", "0.1"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    goesview.parse_age(text)

    def test_parse_age_refuses_what_it_cannot_read(self):
        """A typo must be an error, not a guess: "5m" could be minutes or months."""
        for text in ("", "abc", "-3d", "5m", "5min", "3 4d", "d", "3d2h", "1e3d"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    goesview.parse_age(text)

    def test_cache_keep_can_be_switched_off(self):
        """--cache-keep off keeps the cache forever (None), and still reads ages."""
        for text in ("off", "OFF", "none", "never", "0", " off "):
            with self.subTest(text=text):
                self.assertIsNone(goesview.parse_age_or_off(text))
        self.assertEqual(goesview.parse_age_or_off("14d"), 14 * DAY)
        with self.assertRaises(argparse.ArgumentTypeError):
            goesview.parse_age_or_off("1h")

    def test_parse_size_units(self):
        """Sizes are binary: 500M, 200G, 1.5T, with an optional B or iB."""
        for text, size in [("500M", 500 * 1024 ** 2), ("200G", 200 * 1024 ** 3), ("1.5T", int(1.5 * 1024 ** 4)),
                           ("10k", 10 * 1024), ("20GB", 20 * 1024 ** 3), ("20GiB", 20 * 1024 ** 3),
                           (" 20 g ", 20 * 1024 ** 3), ("0.5m", 512 * 1024)]:
            with self.subTest(text=text):
                self.assertEqual(goesview.parse_size(text), size)

    def test_parse_size_refuses_a_bare_number(self):
        """--satdump-max-size 200 must not be read as 200 bytes (which would delete everything)."""
        for text in ("200", "", "big", "-5G", "5X", "G", "5 GG"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    goesview.parse_size(text)

    def test_parse_cap(self):
        """A disk cap is a percentage between 1 and 99, with or without the sign."""
        self.assertAlmostEqual(goesview.parse_cap("90%"), 0.9)
        self.assertAlmostEqual(goesview.parse_cap("90"), 0.9)
        self.assertAlmostEqual(goesview.parse_cap(" 72.5 % "), 0.725)
        for text in ("0", "0%", "0.5", "100%", "150", "", "full", "-5%", "90%%"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    goesview.parse_cap(text)

    def test_limits_without_any_limit_are_false(self):
        """Limits() is what "no --satdump-* option" turns into; it must test false."""
        self.assertFalse(Limits())
        self.assertFalse(Limits(None, None, None))
        self.assertTrue(Limits(keep=30 * DAY))
        self.assertTrue(Limits(max_size=1))
        self.assertTrue(Limits(disk_cap=0.9))

    def test_limits_describe_names_each_limit(self):
        text = Limits(keep=30 * DAY, max_size=20 * 1024 ** 3, disk_cap=0.9).describe()
        self.assertIn("30 days", text)
        self.assertIn("20.0 GB", text)
        self.assertIn("90%", text)
        self.assertIn("1 day", Limits(keep=DAY).describe())
        self.assertNotIn("1 days", Limits(keep=DAY).describe())

    def test_sizes_and_counts_read_naturally(self):
        """The wording of the log and the Live page's Storage block."""
        self.assertEqual([goesview.human(n) for n in (0, 1023, 1536, 5 * 1024 ** 2, 20 * 1024 ** 3, 3 * 1024 ** 4)],
                         ["0 B", "1023 B", "1.5 kB", "5.0 MB", "20.0 GB", "3.0 TB"])
        self.assertEqual([goesview.count(n, "folder") for n in (0, 1, 2, 1.5)],
                         ["0 folders", "1 folder", "2 folders", "1.5 folders"])

    def test_satdump_product_names(self):
        """What counts as "a file SatDump writes into a timestamp folder"."""
        for name in ("G18_2_20261117T201021Z.png", "G18_13_20261117T201021Z.png", "G19_7_20261117T201021Z_1.png",
                     "HIM_13_20261117T200000Z.png", "G18_ACHA_20261117T201021Z.png",
                     "abi_rgb_ABI_False_Color.png", "abi_rgb_ABI_False_Color_map.png", "product.cbor"):
            with self.subTest(name=name):
                self.assertTrue(goesview.satdump_product(name))
        for name in ("notes.txt", "my-crop.png", "G18_2.png", "product.cbor.bak", "Thumbs.db", ".DS_Store",
                     "False_Color_usradioguy.jpg", "IMG_2041.jpg"):
            with self.subTest(name=name):
                self.assertFalse(goesview.satdump_product(name))


# ---------------------------------------------- which folders count as GOES runs

class GoesRunsTests(fx.GoesviewCase):
    def names(self, paths):
        return [os.path.basename(p) for p in paths]

    def test_only_goes_hrit_named_folders_with_data_are_runs(self):
        """Runs are the folders directly under the root that carry SatDump's goes_hrit
        name and hold IMAGES/, L2/ or EMWIN/. Everything else is "others"."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(old, run=RUN)
        t.bulletin(old, run=OLD_RUN)                       # a run holding only EMWIN still counts
        t.slot(old, run="2026-11-01_08-00_noaa_hrpt_1.7 GHz", group="IMAGES/NOAA-19")
        t.slot(old, run="my goes pictures")                 # GOES images, but the user's own folder
        t.write("goes_hrit_empty/keep.txt")                 # the name, but no data folders
        t.write("goes_hrit_notes.txt")                      # the name, but a file
        t.write("baseband.cf32")
        runs, protected, others = goesview.goes_runs(t.root)
        self.assertEqual(self.names(runs), [OLD_RUN, RUN])
        self.assertEqual(protected, [])
        self.assertEqual(others, ["2026-11-01_08-00_noaa_hrpt_1.7 GHz", "baseband.cf32", "goes_hrit_empty",
                                  "goes_hrit_notes.txt", "my goes pictures"])

    def test_runs_are_only_looked_for_directly_under_the_root(self):
        """A goes_hrit folder nested inside another folder is not a run."""
        t = self.tree
        t.slot(self.now - 40 * DAY, run=f"archive-2025/{RUN}")
        runs, protected, others = goesview.goes_runs(t.root)
        self.assertEqual((runs, protected, others), ([], [], ["archive-2025"]))

    def test_protect_moves_a_run_out_of_reach(self):
        t = self.tree
        t.slot(self.now, run=RUN)
        t.slot(self.now, run=OLD_RUN)
        runs, protected, others = goesview.goes_runs(t.root, protect={OLD_RUN})
        self.assertEqual(self.names(runs), [RUN])
        self.assertEqual(self.names(protected), [OLD_RUN])
        self.assertEqual(others, [])

    def test_include_adds_a_folder_the_user_named(self):
        """--satdump-include makes a self-named folder a run, but only if it holds data."""
        t = self.tree
        t.slot(self.now, run="my goes pictures")
        t.write("holiday photos/beach.jpg")
        runs, _, others = goesview.goes_runs(t.root, include={"my goes pictures", "holiday photos"})
        self.assertEqual(self.names(runs), ["my goes pictures"])
        self.assertEqual(others, ["holiday photos"])

    def test_a_symlinked_run_folder_is_not_a_run(self):
        """A link named like a run must not lead retention out of the root."""
        t = self.tree
        outside = os.path.join(t.tmp, "elsewhere", RUN)
        os.makedirs(os.path.join(outside, "IMAGES"))
        os.symlink(outside, t.path("2025-01-01_00-00_goes_hrit_link"))
        runs, protected, others = goesview.goes_runs(t.root)
        self.assertEqual(runs, [])
        self.assertEqual(others, ["2025-01-01_00-00_goes_hrit_link"])

    def test_root_that_is_itself_a_run(self):
        """Pointed straight at one run folder, that folder is the only run."""
        t = self.tree
        run = t.path(RUN)
        t.slot(self.now, run=RUN)
        self.assertEqual(goesview.goes_runs(run), ([run], [], []))
        # a renamed run folder still counts when pointed at directly, if it holds IMAGES/GOES-nn
        t.slot(self.now, run="renamed")
        self.assertEqual(goesview.goes_runs(t.path("renamed")), ([t.path("renamed")], [], []))
        # but a folder of some other satellite's images does not
        t.slot(self.now, run="noaa", group="IMAGES/NOAA-19")
        self.assertEqual(goesview.goes_runs(t.path("noaa"))[0], [])

    def test_missing_root(self):
        self.assertEqual(goesview.goes_runs(os.path.join(self.tree.tmp, "nowhere")), ([], [], []))


# ----------------------------------------------- the default: never delete SatDump data

def build_estate(tree, now):
    """A root as a user's might really look. Returns (old, fresh, bystanders):
    old and fresh SatDump products inside GOES HRIT runs, and everything a
    --satdump-* limit must never reach."""
    old = now - 40 * DAY
    t = tree
    cur_slot = t.slot(old, extra=("abi_rgb_ABI_False_Color.png", "abi_rgb_ABI_False_Color_map.png"))
    t.write(cur_slot + "/product.cbor", b"\xa0")
    olds = [
        cur_slot,
        t.slot(old, group="L2/GOES-18/Full Disk", bands=("ACHA", "SST")),
        t.bulletin(old),
        t.bulletin(old, ident="RADNTHES", ext="GIF", text=b"GIF89a"),
        t.chart(old),
        t.slot(old - 20 * DAY, run=OLD_RUN),               # old runs are covered on purpose
        t.bulletin(old - 20 * DAY, run=OLD_RUN),
    ]
    fresh = [t.slot(now - 600), t.bulletin(now - 600), t.chart(now - 600)]
    other_sat = "2026-11-01_08-00_noaa_hrpt_1.7 GHz"
    bystanders = [
        # another satellite's run, with a product name SatDump retention would recognise
        t.png(f"{other_sat}/IMAGES/NOAA-19/{fx.slot_name(old)}/avhrr_3_rgb_221.png"),
        t.slot(old, run=other_sat, group="IMAGES/GOES-18/Full Disk"),
        # the user's own folder of GOES images
        t.slot(old, run="my goes pictures"),
        t.bulletin(old, run="my goes pictures"),
        # loose files beside the runs, some with SatDump-like names
        t.write("baseband_1694100000Hz.cf32", b"\0" * 64),
        t.write("notes.txt"),
        t.write("goes_hrit_notes.txt"),
        t.write(fx.emwin_name(old)),
        t.write(fx.nws_name(old)),
        # named like a run, but not one
        t.write("goes_hrit_empty/keep.txt"),
        t.png(f"goes_hrit_empty/{fx.slot_name(old)}/{fx.band_name(old)}"),
        # inside a real run, but outside IMAGES/, L2/ and EMWIN/
        t.write(f"{RUN}/dataset.json", b"{}"),
        t.write(f"{RUN}/DCS/DCS_{fx.slot_name(old)}.json", b"[]"),
        t.write(f"{RUN}/Admin Messages/{fx.emwin_name(old)}"),
        t.png(f"{RUN}/my-notes/{fx.slot_name(old)}/{fx.band_name(old)}"),
        t.write(f"{OLD_RUN}/goes_hrit.log"),
    ]
    return olds, fresh, bystanders


class NeverWithoutAskingTests(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        build_estate(self.tree, self.now - 400 * DAY)  # everything over a year old by any clock
        self.app = fx.make_app(self, self.tree)
        self.before = fx.snapshot(self.tree.root)
        self.assertGreater(len(self.before), 30)

    def sweep_everything(self, pruner):
        pruner.sweep_cache()
        pruner.sweep_satdump()
        pruner.sweep_dcs()

    def test_default_configuration_deletes_nothing_satdump_wrote(self):
        """README: "GOESview never deletes it unless you ask by name." With main()'s
        defaults (cache kept 30 days, no --satdump-* option) a year-old archive is untouched."""
        pruner = fx.make_pruner(self.app, cache=Limits(keep=30 * DAY))
        self.sweep_everything(pruner)
        self.assertEqual(fx.snapshot(self.tree.root), self.before)
        self.assertIsNone(pruner.scope, "no SatDump limit: there is no scope to report")
        self.assertIsNone(pruner.last["satdump"])

    def test_satdump_limits_of_none_delete_nothing(self):
        pruner = goesview.Pruner(self.app, Limits(keep=30 * DAY), None, 600, False)
        self.sweep_everything(pruner)
        self.assertEqual(fx.snapshot(self.tree.root), self.before)

    def test_harshest_cache_limits_still_delete_nothing_satdump_wrote(self):
        """The --cache-* options never touch SatDump's files, however tight they are."""
        pruner = fx.make_pruner(self.app, cache=Limits(keep=3 * HOUR, max_size=1, disk_cap=0.5))
        with disk_over_cap_by(2 ** 38):
            self.sweep_everything(pruner)
        self.assertEqual(fx.snapshot(self.tree.root), self.before)

    def test_stats_report_no_satdump_policy(self):
        """The Live page's Storage block shows that SatDump data is not subject to deletion."""
        fx.make_pruner(self.app, cache=Limits(keep=30 * DAY))
        storage = self.app.stats()["storage"]
        self.assertIsNone(storage["satdump"])
        self.assertIsNone(storage["scope"])
        self.assertIn("30 days", storage["cache"])


# ---------------------------------------------------- what a SatDump limit can reach

class SatdumpScopeTests(fx.GoesviewCase):
    def test_only_goes_hrit_runs_are_reached(self):
        """With --satdump-keep, old products in GOES HRIT runs (old runs included) go.
        Other satellites' runs, the user's folders, loose files, and everything in a
        run outside IMAGES/, L2/ and EMWIN/ are byte-for-byte as they were."""
        olds, fresh, bystanders = build_estate(self.tree, self.now)
        app = fx.make_app(self, self.tree)
        before = fx.snapshot(self.tree.root)
        pruner = fx.make_pruner(app, satdump=Limits(keep=5 * DAY))
        pruner.sweep_satdump()
        after = fx.snapshot(self.tree.root)
        self.assertOnlyRemoved(before, after, olds)
        for rel in fresh + bystanders:
            self.assertIn(rel, after)

    def test_scope_is_reported(self):
        """README: "You are told what is covered": the run folders the limits apply to
        and the items they never touch, in the log and on the Live page."""
        build_estate(self.tree, self.now)
        app = fx.make_app(self, self.tree)
        pruner = fx.make_pruner(app, satdump=Limits(keep=5 * DAY), protect=[OLD_RUN])
        self.assertEqual(pruner.scope["runs"], [RUN])
        self.assertEqual(pruner.scope["protected"], [OLD_RUN])
        self.assertIn("my goes pictures", pruner.scope["others"])
        self.assertIn("baseband_1694100000Hz.cf32", pruner.scope["others"])
        self.assertEqual(app.stats()["storage"]["scope"], pruner.scope)
        said = "\n".join(self.logs)
        self.assertIn(RUN, said)
        self.assertIn("--satdump-protect: " + OLD_RUN, said)
        self.assertIn("my goes pictures", said)

    def test_protected_run_is_never_deleted_from(self):
        """--satdump-protect FOLDER: never delete from this run folder."""
        olds, _, _ = build_estate(self.tree, self.now)
        app = fx.make_app(self, self.tree)
        before = fx.snapshot(self.tree.root)
        fx.make_pruner(app, satdump=Limits(keep=5 * DAY), protect=[OLD_RUN]).sweep_satdump()
        after = fx.snapshot(self.tree.root)
        self.assertOnlyRemoved(before, after, [rel for rel in olds if rel.startswith(RUN + "/")])
        self.assertTrue(any(rel.startswith(OLD_RUN + "/EMWIN/") for rel in after))

    def test_user_folder_is_reached_only_when_included(self):
        """A folder you named yourself is left alone even if it holds GOES images,
        unless you add it with --satdump-include."""
        olds, _, _ = build_estate(self.tree, self.now)
        app = fx.make_app(self, self.tree)
        before = fx.snapshot(self.tree.root)
        mine = sorted(rel for rel, kind in before.items()
                      if rel.startswith("my goes pictures/") and (kind != "dir" or fx.goesview.SLOT_RE.match(rel.split("/")[-1])))
        self.assertTrue(mine)
        fx.make_pruner(app, satdump=Limits(keep=5 * DAY), include=["my goes pictures"]).sweep_satdump()
        after = fx.snapshot(self.tree.root)
        gone = sorted(set(before) - set(after))
        self.assertTrue(any(rel.startswith("my goes pictures/IMAGES/") for rel in gone))
        self.assertTrue(any(rel.startswith("my goes pictures/EMWIN/") for rel in gone))
        # everything that went was in a GOES HRIT run or in the folder named by --satdump-include
        self.assertTrue(all(rel.startswith((RUN + "/", OLD_RUN + "/", "my goes pictures/")) for rel in gone))

    def test_root_that_is_one_run_folder(self):
        """Pointed straight at a run folder, retention works inside it and still
        leaves everything that is not a SatDump product."""
        t, old = self.tree, self.now - 40 * DAY
        doomed = [t.slot(old, run=""), t.bulletin(old, run=""), t.chart(old, run="")]
        t.slot(self.now - 600, run="")
        t.write("notes.txt")
        t.slot(old, run="backup")  # a folder beside IMAGES/ that happens to hold GOES images
        t.write("DCS/DCS_old.json")
        app = fx.make_app(self, t)
        before = fx.snapshot(t.root)
        pruner = fx.make_pruner(app, satdump=Limits(keep=5 * DAY))
        self.assertEqual(pruner.scope["runs"], ["satdump"])
        pruner.sweep_satdump()
        self.assertOnlyRemoved(before, fx.snapshot(t.root), doomed)

    def test_links_never_lead_out_of_the_root(self):
        """Neither a linked run folder, a linked IMAGES/ folder, nor a link inside one
        lets retention delete anything outside the data folder."""
        t, old = self.tree, self.now - 40 * DAY
        elsewhere = os.path.join(t.tmp, "elsewhere")
        for sub in ("run/IMAGES/GOES-18/Full Disk", "L2/GOES-18/Full Disk", "GOES-16/Full Disk"):
            folder = os.path.join(elsewhere, *sub.split("/"), fx.slot_name(old))
            os.makedirs(folder)
            with open(os.path.join(folder, fx.band_name(old)), "wb") as f:
                f.write(fx.png_bytes())
        doomed = [t.slot(old)]
        t.slot(self.now - 600)
        os.symlink(os.path.join(elsewhere, "run"), t.path("2025-01-01_00-00_goes_hrit_link"))
        os.symlink(os.path.join(elsewhere, "L2"), t.path(f"{RUN}/L2"))              # a whole anchor folder
        os.symlink(os.path.join(elsewhere, "GOES-16"), t.path(f"{RUN}/IMAGES/GOES-16"))  # a link inside one
        outside = fx.snapshot(elsewhere)
        app = fx.make_app(self, t)
        before = fx.snapshot(t.root)
        fx.make_pruner(app, satdump=Limits(keep=5 * DAY)).sweep_satdump()
        self.assertEqual(fx.snapshot(elsewhere), outside)
        self.assertOnlyRemoved(before, fx.snapshot(t.root), doomed)

    # Regression: with the root pointing straight at one run folder, goes_runs once returned that
    # run before looking at `protect`, so --satdump-protect naming it was ignored and its old data went.
    def test_protect_naming_the_run_the_root_points_at(self):
        t = self.tree
        t.slot(self.now - 40 * DAY)
        t.slot(self.now - 600)
        app = fx.make_app(self, t, root=t.path(RUN))
        before = fx.snapshot(t.root)
        fx.make_pruner(app, satdump=Limits(keep=5 * DAY), protect=[RUN]).sweep_satdump()
        self.assertEqual(fx.snapshot(t.root), before)

    # Regression: the walk skipped links it met inside IMAGES/, L2/ and EMWIN/, but followed one of
    # those three folders when it was itself a link. Leading to another folder under the root, it passed
    # the "inside the data folder" check and that folder's old data was deleted.
    def test_a_linked_anchor_folder_does_not_reach_another_folder_under_the_root(self):
        """A folder you made yourself is left alone, even if a run's L2/ is a link to it."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(self.now - 600)
        t.slot(old, run="my goes pictures", group="L2/GOES-18/Full Disk")
        os.symlink(t.path("my goes pictures/L2"), t.path(f"{RUN}/L2"))
        app = fx.make_app(self, t)
        before = fx.snapshot(t.root)
        pruner = fx.make_pruner(app, satdump=Limits(keep=5 * DAY))
        self.assertEqual(pruner.scope["others"], ["my goes pictures"])
        pruner.sweep_satdump()
        self.assertEqual(fx.snapshot(t.root), before)


class SatdumpInsideRunTests(fx.GoesviewCase):
    def sweep(self, **limits):
        app = fx.make_app(self, self.tree)
        before = fx.snapshot(self.tree.root)
        pruner = fx.make_pruner(app, satdump=Limits(**(limits or {"keep": 5 * DAY})))
        pruner.sweep_satdump()
        return pruner, before, fx.snapshot(self.tree.root)

    def test_every_kind_of_recognised_product_goes(self):
        """Timestamp folders under IMAGES/ and L2/, EMWIN files and NWS charts."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(self.now - 600)
        full = t.slot(old, extra=("abi_rgb_ABI_False_Color.png", "abi_rgb_ABI_False_Color_map.png"))
        t.write(full + "/product.cbor", b"\xa0")
        doomed = [
            full,
            t.slot(old, group=fx.MESO),
            t.slot(old, group="IMAGES/GOES-19/Full Disk", sat="G19"),
            t.slot(old, group="IMAGES/Himawari/Full Disk", sat="HIM", bands=(3, 13)),
            t.slot(old, group="L2/GOES-18/Full Disk", bands=("ACHA", "SST")),
            t.bulletin(old),
            t.bulletin(old, ident="RADNTHES", ext="GIF", text=b"GIF89a"),
            t.chart(old),
        ]
        _, before, after = self.sweep()
        self.assertOnlyRemoved(before, after, doomed)

    def test_unrecognised_things_beside_the_products_stay(self):
        """Inside a run, only recognised SatDump products go."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(self.now - 600)
        doomed = [t.slot(old), t.bulletin(old), t.chart(old)]
        kept = [
            t.write(f"{RUN}/{FULL_DISK}/README.txt", mtime=old),
            t.png(f"{RUN}/{FULL_DISK}/my-crop.png", mtime=old),
            t.png(f"{RUN}/{FULL_DISK}/favourites/{fx.band_name(old)}", mtime=old),   # a band, but not in a scan folder
            t.png(f"{RUN}/IMAGES/.Trash-1000/{fx.slot_name(old)}/{fx.band_name(old)}", mtime=old),
            t.write(f"{RUN}/EMWIN/notes.txt", mtime=old),
            t.write(f"{RUN}/EMWIN/.directory", mtime=old),
            t.png(f"{RUN}/IMAGES/NWS/sketch.png", mtime=old),
            t.write(f"{RUN}/IMAGES/timelapse.mp4", mtime=old),
        ]
        pruner, before, after = self.sweep()
        self.assertOnlyRemoved(before, after, doomed)
        for rel in kept:
            self.assertIn(rel, after)
        self.assertGreater(pruner.scope["skipped"], 0)

    def test_timestamp_folder_holding_a_foreign_file_is_left_whole(self):
        """README: "A timestamp folder that contains anything SatDump did not write
        is left whole": SatDump's own files in it stay too."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(self.now - 600)
        with_notes = t.slot(old)
        t.write(with_notes + "/my notes.txt")
        with_folder = t.slot(old + 600)
        t.png(with_folder + "/crops/detail.png")
        with_link = t.slot(old + 1200)
        os.symlink(t.path(f"{with_notes}/{fx.band_name(old)}"), t.path(f"{with_link}/{fx.band_name(old + 1200, 7)}"))
        clean = t.slot(old + 1800)
        pruner, before, after = self.sweep()
        self.assertOnlyRemoved(before, after, [clean])
        self.assertIn(f"{with_notes}/{fx.band_name(old, 2)}", after)
        self.assertEqual(pruner.scope["skipped"], 3)
        self.assertTrue(any("leaving 3 unrecognised items" in line for line in self.logs), self.logs)

    def test_same_holds_under_a_size_limit(self):
        """A size limit that nothing can satisfy still only takes recognised products."""
        t, old = self.tree, self.now - 40 * DAY
        t.slot(self.now - 600)
        mixed = t.slot(old)
        t.write(mixed + "/my notes.txt")
        doomed = [t.slot(old + 600), t.bulletin(old)]
        t.write(f"{RUN}/EMWIN/notes.txt")
        _, before, after = self.sweep(max_size=1)
        self.assertOnlyRemoved(before, after, doomed)


# --------------------------------------------------------- how the limits behave

class SatdumpRulesTests(fx.GoesviewCase):
    def slots(self, *ages, base=None):
        """One scan per age (seconds before `base`, default now)."""
        base = self.now if base is None else base
        return [self.tree.slot(base - age) for age in ages]

    def sweep(self, dry_run=False, **limits):
        self.app = fx.make_app(self, self.tree)
        self.before = fx.snapshot(self.tree.root)
        self.pruner = fx.make_pruner(self.app, satdump=Limits(**limits), dry_run=dry_run)
        self.pruner.sweep_satdump()
        return fx.snapshot(self.tree.root)

    def test_keep_removes_what_is_older(self):
        a, b, c, d = self.slots(40 * DAY, 31 * DAY, 29 * DAY, 600)
        after = self.sweep(keep=30 * DAY)
        self.assertOnlyRemoved(self.before, after, [a, b])
        self.assertFalse(self.pruner.last["satdump"]["dry"])
        self.assertIn("2 folders", self.pruner.last["satdump"]["what"])

    def test_dry_run_deletes_nothing(self):
        """--prune-dry-run logs what it would delete and deletes nothing."""
        self.slots(40 * DAY, 31 * DAY, 600)
        self.tree.bulletin(self.now - 40 * DAY)
        after = self.sweep(dry_run=True, keep=30 * DAY, max_size=1)
        self.assertEqual(after, self.before)
        last = self.pruner.last["satdump"]
        self.assertTrue(last["dry"])
        self.assertTrue(any("dry run" in line and "would remove" in line for line in self.logs), self.logs)
        # and the catalog still lists everything
        self.assertEqual(len(self.app.catalog.items), 7)

    def test_nothing_newer_than_three_hours_goes_under_a_size_limit(self):
        """README: "Data from the last 3 hours is never deleted, whatever the limits
        say. If a limit cannot be met without that, it logs a warning instead." """
        a, b, c, d = self.slots(10 * DAY, 5 * HOUR, 2 * HOUR, 600)
        recent_text = self.tree.bulletin(self.now - HOUR)
        old_text = self.tree.bulletin(self.now - 6 * HOUR)
        after = self.sweep(max_size=1)
        self.assertOnlyRemoved(self.before, after, [a, b, old_text])
        self.assertIn(recent_text, after)
        self.assertTrue(any("prune SatDump data: still" in line for line in self.logs), self.logs)

    def test_nothing_newer_than_three_hours_goes_under_a_disk_cap(self):
        a, b, c, d = self.slots(10 * DAY, 5 * HOUR, 2 * HOUR, 600)
        with disk_over_cap_by(2 ** 38):
            after = self.sweep(disk_cap=0.5)
        self.assertOnlyRemoved(self.before, after, [a, b])
        self.assertTrue(any("cap" in line and "cannot free more" in line for line in self.logs), self.logs)

    def test_ages_count_back_from_the_newest_data_not_the_clock(self):
        """After a long outage (or with a clock set far ahead) the archive must not
        all look old: 30 days are kept counting from the last thing received."""
        newest = self.now - 400 * DAY
        a, b, c, d = self.slots(40 * DAY, 20 * DAY, 2 * DAY, 0, base=newest)
        after = self.sweep(keep=30 * DAY)
        self.assertOnlyRemoved(self.before, after, [a])

    def test_three_hour_floor_also_counts_from_the_newest_data(self):
        """The same after an outage for the size rule: the last 3 hours received stay."""
        newest = self.now - 400 * DAY
        a, b, c, d = self.slots(10 * DAY, 5 * HOUR, 2 * HOUR, 0, base=newest)
        after = self.sweep(max_size=1)
        self.assertOnlyRemoved(self.before, after, [a, b])

    def test_a_folder_dated_in_the_future_does_not_age_everything_else(self):
        """One folder with a garbled future date cannot make the whole archive look old."""
        self.slots(2 * DAY, DAY, 600, -5 * 365 * DAY)
        after = self.sweep(keep=5 * DAY)
        self.assertEqual(after, self.before)

    def test_size_limit_removes_the_oldest_first_and_only_enough(self):
        """Deletion is always oldest first."""
        a, b, c, d = self.slots(40 * DAY, 30 * DAY, 20 * DAY, 600)
        sizes = [goesview.tree_size(self.tree.path(rel)) for rel in (a, b, c, d)]
        self.assertTrue(all(size > 0 for size in sizes))
        # room for everything but one byte: only the oldest scan has to go
        after = self.sweep(max_size=sum(sizes) - 1)
        self.assertOnlyRemoved(self.before, after, [a])
        # room for the newest two only
        self.pruner.satdump = Limits(max_size=sizes[2] + sizes[3])
        self.pruner.sweep_satdump()
        self.assertOnlyRemoved(self.before, fx.snapshot(self.tree.root), [a, b])

    def test_size_limit_that_is_met_removes_nothing(self):
        self.slots(40 * DAY, 600)
        after = self.sweep(max_size=10 * 1024 ** 3)
        self.assertEqual(after, self.before)
        self.assertIsNone(self.pruner.last["satdump"])

    def test_disk_cap_frees_only_what_is_needed(self):
        """Over the cap by one byte: the single oldest unit goes. Under it: nothing."""
        a, b, c, d = self.slots(40 * DAY, 30 * DAY, 20 * DAY, 600)
        with disk_over_cap_by(-1):
            after = self.sweep(disk_cap=0.5)
        self.assertEqual(after, self.before)
        with disk_over_cap_by(1):
            self.pruner.sweep_satdump()
        self.assertOnlyRemoved(self.before, fx.snapshot(self.tree.root), [a])

    def test_limits_combine(self):
        """README: "--satdump-keep 3mo --satdump-disk-cap 90% keeps three months
        unless the disk gets tight, then trims further." """
        a, b, c, d = self.slots(40 * DAY, 20 * DAY, 10 * DAY, 600)
        size_a = goesview.tree_size(self.tree.path(a))
        with disk_over_cap_by(-1):                       # disk fine: only the age rule applies
            after = self.sweep(keep=30 * DAY, disk_cap=0.5)
        self.assertOnlyRemoved(self.before, after, [a])
        a2 = self.tree.slot(self.now - 41 * DAY)
        self.before = fx.snapshot(self.tree.root)
        with disk_over_cap_by(goesview.tree_size(self.tree.path(a2)) + 1):   # tight: one more than age alone frees
            self.pruner.sweep_satdump()
        self.assertOnlyRemoved(self.before, fx.snapshot(self.tree.root), [a2, b])
        self.assertGreater(size_a, 0)

    def test_empty_runs_are_a_no_op(self):
        self.tree.mkdir(f"{RUN}/IMAGES")
        after = self.sweep(keep=5 * DAY, max_size=1)
        self.assertEqual(after, self.before)

    def test_repeated_sweeps_are_stable(self):
        """A second sweep with nothing new finds nothing more to delete."""
        a, b, c = self.slots(40 * DAY, 2 * DAY, 600)
        after = self.sweep(keep=30 * DAY)
        self.pruner.sweep_satdump()
        self.pruner.sweep_satdump()
        self.assertEqual(fx.snapshot(self.tree.root), after)
        self.assertOnlyRemoved(self.before, after, [a])


# ---------------------------------------- what goes with deleted SatDump data

class SatdumpAftermathTests(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t = self.tree
        self.old, self.new = self.now - 40 * DAY, self.now - 600
        self.old_slot, self.new_slot = t.slot(self.old), t.slot(self.new)
        self.old_text, self.new_text = t.bulletin(self.old), t.bulletin(self.new)
        self.old_chart = t.chart(self.old)
        self.app = fx.make_app(self, t)
        self.catalog = self.app.catalog

    def fake_cache_files(self, vpath):
        """Put a file where each preview of `vpath` would be cached."""
        targets = self.app.previews.cached(vpath, os.stat(self.catalog.resolve(vpath)))
        for target in targets:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(b"jpeg")
        return targets

    def test_catalog_forgets_what_was_deleted(self):
        """Nothing in the viewer still points at a deleted file."""
        old_band = f"s/{self.old_slot}/{fx.band_name(self.old, 2)}"
        new_band = f"s/{self.new_slot}/{fx.band_name(self.new, 2)}"
        self.assertIn(old_band, self.catalog.by_path)
        events = self.catalog.subscribe()
        fx.make_pruner(self.app, satdump=Limits(keep=5 * DAY)).sweep_satdump()
        for vpath in (old_band, "s/" + self.old_text, "s/" + self.old_chart):
            self.assertNotIn(vpath, self.catalog.by_path)
        for vpath in (new_band, "s/" + self.new_text):
            self.assertIn(vpath, self.catalog.by_path)
        self.assertEqual(sorted(fx.paths(self.catalog.feed(limit=100)[0])),
                         sorted([new_band, new_band.replace("_2_", "_13_"), "s/" + self.new_text]))
        self.assertNotIn("weather/NWS charts/pac_sfc_analysis", self.catalog.types)  # its only frame went
        event, data, _ = events.get_nowait()
        self.assertEqual((event, data["what"]), ("pruned", "satdump"))

    def test_previews_of_deleted_images_go_with_them(self):
        """README: "The previews and colour frames made from deleted data go with it." """
        old_band = f"s/{self.old_slot}/{fx.band_name(self.old, 2)}"
        new_band = f"s/{self.new_slot}/{fx.band_name(self.new, 2)}"
        old_previews = self.fake_cache_files(old_band) + self.fake_cache_files("s/" + self.old_chart)
        new_previews = self.fake_cache_files(new_band)
        derived = self.catalog.derived_root
        old_frame = os.path.join(derived, *self.old_slot.split("/"), "False_Color_usradioguy.jpg")
        new_frame = os.path.join(derived, *self.new_slot.split("/"), "False_Color_usradioguy.jpg")
        for frame in (old_frame, new_frame):
            os.makedirs(os.path.dirname(frame))
            with open(frame, "wb") as f:
                f.write(b"jpeg")
        fx.make_pruner(self.app, satdump=Limits(keep=5 * DAY)).sweep_satdump()
        for path in old_previews + [old_frame]:
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(os.path.exists(os.path.dirname(old_frame)))
        for path in new_previews + [new_frame]:
            self.assertTrue(os.path.exists(path), path)

    def test_text_index_is_told(self):
        """Deleted bulletins are dropped from the search index."""
        self.app.text_index = mock.Mock()
        fx.make_pruner(self.app, satdump=Limits(keep=5 * DAY)).sweep_satdump()
        (dead,), _ = self.app.text_index.forget.call_args
        self.assertIn("s/" + self.old_text, dead)
        self.assertNotIn("s/" + self.new_text, dead)

    def test_dry_run_leaves_cache_and_catalog_alone_too(self):
        old_band = f"s/{self.old_slot}/{fx.band_name(self.old, 2)}"
        previews = self.fake_cache_files(old_band)
        self.app.text_index = mock.Mock()
        events = self.catalog.subscribe()
        fx.make_pruner(self.app, satdump=Limits(keep=5 * DAY), dry_run=True).sweep_satdump()
        self.assertIn(old_band, self.catalog.by_path)
        self.assertTrue(all(os.path.exists(p) for p in previews))
        self.app.text_index.forget.assert_not_called()
        self.assertTrue(events.empty())


class ArchiveZipRetentionTests(fx.GoesviewCase):
    """README, Bulletin archive: "Retention covers them. The --satdump-* limits
    treat each day's zip as one unit." """

    def test_day_archives_are_units_by_their_day(self):
        import zipfile
        t = self.tree
        t.slot(self.now - 600)
        old_day, new_day = fx.day_name(self.now - 40 * DAY), fx.day_name(self.now - 2 * DAY)
        for day, ts in ((old_day, self.now - 40 * DAY), (new_day, self.now - 2 * DAY)):
            t.mkdir(f"{RUN}/EMWIN-archive")
            with zipfile.ZipFile(t.path(f"{RUN}/EMWIN-archive/{day}.zip"), "w") as zf:
                zf.writestr(fx.emwin_name(ts, serial=1), "one")
                zf.writestr(fx.emwin_name(ts, serial=2), "two")
        t.write(f"{RUN}/EMWIN-archive/my-backup.zip", b"PK")           # not a day archive: not SatDump retention's
        t.write(f"{RUN}/EMWIN-archive/notes.txt")
        app = fx.make_app(self, t)
        old_members = app.catalog.members(f"z/{RUN}/EMWIN-archive/{old_day}.zip/")
        self.assertEqual(len(old_members), 2)
        self.assertEqual(app.catalog.read_bytes(old_members[0]), b"one")  # opens the archive, as a reader would
        before = fx.snapshot(t.root)
        fx.make_pruner(app, satdump=Limits(keep=5 * DAY)).sweep_satdump()
        self.assertOnlyRemoved(before, fx.snapshot(t.root), [f"{RUN}/EMWIN-archive/{old_day}.zip"])
        self.assertEqual(app.catalog.members(f"z/{RUN}/EMWIN-archive/{old_day}.zip/"), [])
        self.assertEqual(len(app.catalog.members(f"z/{RUN}/EMWIN-archive/{new_day}.zip/")), 2)
        self.assertIsNone(app.catalog.read_bytes(old_members[0]))


# ------------------------------------------------------------- GOESview's own cache

class CacheRetentionTests(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        build_estate(self.tree, self.now - 400 * DAY)
        self.app = fx.make_app(self, self.tree)
        self.derived = self.app.catalog.derived_root
        self.previews = self.app.previews.folder
        self.root_before = fx.snapshot(self.tree.root)

    def cache_file(self, *parts, age=0.0, data=b"jpeg" * 64):
        path = os.path.join(self.tree.cache, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        when = time.time() - age
        os.utime(path, (when, when))
        return os.path.relpath(path, self.tree.cache).replace(os.sep, "/")

    def frame(self, age):
        """A colour frame GOESview built for the scan `age` seconds ago."""
        return os.path.dirname(self.cache_file("derived", RUN, "IMAGES", "GOES-18", "Full Disk",
                                               fx.slot_name(self.now - age), "False_Color_usradioguy.jpg"))

    def preview(self, name, age, size="320"):
        return self.cache_file("previews", size, name[:2], name[2:] + ".jpg", age=age)

    def sweep(self, dry_run=False, **limits):
        self.pruner = fx.make_pruner(self.app, cache=Limits(**limits), dry_run=dry_run)
        before = fx.snapshot(self.tree.cache)
        self.pruner.sweep_cache()
        return before, fx.snapshot(self.tree.cache)

    def test_old_previews_and_frames_go_recent_ones_stay(self):
        """By default GOESview deletes cached files older than 30 days."""
        doomed = [self.frame(40 * DAY), self.preview("aa01", 40 * DAY), self.preview("bb02", 31 * DAY, "1920")]
        kept = [self.frame(2 * DAY), self.preview("cc03", 29 * DAY), self.preview("dd04", 60)]
        before, after = self.sweep(keep=30 * DAY)
        self.assertOnlyRemoved(before, after, doomed)
        for rel in kept:
            self.assertIn(rel, after)
        self.assertIn("1 colour frame folder and 2 previews", self.pruner.last["cache"]["what"])

    def test_cache_sweep_never_touches_satdump_data(self):
        """Cache retention only deletes under the cache folder: a year-old SatDump
        archive beside it is untouched."""
        self.frame(40 * DAY)
        self.preview("aa01", 40 * DAY)
        self.sweep(keep=3 * HOUR, max_size=1)
        self.assertEqual(fx.snapshot(self.tree.root), self.root_before)

    def test_other_files_in_the_cache_folder_survive(self):
        """Only previews (*.jpg under previews/) and colour-frame folders count as cache
        units: the search index, DCS and monitor databases live there too."""
        doomed = [self.preview("aa01", 40 * DAY)]
        self.cache_file("text-index-2.sqlite3", age=400 * DAY)
        self.cache_file("dcs-2.sqlite3", age=400 * DAY)
        self.cache_file("monitor-1.sqlite3", age=400 * DAY)
        self.cache_file("previews", "320", "aa", "render.12345.tmp", age=400 * DAY)
        self.cache_file("previews", "notes.txt", age=400 * DAY)
        self.cache_file("derived", "notes", "readme.jpg", age=400 * DAY)
        before, after = self.sweep(keep=30 * DAY)
        self.assertOnlyRemoved(before, after, doomed)

    def test_dry_run_deletes_nothing(self):
        self.frame(40 * DAY)
        self.preview("aa01", 40 * DAY)
        before, after = self.sweep(dry_run=True, keep=30 * DAY)
        self.assertEqual(after, before)
        self.assertTrue(self.pruner.last["cache"]["dry"])

    def test_no_cache_limit_keeps_everything(self):
        """--cache-keep off: the cache is kept forever."""
        self.frame(400 * DAY)
        self.preview("aa01", 400 * DAY)
        before, after = self.sweep()
        self.assertEqual(after, before)
        self.assertIsNone(self.pruner.last["cache"])

    def test_size_limit_removes_oldest_first(self):
        oldest = self.preview("aa01", 10 * DAY)
        older = self.preview("bb02", 5 * DAY)
        newer = self.preview("cc03", DAY)
        size = goesview.on_disk(os.lstat(os.path.join(self.tree.cache, newer)))
        before, after = self.sweep(max_size=2 * size)
        self.assertOnlyRemoved(before, after, [oldest])
        self.assertIn(older, after)

    def test_size_limit_spares_the_last_three_hours(self):
        old = self.preview("aa01", 10 * DAY)
        self.preview("bb02", 2 * HOUR)
        self.preview("cc03", 60)
        before, after = self.sweep(max_size=1)
        self.assertOnlyRemoved(before, after, [old])

    def test_disk_cap_trims_the_cache(self):
        """--cache-disk-cap: when the disk is fuller than this, the oldest cached files go."""
        oldest = self.preview("aa01", 10 * DAY)
        self.preview("bb02", 5 * DAY)
        with disk_over_cap_by(-1):
            before, after = self.sweep(disk_cap=0.5)
        self.assertEqual(after, before)
        with disk_over_cap_by(1):  # over the cap, the hourly pause does not apply
            self.pruner.sweep_cache()
        self.assertOnlyRemoved(before, fx.snapshot(self.tree.cache), [oldest])

    def test_a_link_in_the_cache_does_not_reach_outside_it(self):
        """A preview that is really a link to a file elsewhere: the target is not deleted."""
        victim = self.tree.path(f"{RUN}/IMAGES/keep-me.jpg")
        self.tree.write(f"{RUN}/IMAGES/keep-me.jpg", b"precious", mtime=time.time() - 400 * DAY)
        link = os.path.join(self.previews, "320", "zz", "link.jpg")
        os.makedirs(os.path.dirname(link))
        os.symlink(victim, link)
        os.utime(link, (time.time() - 400 * DAY,) * 2, follow_symlinks=False)
        self.preview("aa01", 40 * DAY)
        self.sweep(keep=30 * DAY)
        with open(victim, "rb") as f:
            self.assertEqual(f.read(), b"precious")

    def test_previews_folder_outside_the_cache_is_not_deleted_from(self):
        """Whatever the Previews object points at, nothing outside the cache folder goes."""
        elsewhere = os.path.join(self.tree.tmp, "elsewhere")
        stray = os.path.join(elsewhere, "320", "aa", "old.jpg")
        os.makedirs(os.path.dirname(stray))
        with open(stray, "wb") as f:
            f.write(b"jpeg")
        os.utime(stray, (time.time() - 400 * DAY,) * 2)
        self.app.previews.folder = elsewhere
        self.sweep(keep=30 * DAY)
        self.assertTrue(os.path.exists(stray))

    def test_catalog_forgets_pruned_colour_frames(self):
        rel = self.frame(40 * DAY)                       # "derived/<run>/.../<slot>"
        vpath = "d/" + rel[len("derived/"):] + "/False_Color_usradioguy.jpg"
        spec = goesview.Spec("imagery", "GOES-18 · Full Disk", "False Color · usradioguy", "image", 0.0)
        when = self.now - 40 * DAY
        self.assertIsNotNone(self.app.catalog.add_derived(vpath, spec, when, when, 256, None, live=False))
        events = self.app.catalog.subscribe()
        self.sweep(keep=30 * DAY)
        self.assertNotIn(vpath, self.app.catalog.by_path)
        event, data, _ = events.get_nowait()
        self.assertEqual((event, data["what"]), ("pruned", "cache"))
        self.assertEqual(fx.snapshot(self.tree.root), self.root_before)


if __name__ == "__main__":
    unittest.main()
