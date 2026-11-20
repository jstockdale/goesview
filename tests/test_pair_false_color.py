"""pair_false_color.py: colour tables, pairing band 2 with band 13 across split
timestamp folders, and the command-line tool built on them.

README: "SatDump only builds its colour composite when band 2 and band 13 land
in the same timestamp folder, and the HRIT feed regularly files one of them
under the previous scan. goesview pairs them back up and builds the frame itself."

Needs numpy and Pillow; every test is skipped without them.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402  (puts the project folder on sys.path)

try:
    import numpy as np
    from PIL import Image
    import pair_false_color as fc
except ImportError:
    np = Image = fc = None

NEEDS = "needs numpy and Pillow"
WINDOW = timedelta(minutes=11)   # the tool's default --window, and what goesview uses for full disk


def slot(clock, *bands, extra=()):
    """A Slot for 2026-11-17 at HH:MM[:SS], holding one PNG per band."""
    clock = clock if clock.count(":") == 2 else clock + ":00"
    name = "2026-11-17_" + clock.replace(":", "-")
    stamp = "20261117T" + clock.replace(":", "") + "Z"
    return fc.Slot(name, [f"G18_{band}_{stamp}.png" for band in bands] + list(extra))


def clock(s):
    return s.name[11:16].replace("-", ":")


def names(pairs):
    """Pairs as (target, band 2 from, band 13 from), each an HH:MM."""
    return [(clock(target), clock(vis), clock(ir)) for target, vis, ir in pairs]


def orphans(unpaired):
    return [(clock(s), missing) for s, missing in unpaired]


def make_lut(path, alpha=128):
    """A table in which every cell says where it is: (row, column, a mix, alpha)."""
    rows, cols = np.mgrid[0:256, 0:256]
    lut = np.dstack([rows, cols, (rows * 7 + cols * 3) % 256, np.full_like(rows, alpha)]).astype(np.uint8)
    Image.fromarray(lut).save(path)
    return lut


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goesview-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


@unittest.skipUnless(fc, NEEDS)
class LutTableTests(TempDirCase):
    def test_band_2_picks_the_row_and_band_13_the_column(self):
        lut = make_lut(self.path("lut.png"))
        table = fc.lut_table(self.path("lut.png"), None)
        self.assertEqual(table.shape, (256, 256, 4))
        self.assertEqual(table.dtype, np.uint8)
        self.assertEqual(table[10, 200].tolist(), [10, 200, (70 + 600) % 256, 255])
        self.assertEqual(table[200, 10].tolist(), [200, 10, (1400 + 30) % 256, 255])
        self.assertTrue((table[..., :3][1:255] == lut[..., :3][1:255]).all())

    def test_result_is_opaque_except_for_space(self):
        """Whatever the table's own alpha, pixels are opaque; (255, 0) is how SatDump
        encodes space around the disk, and stays transparent black."""
        make_lut(self.path("lut.png"), alpha=128)
        table = fc.lut_table(self.path("lut.png"), None)
        self.assertEqual(table[255, 0].tolist(), [0, 0, 0, 0])
        alpha = table[..., 3].copy()
        alpha[255, 0] = 255
        self.assertTrue((alpha == 255).all())

    def test_band_2_goes_through_the_contrast_curve(self):
        lut = make_lut(self.path("lut.png"))
        curve = np.arange(255, -1, -1, dtype=np.uint8).reshape(1, 256)   # a curve that inverts band 2
        Image.fromarray(curve).save(self.path("curve.png"))
        table = fc.lut_table(self.path("lut.png"), self.path("curve.png"))
        for vis, ir in ((0, 0), (10, 200), (254, 7), (255, 255)):
            self.assertEqual(table[vis, ir].tolist()[:3], lut[255 - vis, ir].tolist()[:3])
        self.assertEqual(table[255, 0].tolist(), [0, 0, 0, 0])

    def test_table_without_alpha_is_accepted(self):
        lut = make_lut(self.path("rgba.png"))
        Image.fromarray(np.ascontiguousarray(lut[..., :3])).save(self.path("rgb.png"))
        table = fc.lut_table(self.path("rgb.png"), None)
        self.assertEqual(table[10, 200].tolist(), [10, 200, (70 + 600) % 256, 255])

    def test_table_of_the_wrong_size_is_refused(self):
        Image.new("RGBA", (256, 128)).save(self.path("short.png"))
        with self.assertRaises(ValueError) as caught:
            fc.lut_table(self.path("short.png"), None)
        self.assertIn("256x256", str(caught.exception))

    def test_colourise_looks_each_pixel_up(self):
        make_lut(self.path("lut.png"))
        table = fc.lut_table(self.path("lut.png"), None)
        vis = np.array([[0, 10], [200, 255]], dtype=np.uint8)
        ir = np.array([[0, 200], [10, 0]], dtype=np.uint8)
        out = fc.colourise(table, vis, ir)
        self.assertEqual(out.shape, (2, 2, 4))
        self.assertEqual(out[0, 1].tolist(), [10, 200, (70 + 600) % 256, 255])
        self.assertEqual(out[1, 0].tolist(), [200, 10, (1400 + 30) % 256, 255])
        self.assertEqual(out[1, 1].tolist(), [0, 0, 0, 0])   # space

    def test_find_lut_takes_a_file_path(self):
        make_lut(self.path("lut.png"))
        self.assertEqual(fc.find_lut(self.path("lut.png")), self.path("lut.png"))
        self.assertIsNone(fc.find_lut(self.path("missing.png")))
        self.assertIsNone(fc.find_lut("no-such-table"))


@unittest.skipUnless(fc, NEEDS)
class SlotTests(unittest.TestCase):
    def test_slot_knows_its_time_and_bands(self):
        s = slot("20:10:21", 2, 13, 7, extra=("abi_rgb_ABI_False_Color.png", "product.cbor", "G18_x_notes.txt"))
        self.assertEqual((s.time.hour, s.time.minute, s.time.second), (20, 10, 21))
        self.assertEqual(sorted(s.bands), [2, 7, 13])
        self.assertEqual(s.bands[2], "G18_2_20261117T201021Z.png")
        self.assertIn("abi_rgb_ABI_False_Color.png", s.files)


@unittest.skipUnless(fc, NEEDS)
class FindPairsTests(unittest.TestCase):
    def test_both_bands_in_one_folder(self):
        pairs, unpaired = fc.find_pairs([slot("20:00", 2, 13), slot("20:10", 1, 2, 7, 13)], WINDOW)
        self.assertEqual(names(pairs), [("20:00", "20:00", "20:00"), ("20:10", "20:10", "20:10")])
        self.assertEqual(unpaired, [])

    def test_band_2_filed_under_the_previous_scan(self):
        """The split the README describes: the image belongs to the later folder,
        the scan the rest of the bands share."""
        early, late = slot("20:00", 2), slot("20:10", 1, 7, 13)
        pairs, unpaired = fc.find_pairs([early, late], WINDOW)
        self.assertEqual(len(pairs), 1)
        target, vis, ir = pairs[0]
        self.assertIs(target, late)
        self.assertIs(vis, early)
        self.assertIs(ir, late)
        self.assertEqual(unpaired, [])

    def test_band_13_filed_under_the_previous_scan(self):
        early, late = slot("20:00", 13), slot("20:10", 1, 2, 7)
        (target, vis, ir), = fc.find_pairs([early, late], WINDOW)[0]
        self.assertIs(target, late)
        self.assertIs(vis, late)
        self.assertIs(ir, early)

    def test_order_of_the_listing_does_not_matter(self):
        early, late = slot("20:00", 2), slot("20:10", 13)
        (target, vis, ir), = fc.find_pairs([late, early], WINDOW)[0]
        self.assertEqual((target, vis, ir), (late, early, late))

    def test_window_is_inclusive_and_no_wider(self):
        """Two bands exactly the window apart pair; a second more and they do not."""
        self.assertEqual(names(fc.find_pairs([slot("20:00", 2), slot("20:11", 13)], WINDOW)[0]),
                         [("20:11", "20:00", "20:11")])
        pairs, unpaired = fc.find_pairs([slot("20:00", 2), slot("20:11:01", 13)], WINDOW)
        self.assertEqual(pairs, [])
        self.assertEqual(orphans(unpaired), [("20:00", 13), ("20:11", 2)])

    def test_mesoscale_window(self):
        """goesview pairs mesoscale sectors (a scan a minute) within 90 seconds."""
        window = timedelta(seconds=90)
        self.assertEqual(len(fc.find_pairs([slot("20:00:00", 2), slot("20:01:00", 13)], window)[0]), 1)
        self.assertEqual(len(fc.find_pairs([slot("20:00:00", 2), slot("20:02:00", 13)], window)[0]), 0)

    def test_nearest_partner_wins(self):
        """With two candidates in the window, a lone band takes the nearer one."""
        pairs, unpaired = fc.find_pairs([slot("20:00", 2), slot("20:04", 13), slot("20:09", 13)], WINDOW)
        self.assertEqual(names(pairs), [("20:04", "20:00", "20:04")])
        self.assertEqual(orphans(unpaired), [("20:09", 2)])
        pairs, unpaired = fc.find_pairs([slot("20:00", 2), slot("20:09", 13), slot("20:04", 13)], WINDOW)
        self.assertEqual(names(pairs), [("20:04", "20:00", "20:04")])

    def test_a_folder_is_used_for_one_image_only(self):
        slots = [slot("20:00", 2), slot("20:05", 13), slot("20:08", 2)]
        pairs, unpaired = fc.find_pairs(slots, WINDOW)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(len(unpaired), 1)
        used = {pairs[0][1].name, pairs[0][2].name}
        self.assertNotIn(unpaired[0][0].name, used)
        self.assertEqual(unpaired[0][1], 13)

    def test_a_complete_folder_does_not_lend_its_bands(self):
        """A stray band 2 next to a complete scan stays an orphan: the complete scan
        keeps its own pair."""
        pairs, unpaired = fc.find_pairs([slot("20:00", 2), slot("20:10", 2, 13)], WINDOW)
        self.assertEqual(names(pairs), [("20:10", "20:10", "20:10")])
        self.assertEqual(orphans(unpaired), [("20:00", 13)])

    def test_folders_with_neither_band_are_ignored(self):
        pairs, unpaired = fc.find_pairs([slot("20:00", 1, 7, 9), slot("20:10")], WINDOW)
        self.assertEqual((pairs, unpaired), ([], []))

    def test_a_mixed_listing(self):
        """Whole scans, one split scan and one orphan in the same listing: an image
        for every scan that can have one, each folder used once."""
        slots = [slot("19:50", 2, 13), slot("20:00", 2), slot("20:10", 7, 13), slot("20:20", 1, 2, 13), slot("20:30", 13)]
        pairs, unpaired = fc.find_pairs(slots, WINDOW)
        self.assertEqual(names(pairs), [("19:50", "19:50", "19:50"), ("20:10", "20:00", "20:10"), ("20:20", "20:20", "20:20")])
        self.assertEqual(orphans(unpaired), [("20:30", 2)])


@unittest.skipUnless(fc, NEEDS)
class LoadPairTests(unittest.TestCase):
    def test_equal_sizes_pass_through(self):
        vis = Image.fromarray(np.arange(16, dtype=np.uint8).reshape(4, 4) * 10)
        ir = Image.fromarray(np.arange(16, dtype=np.uint8).reshape(4, 4)[::-1].copy())
        v, i = fc.load_pair(vis, ir)
        self.assertTrue((v == np.asarray(vis)).all())
        self.assertTrue((i == np.asarray(ir)).all())

    def test_coarser_band_13_is_upscaled_to_band_2(self):
        """On mesoscale sectors band 2 arrives at four times the resolution of band 13."""
        vis = Image.new("L", (8, 8), 200)
        ir = Image.new("L", (2, 2), 50)
        v, i = fc.load_pair(vis, ir)
        self.assertEqual((v.shape, i.shape), ((8, 8), (8, 8)))
        self.assertEqual((v.dtype, i.dtype), (np.uint8, np.uint8))
        self.assertTrue((v == 200).all())
        self.assertTrue((i == 50).all())

    def test_coarser_band_2_is_upscaled_too(self):
        v, i = fc.load_pair(Image.new("L", (3, 3), 9), Image.new("L", (12, 12), 77))
        self.assertEqual((v.shape, i.shape), ((12, 12), (12, 12)))
        self.assertTrue((v == 9).all())

    def test_upscaling_keeps_the_picture(self):
        ramp = np.repeat(np.array([[0, 80, 160, 240]], dtype=np.uint8), 4, axis=0)   # dark on the left, bright on the right
        v, i = fc.load_pair(Image.new("L", (16, 16), 1), Image.fromarray(ramp))
        self.assertEqual(i.shape, (16, 16))
        self.assertTrue((np.diff(i.astype(int), axis=1) >= 0).all(), "left-to-right ramp was scrambled")
        self.assertLessEqual(int(i[:, 0].max()), 40)
        self.assertGreaterEqual(int(i[:, -1].min()), 200)
        self.assertTrue((i == i[0]).all(), "rows of a horizontal ramp should be alike")

    def test_other_pixel_formats_become_8_bit_grey(self):
        v, i = fc.load_pair(Image.new("RGB", (4, 4), (90, 90, 90)), Image.new("RGBA", (4, 4), (30, 30, 30, 255)))
        self.assertEqual((v.shape, i.shape), ((4, 4), (4, 4)))
        self.assertTrue((v == 90).all())
        self.assertTrue((i == 30).all())


@unittest.skipUnless(fc, NEEDS)
class LearnTests(TempDirCase):
    def test_fill_unseen_takes_the_nearest_observed_colour(self):
        table = np.zeros((256, 256, 4), dtype=np.uint8)
        seen = np.zeros((256, 256), dtype=bool)
        table[0, 0], table[255, 255] = (10, 20, 30, 255), (200, 210, 220, 255)
        seen[0, 0] = seen[255, 255] = True
        filled = fc.fill_unseen(table, seen)
        self.assertEqual(filled[0, 0].tolist(), [10, 20, 30, 255])
        self.assertEqual(filled[5, 9].tolist(), [10, 20, 30, 255])
        self.assertEqual(filled[250, 240].tolist(), [200, 210, 220, 255])
        self.assertEqual(filled[100, 100].tolist(), [10, 20, 30, 255])
        self.assertEqual(filled[160, 160].tolist(), [200, 210, 220, 255])
        self.assertFalse(seen[5, 9], "the caller's mask must not be altered")
        self.assertEqual(table[5, 9].tolist(), [0, 0, 0, 0])

    def test_pick_samples_spreads_over_the_complete_slots(self):
        complete = [slot(f"20:{m:02d}", 2, 13, extra=(fc.SATDUMP_COLOR,)) for m in range(0, 50, 5)]
        others = [slot("21:00", 2, 13), slot("21:10", 2, extra=(fc.SATDUMP_COLOR,))]
        picked = fc.pick_samples(complete + others, 3)
        self.assertEqual(len(picked), 3)
        self.assertIs(picked[0], complete[0])
        self.assertIs(picked[-1], complete[-1])
        self.assertEqual(fc.pick_samples(complete + others, 50), complete)
        self.assertEqual(fc.pick_samples(complete + others, 1), [complete[0]])

    def test_learned_table_reproduces_the_colours_it_saw(self):
        """--lut learn: "copies whatever colours your SatDump already produces"."""
        make_lut(self.path("lut.png"))
        truth = fc.lut_table(self.path("lut.png"), None)
        rng = np.random.RandomState(7)
        s = slot("20:00", 2, 13, extra=(fc.SATDUMP_COLOR,))
        os.makedirs(self.path("fd", s.name))
        vis = rng.randint(0, 256, (48, 48)).astype(np.uint8)
        ir = rng.randint(0, 256, (48, 48)).astype(np.uint8)
        Image.fromarray(vis).save(self.path("fd", s.name, s.bands[2]))
        Image.fromarray(ir).save(self.path("fd", s.name, s.bands[13]))
        Image.fromarray(truth[vis, ir]).save(self.path("fd", s.name, fc.SATDUMP_COLOR))
        table, seen = fc.learn_table(fc.Source(self.path("fd")), [s], lambda msg: None)
        self.assertEqual(table.shape, (256, 256, 4))
        self.assertEqual(int(seen.sum()), len(set(zip(vis.ravel().tolist(), ir.ravel().tolist()))))
        self.assertTrue(seen[vis, ir].all())
        self.assertTrue((table[vis, ir] == truth[vis, ir]).all())


@unittest.skipUnless(fc, NEEDS)
class CommandLineTests(TempDirCase):
    """The tool run as the README shows, on a small "Full Disk" folder."""

    def setUp(self):
        super().setUp()
        self.table_path = self.path("mylut.png")
        make_lut(self.table_path)
        self.table = fc.lut_table(self.table_path, None)
        self.root = self.path("Full Disk")
        self.early, self.late, self.done, self.orphan = "2026-11-17_20-00-00", "2026-11-17_20-10-00", "2026-11-17_20-20-00", "2026-11-17_22-00-00"
        self.band(self.early, 2, 100, size=(8, 8))            # band 2, filed under the previous scan
        self.band(self.late, 13, 30, size=(2, 2))             # the scan it belongs to
        self.band(self.late, 7, 5)
        self.band(self.done, 2, 60)                           # a scan SatDump coloured itself
        self.band(self.done, 13, 90)
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(os.path.join(self.root, self.done, fc.SATDUMP_COLOR))
        self.band(self.orphan, 13, 40)                        # nothing near it
        os.makedirs(os.path.join(self.root, "not a scan"))

    def band(self, folder, band, value, size=(8, 8)):
        os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        stamp = folder.replace("-", "").replace("_", "T") + "Z"
        Image.new("L", size, value).save(os.path.join(self.root, folder, f"G18_{band}_{stamp}.png"))

    def run_tool(self, *args):
        argv = ["pair_false_color.py", self.root, "--lut", self.table_path, "--curve", "none", *args]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            fc.main()
        return out.getvalue()

    def output(self, folder, root=None, name="abi_rgb_ABI_False_Color_mylut.png"):
        return os.path.join(root or self.root, folder, name)

    def test_scan_lists_only_timestamp_folders(self):
        slots = fc.scan(fc.Source(self.root))
        self.assertEqual([s.name for s in slots], [self.early, self.late, self.done, self.orphan])
        self.assertEqual(sorted(slots[1].bands), [7, 13])

    def test_dry_run_reports_and_writes_nothing(self):
        before = fx.snapshot(self.root)
        said = self.run_tool("--dry-run")
        self.assertEqual(fx.snapshot(self.root), before)
        self.assertIn("4 folders, 1 colour images to build, 1 bands with no partner", said)
        self.assertIn(f"{self.late}: band 2 from {self.early}, band 13 from {self.late}", said)
        self.assertIn(f"no band 2 within 11 min of {self.orphan}", said)

    def test_builds_the_image_satdump_skipped_in_the_later_folder(self):
        before = fx.snapshot(self.root)
        self.run_tool()
        after = fx.snapshot(self.root)
        self.assertEqual(sorted(set(after) - set(before)), [f"{self.late}/abi_rgb_ABI_False_Color_mylut.png"])
        self.assertEqual({rel: after[rel] for rel in before}, before, "an existing file was changed")
        with Image.open(self.output(self.late)) as image:
            self.assertEqual(image.size, (8, 8))                 # band 13 was upscaled to band 2
            pixels = np.asarray(image.convert("RGBA"))
        self.assertTrue((pixels == self.table[100, 30]).all())

    def test_all_also_builds_slots_satdump_already_coloured(self):
        self.run_tool("--all")
        with Image.open(self.output(self.done)) as image:
            self.assertTrue((np.asarray(image.convert("RGBA")) == self.table[60, 90]).all())
        with Image.open(os.path.join(self.root, self.done, fc.SATDUMP_COLOR)) as image:
            self.assertEqual(image.getpixel((0, 0)), (1, 2, 3, 255), "SatDump's own image was overwritten")

    def test_existing_output_is_not_rebuilt_unless_forced(self):
        self.run_tool()
        os.utime(self.output(self.late), (1_000_000_000, 1_000_000_000))
        said = self.run_tool()
        self.assertIn("0 colour images to build", said)
        self.assertEqual(os.stat(self.output(self.late)).st_mtime, 1_000_000_000)
        self.run_tool("--force")
        self.assertGreater(os.stat(self.output(self.late)).st_mtime, 1_000_000_000)

    def test_out_writes_elsewhere_and_leaves_the_source_alone(self):
        before = fx.snapshot(self.root)
        out = self.path("repaired")
        self.run_tool("--out", out, "--name", "colour.png")
        self.assertEqual(fx.snapshot(self.root), before)
        self.assertEqual(sorted(fx.snapshot(out)), [self.late, f"{self.late}/colour.png"])

    def test_narrower_window_leaves_the_pair_unbuilt(self):
        said = self.run_tool("--window", "5")
        self.assertIn("0 colour images to build, 3 bands with no partner", said)
        self.assertFalse(os.path.exists(self.output(self.late)))

    def test_missing_colour_table_stops_before_writing(self):
        argv = ["pair_false_color.py", self.root, "--lut", self.path("missing.png"), "--curve", "none"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                fc.main()
        self.assertIn("Colour table not found", str(stopped.exception))
        self.assertFalse(os.path.exists(self.output(self.late, name="abi_rgb_ABI_False_Color_missing.png")))


if __name__ == "__main__":
    unittest.main()
