"""Finding new files without listing big folders: watch.py, and its use by the catalog and the DCS reader.

SatDump puts a day's bulletins (and a day's DCS files) into one flat folder each. Listing
tens of thousands of files every few seconds made GOESview's processor load climb all day
and drop back when the day was archived. These tests pin down the cure: a big folder is
listed once, new files are then heard of from the system, and the folder is listed again
only when what was heard cannot be trusted.

The watch itself is Linux inotify; those tests are skipped elsewhere. The fallbacks (no
watch, a watch that reports nothing) run everywhere.
"""
import os
import shutil
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
import test_dcs as td  # noqa: E402
from gv_fixtures import RUN  # noqa: E402

goesview = fx.goesview
dcs = goesview.dcs
watch = goesview.watch

CAN_WATCH = watch.start() is not None
needs_watch = unittest.skipUnless(CAN_WATCH, "this system cannot report new files (no inotify)")
EMWIN = f"{RUN}/EMWIN"


class Listings:
    """Counts the listings of each folder while it is in force (a `with` block)."""

    def __init__(self):
        self.seen = []
        self.real = os.scandir

    def __enter__(self):
        def scandir(path="."):
            self.seen.append(os.fspath(path))
            return self.real(path)
        self.patch = mock.patch.object(os, "scandir", scandir)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def of(self, path):
        return sum(1 for p in self.seen if p == path)


class Silent:
    """A watch that accepts every folder and never reports anything, as on a network drive
    that another machine writes to."""

    def __init__(self):
        self.folders = set()

    def add(self, path):
        self.folders.add(path)
        return True

    def drop(self, path):
        self.folders.discard(path)

    def watching(self, path):
        return path in self.folders

    def read(self):
        return {}, set(), False


class Overflowing(Silent):
    """A watch that reports having lost track, once."""

    def __init__(self):
        super().__init__()
        self.lost = True

    def read(self):
        lost, self.lost = self.lost, False
        return {}, set(), lost


class Budget(unittest.TestCase):
    def test_listings_take_at_most_their_share_of_a_processor(self):
        self.assertAlmostEqual(watch.budget(0.5), 10)         # half a second each: every ten seconds
        self.assertAlmostEqual(watch.budget(0.5, 0.01), 50)
        self.assertEqual(watch.budget(0.0), 0)
        self.assertEqual(watch.budget(0.01), 0)               # quick enough not to ration
        self.assertEqual(watch.budget(0.001, least=60), 60)

    def test_no_watch_off_linux(self):
        with mock.patch.object(watch.sys, "platform", "darwin"):
            self.assertIsNone(watch.start())


@needs_watch
class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(fx.tempfile.mkdtemp(prefix="goesview-watch-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.w = watch.start()
        self.addCleanup(self.w.close)

    def touch(self, *parts):
        path = os.path.join(self.tmp, *parts)
        with open(path, "w"):
            pass
        return path

    def test_new_files_are_reported_by_name_in_order(self):
        self.assertTrue(self.w.add(self.tmp))
        self.assertEqual(self.w.read(), ({}, set(), False))
        self.touch("a.txt")
        self.touch("b.txt")
        news, touched, lost = self.w.read()
        self.assertEqual(news, {self.tmp: ["a.txt", "b.txt"]})
        self.assertEqual(touched, {self.tmp})
        self.assertFalse(lost)
        self.assertEqual(self.w.read(), ({}, set(), False))   # each thing is reported once

    def test_a_file_renamed_into_place_is_reported_under_its_final_name(self):
        """How an archive is written: to a temporary name, then moved."""
        self.w.add(self.tmp)
        os.replace(self.touch("day.zip.tmp"), os.path.join(self.tmp, "day.zip"))
        self.assertEqual(self.w.read()[0], {self.tmp: ["day.zip.tmp", "day.zip"]})

    def test_a_new_folder_is_reported_too(self):
        self.w.add(self.tmp)
        os.mkdir(os.path.join(self.tmp, "2026-11-20_18-00-22"))
        self.assertEqual(self.w.read()[0], {self.tmp: ["2026-11-20_18-00-22"]})

    def test_a_deletion_counts_as_a_change_but_is_not_news(self):
        path = self.touch("a.txt")
        self.w.add(self.tmp)
        os.unlink(path)
        self.assertEqual(self.w.read(), ({}, {self.tmp}, False))

    def test_rewriting_a_file_is_not_a_change_to_the_folder(self):
        path = self.touch("a.txt")
        self.w.add(self.tmp)
        with open(path, "a") as f:
            f.write("more")
        self.assertEqual(self.w.read(), ({}, set(), False))

    def test_each_folder_is_kept_apart_and_subfolders_are_not_included(self):
        one, two = os.path.join(self.tmp, "one"), os.path.join(self.tmp, "two")
        os.mkdir(one), os.mkdir(two), os.mkdir(os.path.join(one, "deeper"))
        self.assertTrue(self.w.add(one) and self.w.add(two))
        self.touch("one", "a"), self.touch("two", "b"), self.touch("one", "deeper", "c")
        self.assertEqual(self.w.read()[0], {one: ["a"], two: ["b"]})

    def test_adding_twice_is_harmless_and_dropping_stops_the_reports(self):
        self.assertTrue(self.w.add(self.tmp) and self.w.add(self.tmp))
        self.assertTrue(self.w.watching(self.tmp))
        self.w.drop(self.tmp)
        self.w.drop(self.tmp)
        self.assertFalse(self.w.watching(self.tmp))
        self.touch("a.txt")
        self.assertEqual(self.w.read(), ({}, set(), False))

    def test_what_cannot_be_watched_is_refused(self):
        self.assertFalse(self.w.add(os.path.join(self.tmp, "missing")))
        self.assertFalse(self.w.add(self.touch("a-file")))   # only folders

    def test_a_folder_that_is_deleted_is_no_longer_watched(self):
        gone = os.path.join(self.tmp, "gone")
        os.mkdir(gone)
        self.w.add(gone)
        os.rmdir(gone)
        self.w.read()
        self.assertFalse(self.w.watching(gone))
        os.mkdir(gone)   # the same name again is a new folder: it can be watched afresh
        self.assertTrue(self.w.add(gone))
        self.touch("gone", "a")
        self.assertEqual(self.w.read()[0], {gone: ["a"]})

    def test_names_that_are_not_valid_text_survive(self):
        self.w.add(self.tmp)
        raw = os.path.join(os.fsencode(self.tmp), b"caf\xe9.txt")
        with open(raw, "w"):
            pass
        (name,) = self.w.read()[0][self.tmp]
        self.assertEqual(os.fsencode(name), b"caf\xe9.txt")

    def test_losing_track_is_owned_up_to(self):
        """The system queues only so many reports; past that it says so, and we must list."""
        with open("/proc/sys/fs/inotify/max_queued_events") as f:
            limit = int(f.read())
        if limit > 100_000:
            self.skipTest("the queue here is too long to fill in a test")
        self.w.add(self.tmp)
        for n in range(limit + 50):
            self.touch(f"{n}")
        news, _, lost = self.w.read()
        self.assertTrue(lost)
        self.assertLess(len(news[self.tmp]), limit + 50)
        self.touch("after")
        self.assertEqual(self.w.read(), ({self.tmp: ["after"]}, {self.tmp}, False))


class CatalogCase(fx.GoesviewCase):
    """A run whose EMWIN folder already counts as big (the threshold is lowered to 5 files)."""

    watcher = staticmethod(watch.start)

    def setUp(self):
        super().setUp()
        self.t0 = self.now - 3600
        self.old = [self.tree.bulletin(self.t0 + n, "AFDSEWWA") for n in range(8)]
        self.slot = self.tree.slot(self.t0)
        self.tree.write(f"{RUN}/DCS/pH-26324093253-A.dcs.json", b"{}")
        self.folder = self.tree.path(EMWIN)

    def catalog(self, settle=0.0, watcher="default", age=True):
        w = self.watcher() if watcher == "default" else watcher
        if hasattr(w, "close"):
            self.addCleanup(w.close)
        if age:
            self.tree.age_dirs()
        c = goesview.Catalog(self.tree.root, os.path.join(self.tree.cache, "derived"), settle=settle,
                             watcher=w, watch_from=5)
        c.scan()
        return c


@needs_watch
class WatchedCatalog(CatalogCase):
    def test_a_big_folder_is_watched_and_small_ones_are_not(self):
        c = self.catalog()
        self.assertTrue(c.dirs[self.folder].watched)
        self.assertTrue(c.watcher.watching(self.folder))
        self.assertEqual([p for p, s in c.dirs.items() if s.watched], [self.folder])
        self.assertEqual(len(c.items), 10)
        self.assertTrue(any("EMWIN holds 8 files" in line for line in self.logs))

    def test_new_bulletins_are_found_without_listing_the_folder(self):
        """The point of it all: however full the folder, a new file costs nothing to find."""
        c = self.catalog()
        events = c.subscribe()
        with Listings() as listings:
            found = []
            for n in range(5):
                found.append(self.tree.bulletin(self.t0 + 100 + n, "TORSEWWA"))
                c.poll()
            for _ in range(50):
                c.poll()
        self.assertEqual(listings.of(self.folder), 0)
        for rel in found:
            self.assertIn("s/" + rel, c.by_path)
        self.assertEqual(len(c.items), 15)
        self.assertEqual([events.get_nowait()[1]["p"] for _ in range(5)], ["s/" + rel for rel in found])
        self.assertTrue(events.empty())

    def test_arrival_time_and_size_are_the_same_as_from_a_listing(self):
        c = self.catalog()
        landed = time.time() - 20
        new = self.tree.bulletin(self.t0 + 200, "TORSEWWA", text=b"URGENT\r\n", mtime=landed)
        c.poll()
        item = c.by_path["s/" + new]
        self.assertEqual(item.time, self.t0 + 200)
        self.assertAlmostEqual(item.recv, landed, places=3)
        self.assertEqual(item.size, 8)

    def test_a_file_still_being_written_waits_and_is_then_found_without_a_listing(self):
        c = self.catalog(settle=4.0)
        new = self.tree.bulletin(self.t0 + 300, "TORSEWWA", mtime=time.time())
        with Listings() as listings:
            c.poll()
            self.assertNotIn("s/" + new, c.by_path)
            self.assertEqual(c.dirs[self.folder].waiting, {os.path.basename(new)})
            c.poll()
            self.assertNotIn("s/" + new, c.by_path)
            os.utime(self.tree.path(new), (time.time() - 30,) * 2)   # SatDump finished it a while ago
            c.poll()
        self.assertIn("s/" + new, c.by_path)
        self.assertEqual(c.dirs[self.folder].waiting, set())
        self.assertEqual(listings.of(self.folder), 0)

    def test_a_temporary_file_that_is_gone_again_is_not_waited_for(self):
        c = self.catalog(settle=4.0)
        path = self.tree.path(self.tree.bulletin(self.t0 + 310, "TORSEWWA", mtime=time.time()))
        os.unlink(path)
        c.poll()
        self.assertEqual(c.dirs[self.folder].waiting, set())
        self.assertEqual(len(c.items), 10)

    def test_a_new_folder_inside_a_watched_folder_is_read(self):
        scans = self.tree.path(f"{RUN}/{fx.FULL_DISK}")
        for n in range(1, 6):
            self.tree.slot(self.t0 + 600 * n)
        c = self.catalog()
        self.assertTrue(c.dirs[scans].watched)
        new = self.tree.slot(self.now - 300, bands=(13,))
        with Listings() as listings:
            c.poll()
        self.assertIn(f"s/{new}/{fx.band_name(self.now - 300, 13)}", c.by_path)
        self.assertEqual(listings.of(scans), 0)
        self.assertEqual(listings.of(self.tree.path(new)), 1)

    def test_deletions_do_not_cause_listings(self):
        """The archiver empties the folder once a day: thousands of deletions, all reported."""
        c = self.catalog()
        with Listings() as listings:
            for rel in self.old[:6]:
                os.unlink(self.tree.path(rel))
                c.poll()
                c.poll()
            new = self.tree.bulletin(self.t0 + 400, "TORSEWWA")
            c.poll()
        self.assertEqual(listings.of(self.folder), 0)
        self.assertIn("s/" + new, c.by_path)

    def test_the_same_files_are_found_with_and_without_a_watch(self):
        watched, listed = self.catalog(), self.catalog(watcher=None)
        for step in range(6):
            self.tree.bulletin(self.t0 + 500 + step, "AFDPDTOR", office="KPDT")
            self.tree.bulletin(self.t0 + 500 + step, "RADNTHES", ext="GIF", text=b"GIF89a")
            self.tree.write(f"{EMWIN}/not-an-emwin-name-{step}.bin")
            if step == 3:
                self.tree.slot(self.now - 200)
            self.tree.age_dirs(0)
            watched.poll(), listed.poll()
        self.assertEqual(len(watched.items), 24)
        # (files that landed in the same instant may be numbered in a different order)
        self.assertEqual(sorted((i.path, i.time, i.recv, i.size) for i in watched.items),
                         sorted((i.path, i.time, i.recv, i.size) for i in listed.items))
        self.assertEqual([i.recv for i in watched.items], [i.recv for i in listed.items])
        self.assertEqual(set(watched.types), set(listed.types))

    def test_forgetting_a_folder_stops_watching_it(self):
        scans = self.tree.path(f"{RUN}/{fx.FULL_DISK}")
        for n in range(1, 6):
            self.tree.slot(self.t0 + 600 * n)
        c = self.catalog()
        self.assertTrue(c.watcher.watching(scans))
        c.forget({f"{RUN}/{fx.FULL_DISK}"}, set())
        self.assertFalse(c.watcher.watching(scans))
        self.assertNotIn(scans, c.dirs)

    def test_a_folder_that_went_away_and_came_back_is_picked_up_again(self):
        c = self.catalog()
        shutil.rmtree(self.folder)
        c.poll()
        self.assertFalse(c.dirs[self.folder].watched)
        new = [self.tree.bulletin(self.t0 + 700 + n, "TORSEWWA") for n in range(6)]
        c.poll()
        for rel in new:
            self.assertIn("s/" + rel, c.by_path)
        self.assertTrue(c.dirs[self.folder].watched)   # big again, so watched again

    def test_a_listing_is_still_made_once_an_hour_to_be_sure(self):
        c = self.catalog()
        state = c.dirs[self.folder]
        with Listings() as listings:
            c.poll()
            self.assertEqual(listings.of(self.folder), 0)
            state.listed -= 3601
            c.poll()
            self.assertEqual(listings.of(self.folder), 1)
            c.poll()
            self.assertEqual(listings.of(self.folder), 1)


class CatalogFallbacks(CatalogCase):
    """What happens where the system cannot report new files, or does not."""

    watcher = Silent

    def test_the_dcs_folders_are_never_listed_by_the_catalog(self):
        """SatDump writes a DCS file every few seconds; the DCS reader takes care of those."""
        self.tree.write(f"{RUN}/DCS-archive/2026-11-19.zip", b"PK")
        self.tree.age_dirs()
        with Listings() as listings:
            c = self.catalog(watcher=None, age=False)
            for n in range(3):
                self.tree.write(f"{RUN}/DCS/pH-2632409330{n}-A.dcs.json", b"{}")
                c.poll()
        self.assertEqual(listings.of(self.tree.path(f"{RUN}/DCS")), 0)
        self.assertEqual(listings.of(self.tree.path(f"{RUN}/DCS-archive")), 0)
        self.assertNotIn(self.tree.path(f"{RUN}/DCS"), c.dirs)
        self.assertEqual(len(c.items), 10)

    def test_a_folder_called_dcs_among_the_images_is_still_read(self):
        self.tree.png(f"{RUN}/IMAGES/DCS/{fx.slot_name(self.t0)}/{fx.band_name(self.t0)}")
        c = self.catalog(watcher=None)
        self.assertEqual(len(c.items), 11)

    def test_a_watch_that_reports_nothing_is_caught_and_given_up(self):
        """A drive another machine writes to: the folder changes, the watch stays silent."""
        c = self.catalog()
        state = c.dirs[self.folder]
        self.assertTrue(state.watched)
        first = self.tree.bulletin(self.t0 + 100, "TORSEWWA")
        self.tree.age_dirs(60)
        c.poll()
        self.assertNotIn("s/" + first, c.by_path)   # once could be timing: no listing yet
        c.poll()
        self.assertIn("s/" + first, c.by_path)      # twice: listed, and found
        self.assertTrue(state.watched)
        second = self.tree.bulletin(self.t0 + 101, "TORSEWWA")
        self.tree.age_dirs(30)
        c.poll(), c.poll()
        self.assertIn("s/" + second, c.by_path)
        self.assertFalse(state.watched)
        self.assertTrue(state.deaf)
        self.assertFalse(c.watcher.watching(self.folder))
        self.assertEqual(sum("are not being reported" in line for line in self.logs), 1)
        third = self.tree.bulletin(self.t0 + 102, "TORSEWWA")   # from here on: found the plain way
        self.tree.age_dirs(10)
        c.poll()
        self.assertIn("s/" + third, c.by_path)
        self.assertFalse(state.watched)

    def test_a_change_that_brought_nothing_new_is_not_held_against_the_watch(self):
        c = self.catalog()
        state = c.dirs[self.folder]
        for n in range(4):
            self.tree.age_dirs(60 - n)   # the folder's time moves; nothing is new
            c.poll(), c.poll()
        self.assertTrue(state.watched)
        self.assertEqual(state.unheard, 0)

    def test_after_losing_track_every_watched_folder_is_listed(self):
        c = self.catalog(watcher=Overflowing())
        new = self.tree.bulletin(self.t0 + 100, "TORSEWWA")
        with Listings() as listings:
            c.poll()   # at once, not after waiting to see whether the silence lasts
            self.assertIn("s/" + new, c.by_path)
            self.assertEqual(listings.of(self.folder), 1)
            c.poll()
            self.assertEqual(listings.of(self.folder), 1)
        self.assertTrue(c.dirs[self.folder].watched)

    def test_a_slow_folder_is_listed_only_as_often_as_its_size_allows(self):
        """No watch at all (not Linux): a folder that takes half a second to list is listed
        about every ten seconds, not on every check."""
        c = self.catalog(watcher=None)
        state = c.dirs[self.folder]
        self.assertFalse(state.watched)
        state.cost, state.listed = 0.5, time.time()
        new = self.tree.bulletin(self.t0 + 100, "TORSEWWA")
        with Listings() as listings:
            for _ in range(5):
                c.poll()
            self.assertEqual(listings.of(self.folder), 0)
            self.assertNotIn("s/" + new, c.by_path)
            state.listed = time.time() - 10.5
            c.poll()
            self.assertEqual(listings.of(self.folder), 1)
        self.assertIn("s/" + new, c.by_path)
        self.assertLess(c.dirs[self.folder].cost, 0.5)   # measured afresh each time

    def test_a_quick_folder_is_listed_at_once_as_before(self):
        c = self.catalog(watcher=None)
        with Listings() as listings:
            for n in range(3):
                new = self.tree.bulletin(self.t0 + 100 + n, "TORSEWWA")
                c.poll()
                self.assertIn("s/" + new, c.by_path)
        self.assertEqual(listings.of(self.folder), 3)


class DcsCase(td.StoreCase):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.run_folder, "DCS")

    def store(self, watcher=None, history=3 * 86400):
        if hasattr(watcher, "close"):
            self.addCleanup(watcher.close)
        s = dcs.DcsStore(self.cache, lambda: [self.run_folder], history, self.logged.append, watcher=watcher)
        self.db = s.connect()
        self.addCleanup(self.db.close)
        return s

    def count(self):
        return self.db.execute("SELECT count(*) FROM messages").fetchone()[0]

    def add(self, t, **options):
        return self.write(t, [td.message("16876222", "x", t)], **options)

    def age_folder(self, seconds=60):
        os.utime(self.folder, (time.time() - seconds,) * 2)


@needs_watch
class WatchedDcs(DcsCase):
    def test_new_files_are_read_without_listing_the_folder(self):
        for n in range(3):
            self.add(self.now - 900 + n)
        s = self.store(watch.start())
        with Listings() as listings:
            self.drain(s)
            self.assertEqual(listings.of(self.folder), 1)   # the first look
            for n in range(5):
                self.add(self.now - 600 + n)
                self.drain(s)
            for _ in range(20):
                s.sweep(self.db)
            self.assertEqual(listings.of(self.folder), 1)
        self.assertEqual(self.count(), 8)
        self.assertEqual(s.waiting[RUN_KEY], {})

    def test_a_file_still_being_written_waits_and_is_then_read(self):
        s = self.store(watch.start())
        self.drain(s)
        path = self.add(self.now - 5, settle=False)
        with Listings() as listings:
            self.drain(s)
            self.assertEqual(self.count(), 0)
            self.assertEqual(list(s.waiting[RUN_KEY]), [os.path.basename(path)])
            os.utime(path, (time.time() - 30,) * 2)
            self.drain(s)
            self.assertEqual(listings.of(self.folder), 0)
        self.assertEqual(self.count(), 1)

    def test_a_file_that_is_gone_before_it_was_read_is_let_go(self):
        """Packed into its day's archive, or pruned, in between."""
        s = self.store(watch.start())
        self.drain(s)
        path = self.add(self.now - 5, settle=False)
        s.sweep(self.db)
        os.unlink(path)
        s.sweep(self.db)
        self.assertEqual(s.waiting[RUN_KEY], {})
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.logged, [])

    def test_other_files_and_old_files_are_not_queued(self):
        s = self.store(watch.start(), history=3600)
        self.drain(s)
        td.touch(os.path.join(self.folder, "notes.txt"), "mine")
        self.add(self.now - 7200)   # older than the history kept
        s.sweep(self.db)
        self.assertEqual(s.waiting[RUN_KEY], {})
        self.assertEqual(self.count(), 0)

    def test_a_folder_that_appears_later_is_picked_up(self):
        """Parse DCS switched on in SatDump while GOESview is running."""
        shutil.rmtree(self.folder)
        s = self.store(watch.start())
        s.sweep(self.db)
        self.assertFalse(s.folders[self.folder].watched)
        os.mkdir(self.folder)
        self.add(self.now - 600)
        self.drain(s)
        self.assertTrue(s.folders[self.folder].watched)
        self.assertEqual(self.count(), 1)

    def test_a_listing_is_still_made_once_an_hour_to_be_sure(self):
        s = self.store(watch.start())
        self.drain(s)
        with Listings() as listings:
            s.sweep(self.db)
            self.assertEqual(listings.of(self.folder), 0)
            s.folders[self.folder].listed -= 3601
            s.sweep(self.db)
            self.assertEqual(listings.of(self.folder), 1)


class DcsFallbacks(DcsCase):
    def test_without_a_watch_an_unchanged_folder_is_not_listed_again(self):
        self.add(self.now - 900)
        self.age_folder()
        s = self.store()
        with Listings() as listings:
            self.drain(s)
            for _ in range(10):
                s.sweep(self.db)
            self.assertEqual(listings.of(self.folder), 1)
            self.add(self.now - 800)
            self.age_folder(30)
            self.drain(s)
            self.assertEqual(listings.of(self.folder), 2)
        self.assertEqual(self.count(), 2)

    def test_without_a_watch_a_folder_changed_a_moment_ago_is_listed_again(self):
        """A file added in the same tick of the folder's clock would otherwise be missed."""
        self.add(self.now - 900)
        s = self.store()
        with Listings() as listings:
            s.sweep(self.db)
            s.sweep(self.db)
            self.assertEqual(listings.of(self.folder), 2)

    def test_a_watch_that_reports_nothing_is_caught_and_given_up(self):
        s = self.store(Silent())
        self.drain(s)
        state = s.folders[self.folder]
        self.assertTrue(state.watched)
        for n in range(2):
            self.add(self.now - 600 + n)
            self.age_folder(60 - n)
            s.sweep(self.db)    # once could be timing
            self.drain(s)       # twice: listed, found and read
            self.assertEqual(self.count(), n + 1)
        self.assertFalse(state.watched)
        self.assertTrue(state.deaf)
        self.assertEqual(sum("are not being reported" in line for line in self.logged), 1)
        self.add(self.now - 500)
        self.age_folder(20)
        self.drain(s)
        self.assertEqual(self.count(), 3)

    def test_after_losing_track_the_folder_is_listed(self):
        s = self.store(Overflowing())
        s.watcher.lost = False
        self.drain(s)
        self.add(self.now - 600)
        s.watcher.lost = True
        with Listings() as listings:
            self.drain(s)
            self.assertEqual(listings.of(self.folder), 1)
        self.assertEqual(self.count(), 1)


RUN_KEY = td.RUN


if __name__ == "__main__":
    unittest.main()
