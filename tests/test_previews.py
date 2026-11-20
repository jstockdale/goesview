"""Previews: the small cached JPEG copies that let 5424x5424 frames load at once.

Previews.get() is called directly; the warm-up loop (warm_forever) is never run.
Rendering needs Pillow; without it those tests are skipped, and one test checks
that GOESview then simply offers no previews.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import HOUR  # noqa: E402

goesview = fx.goesview
Image = goesview.Image


class PreviewsCase(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        self.ts = self.now - HOUR
        self.slot = self.tree.slot(self.ts, bands=(13,))
        self.small = f"s/{self.slot}/{fx.band_name(self.ts, 13)}"

    def start(self):
        self.app = fx.make_app(self, self.tree)
        self.previews = self.app.previews
        return self.previews

    def picture(self, name, image):
        rel = f"{self.slot}/{name}"
        image.save(self.tree.path(rel))
        os.utime(self.tree.path(rel), (self.ts + 30, self.ts + 30))
        return "s/" + rel


class WithoutPillowTests(PreviewsCase):
    def test_no_pillow_means_no_previews_not_an_error(self):
        """README: Pillow is for previews; without it the originals are still served."""
        previews = self.start()
        with mock.patch.object(goesview, "Image", None):
            self.assertIsNone(previews.get(self.small, 320))
        self.assertFalse(os.path.exists(previews.folder))

    def test_only_images_are_queued_for_warming_and_live_ones_first(self):
        self.tree.bulletin(self.ts)
        app = fx.make_app(self, self.tree, scan=False)
        app.catalog.listeners.append(app.previews.enqueue)        # as main() wires it
        self.tree.age_dirs()
        app.catalog.scan()
        new = self.tree.slot(self.now - 300, bands=(2,))
        self.tree.bulletin(self.now - 200)
        app.catalog.poll()
        jobs = []
        while not app.previews.jobs.empty():
            jobs.append(app.previews.jobs.get_nowait())
        self.assertEqual([(priority, vpath) for priority, _, vpath in jobs],
                         [(0, f"s/{new}/{fx.band_name(self.now - 300)}"), (1, self.small)])

    def test_cached_paths_depend_on_the_source_files_state(self):
        """A source file that changes gets new previews instead of stale ones."""
        previews = self.start()
        source = self.tree.path(self.small[2:])
        first = previews.cached(self.small, os.stat(source))
        self.assertEqual(len(first), len(previews.SIZES))
        self.assertTrue(all(path.startswith(previews.folder + os.sep) and path.endswith(".jpg") for path in first))
        self.assertEqual(previews.cached(self.small, os.stat(source)), first)
        os.utime(source, (self.ts + 99, self.ts + 99))
        self.assertTrue(set(previews.cached(self.small, os.stat(source))).isdisjoint(first))


@unittest.skipUnless(fx.HAVE_PIL, "needs Pillow")
class RenderTests(PreviewsCase):
    def size_of(self, path):
        with Image.open(path) as image:
            return image.format, image.mode, image.size

    def test_preview_is_a_downscaled_jpeg_in_the_cache(self):
        vpath = self.picture(fx.band_name(self.ts, 2), Image.new("L", (1000, 600), 140))
        previews = self.start()
        path = previews.get(vpath, 320)
        self.assertTrue(path.startswith(os.path.join(previews.folder, "320") + os.sep))
        self.assertEqual(self.size_of(path), ("JPEG", "RGB", (320, 192)))
        self.assertIn(path, previews.cached(vpath, os.stat(self.tree.path(vpath[2:]))))

    def test_asking_for_a_size_also_makes_the_smaller_ones(self):
        vpath = self.picture(fx.band_name(self.ts, 2), Image.new("L", (2400, 1200), 140))
        previews = self.start()
        path = previews.get(vpath, 960)
        self.assertEqual(self.size_of(path)[2], (960, 480))
        made = sorted(os.listdir(previews.folder))
        self.assertEqual(made, ["320", "960"])
        small = previews.get(vpath, 320)
        self.assertEqual(self.size_of(small)[2], (320, 160))
        self.assertEqual(self.size_of(previews.get(vpath, 1920))[2], (1920, 960))

    def test_small_image_is_not_blown_up(self):
        previews = self.start()
        self.assertEqual(self.size_of(previews.get(self.small, 960))[2], (4, 4))

    def test_second_request_uses_the_cached_file(self):
        previews = self.start()
        path = previews.get(self.small, 320)
        os.utime(path, (1_000_000_000, 1_000_000_000))
        with mock.patch.object(previews, "_render", side_effect=AssertionError("rendered again")):
            self.assertEqual(previews.get(self.small, 320), path)
        self.assertEqual(os.stat(path).st_mtime, 1_000_000_000)

    def test_previews_never_touch_the_satdump_folder(self):
        vpath = self.picture(fx.band_name(self.ts, 2), Image.new("L", (1000, 600), 140))
        before = fx.snapshot(self.tree.root)
        previews = self.start()
        for size in previews.SIZES:
            previews.get(vpath, size)
            previews.get(self.small, size)
        self.assertEqual(fx.snapshot(self.tree.root), before)
        self.assertFalse([rel for rel in fx.snapshot(self.tree.cache) if rel.endswith(".tmp")])

    def test_transparent_pixels_become_black(self):
        """SatDump's PNGs encode space around the disk as transparent."""
        image = Image.new("RGBA", (40, 40), (255, 255, 255, 0))
        image.paste((200, 30, 30, 255), (0, 0, 20, 40))
        vpath = self.picture("abi_rgb_ABI_False_Color.png", image)
        previews = self.start()
        with Image.open(previews.get(vpath, 320)) as preview:
            left, right = preview.getpixel((5, 20)), preview.getpixel((35, 20))
        self.assertLess(max(right), 12)
        self.assertGreater(left[0], 180)
        self.assertLess(left[1], 60)

    def test_palette_image(self):
        """EMWIN graphics are palette GIFs and PNGs."""
        previews = self.start()
        vpath = self.picture(fx.band_name(self.ts, 5), Image.new("P", (30, 20), 3))
        self.assertEqual(self.size_of(previews.get(vpath, 320)), ("JPEG", "RGB", (30, 20)))

    def test_unsupported_sizes_and_missing_files(self):
        previews = self.start()
        self.assertIsNone(previews.get(self.small, 500))
        self.assertIsNone(previews.get(self.small, 0))
        self.assertIsNone(previews.get("s/no/such/image.png", 320))
        self.assertIsNone(previews.get("s/../outside.png", 320))
        self.assertIsNone(previews.get("x/whatever.png", 320))

    def test_undecodable_file_is_remembered_not_retried(self):
        """An undecodable file must not take the server down, or be decoded on every request."""
        bad = self.tree.write(f"{self.slot}/{fx.band_name(self.ts, 2)}", b"this is not a PNG", mtime=self.ts + 30)
        previews = self.start()
        self.assertIsNone(previews.get("s/" + bad, 320))
        self.assertTrue(any("preview failed" in line for line in self.logs))
        with mock.patch.object(previews, "_render", side_effect=AssertionError("tried again")):
            self.assertIsNone(previews.get("s/" + bad, 320))
        self.assertIsNotNone(previews.get(self.small, 320))

    def test_truncated_frame_still_gets_a_preview(self):
        """A frame cut short by a dropout: show what arrived of it."""
        whole = self.tree.path(f"{self.slot}/whole.png")
        noise = Image.frombytes("L", (256, 256), os.urandom(256 * 256))
        noise.save(whole)
        with open(whole, "rb") as f:
            data = f.read()
        cut = self.tree.write(f"{self.slot}/{fx.band_name(self.ts, 2)}", data[:len(data) * 2 // 3], mtime=self.ts + 30)
        previews = self.start()
        path = previews.get("s/" + cut, 320)
        self.assertIsNotNone(path)
        self.assertEqual(self.size_of(path)[2], (256, 256))


if __name__ == "__main__":
    unittest.main()
