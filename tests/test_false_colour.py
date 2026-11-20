"""Colourist: GOESview's own false-colour frames, built in the cache.

README: "False colour that survives split scans ... goesview pairs them back up
and builds the frame itself", and "It never writes inside the SatDump folder".

Colourist is a thread; build_next() does one round of its work and is called
directly here. Needs numpy and Pillow.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import FULL_DISK, HOUR  # noqa: E402

goesview = fx.goesview
np, Image = goesview.np, goesview.Image

FRAME_TYPE = "imagery/GOES-18 · Full Disk/False Color · testlut"


@unittest.skipUnless(fx.HAVE_FC, "needs numpy and Pillow")
class ColouristTests(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t, now = self.tree, self.now
        # a smooth table, so a JPEG of one flat colour comes back as nearly that colour
        rows, cols = np.mgrid[0:256, 0:256]
        lut = np.dstack([rows, cols, 255 - rows, np.full_like(rows, 255)]).astype(np.uint8)
        self.lut_path = os.path.join(t.tmp, "testlut.png")
        Image.fromarray(lut).save(self.lut_path)
        self.whole_ts, self.early_ts, self.late_ts = now - 40 * 60, now - 20 * 60, now - 10 * 60
        self.whole = self.scan_folder(self.whole_ts, {2: 60, 13: 90})          # both bands together
        self.early = self.scan_folder(self.early_ts, {2: 100})                 # band 2, filed under the previous scan
        self.late = self.scan_folder(self.late_ts, {13: 30, 7: 5}, size=(4, 4))  # the scan it belongs to

    def scan_folder(self, ts, bands, size=(16, 16), group=FULL_DISK):
        rel = self.tree.slot(ts, bands=(), group=group)
        for band, value in bands.items():
            path = self.tree.path(f"{rel}/{fx.band_name(ts, band)}")
            Image.new("L", size, value).save(path)
            os.utime(path, (ts + 30, ts + 30))
        return rel

    def start(self, max_age=72 * HOUR):
        self.app = fx.make_app(self, self.tree)
        self.catalog = self.app.catalog
        self.colourist = self.app.colourist = goesview.Colourist(self.catalog, "testlut", self.lut_path, None, max_age)
        return self.colourist

    def build_all(self):
        built = 0
        while self.colourist.build_next():
            built += 1
            self.assertLess(built, 20, "build_next never finishes")
        return built

    def frame(self, rel):
        return os.path.join(self.catalog.derived_root, *rel.split("/"), "False_Color_testlut.jpg")

    def test_split_scan_gets_a_frame_in_the_later_folder(self):
        self.start()
        self.assertEqual(self.build_all(), 2)
        vpath = f"d/{self.late}/False_Color_testlut.jpg"
        item = self.catalog.by_path[vpath]
        self.assertEqual(item.type.id, FRAME_TYPE)
        self.assertEqual(item.time, self.late_ts)
        clock = time.strftime("%H:%M", time.gmtime(self.early_ts))
        self.assertEqual(item.note, f"band 2 from the {clock} scan")     # the page says where the odd band came from
        self.assertTrue(os.path.isfile(self.frame(self.late)))
        self.assertFalse(os.path.exists(self.frame(self.early)), "no frame for the folder that only lent its band")
        self.assertNotIn(f"d/{self.early}/False_Color_testlut.jpg", self.catalog.by_path)

    def test_whole_scan_gets_a_frame_without_a_note(self):
        self.start()
        self.build_all()
        item = self.catalog.by_path[f"d/{self.whole}/False_Color_testlut.jpg"]
        self.assertEqual((item.type.id, item.time, item.note), (FRAME_TYPE, self.whole_ts, None))
        self.assertEqual(item.size, os.path.getsize(self.frame(self.whole)))

    def test_frames_are_written_to_the_cache_never_into_the_satdump_folder(self):
        before = fx.snapshot(self.tree.root)
        self.start()
        self.build_all()
        self.assertEqual(fx.snapshot(self.tree.root), before)
        made = sorted(rel for rel, kind in fx.snapshot(self.tree.cache).items() if kind != "dir")
        self.assertEqual(made, sorted(f"derived/{rel}/False_Color_testlut.jpg" for rel in (self.whole, self.late)))

    def test_colours_come_from_the_table(self):
        """Band 2 picks the row, band 13 the column; the coarser band is upscaled."""
        self.start()
        self.build_all()
        with Image.open(self.frame(self.late)) as image:
            self.assertEqual(image.size, (16, 16))
            pixels = np.asarray(image.convert("RGB")).astype(int)
        self.assertLessEqual(int(np.abs(pixels - np.array([100, 30, 155])).max()), 6)
        with Image.open(self.frame(self.whole)) as image:
            pixels = np.asarray(image.convert("RGB")).astype(int)
        self.assertLessEqual(int(np.abs(pixels - np.array([60, 90, 195])).max()), 6)

    def test_one_frame_a_round_newest_first(self):
        colourist = self.start()
        self.assertTrue(colourist.build_next())
        self.assertTrue(os.path.exists(self.frame(self.late)))
        self.assertFalse(os.path.exists(self.frame(self.whole)))
        self.assertTrue(colourist.build_next())
        self.assertTrue(os.path.exists(self.frame(self.whole)))
        self.assertFalse(colourist.build_next())

    def test_frame_sits_beside_its_bands_in_the_feed(self):
        """A frame "arrives" when the later of its two bands did."""
        self.start()
        self.build_all()
        item = self.catalog.by_path[f"d/{self.late}/False_Color_testlut.jpg"]
        self.assertEqual(item.recv, self.late_ts + 30)
        self.assertEqual(self.catalog.feed(limit=1)[0][0], item)
        whole = self.catalog.by_path[f"d/{self.whole}/False_Color_testlut.jpg"]
        self.assertEqual(whole.recv, self.whole_ts + 30)

    def test_only_a_fresh_frame_is_announced_to_open_pages(self):
        self.start()
        events = self.catalog.subscribe()
        self.build_all()
        announced = []
        while not events.empty():
            announced.append(events.get_nowait()[1]["p"])
        self.assertEqual(announced, [f"d/{self.late}/False_Color_testlut.jpg"])   # 10 minutes old; the other is 40

    def test_scans_older_than_colour_hours_are_not_built(self):
        """--colour-hours: "Build colour frames for slots this recent." """
        self.start(max_age=15 * 60)
        self.assertEqual(self.build_all(), 1)
        self.assertTrue(os.path.exists(self.frame(self.late)))
        self.assertFalse(os.path.exists(self.frame(self.whole)))

    def test_frames_built_earlier_are_found_again_not_rebuilt(self):
        self.start()
        self.build_all()
        for rel in (self.whole, self.late):
            os.utime(self.frame(rel), (1_000_000_000, 1_000_000_000))
        self.start(max_age=60)                                  # a restart, now with a short --colour-hours
        self.assertEqual(self.build_all(), 0)
        for rel in (self.whole, self.late):
            self.assertIn(f"d/{rel}/False_Color_testlut.jpg", self.catalog.by_path)
            self.assertEqual(os.stat(self.frame(rel)).st_mtime, 1_000_000_000)
        late = self.catalog.by_path[f"d/{self.late}/False_Color_testlut.jpg"]
        self.assertTrue(late.note.startswith("band 2 from the "))

    def test_unreadable_band_is_given_up_on_quietly(self):
        broken = self.scan_folder(self.now - 5 * 60, {2: 10})
        self.tree.write(f"{broken}/{fx.band_name(self.now - 5 * 60, 13)}", b"not a png at all", mtime=self.now - 270)
        self.start()
        self.assertEqual(self.build_all(), 2)
        self.assertNotIn(f"d/{broken}/False_Color_testlut.jpg", self.catalog.by_path)
        self.assertIn(f"d/{broken}/False_Color_testlut.jpg", self.colourist.failed)
        self.assertFalse(os.path.exists(self.frame(broken)))
        self.assertEqual(os.listdir(os.path.dirname(self.frame(self.late))), ["False_Color_testlut.jpg"])  # no .tmp left
        self.assertTrue(any("false colour failed" in line for line in self.logs))
        self.assertFalse(self.colourist.build_next())

    def test_mesoscale_bands_pair_only_within_ninety_seconds(self):
        near_a = self.scan_folder(self.now - 600, {2: 100}, group=fx.MESO)
        near_b = self.scan_folder(self.now - 540, {13: 30}, group=fx.MESO)
        far_a = self.scan_folder(self.now - 300, {2: 100}, group=fx.MESO)
        far_b = self.scan_folder(self.now - 180, {13: 30}, group=fx.MESO)
        self.start()
        self.build_all()
        self.assertIn(f"d/{near_b}/False_Color_testlut.jpg", self.catalog.by_path)
        for rel in (near_a, far_a, far_b):
            self.assertNotIn(f"d/{rel}/False_Color_testlut.jpg", self.catalog.by_path)

    def test_a_new_image_wakes_the_colourist(self):
        colourist = self.start()
        self.assertFalse(colourist.wake.is_set())
        self.tree.bulletin(self.now - 60)
        self.catalog.poll()
        self.assertFalse(colourist.wake.is_set(), "a bulletin is no reason to look for new frames")
        self.scan_folder(self.now - 120, {2: 100})
        self.catalog.poll()
        self.assertTrue(colourist.wake.is_set())


if __name__ == "__main__":
    unittest.main()
