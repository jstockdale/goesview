"""The bulletin archive (--archive-bulletins): Archiver, and the catalog's view of it.

Pins the README's "Bulletin archive" section: a finished UTC day's EMWIN text
bulletins end up in <run>/EMWIN-archive/DAY.zip byte for byte, the loose files
go only once that is verified, today's and everything that is not a bulletin
stay, and the viewer keeps listing and serving them as before.

Archiver.sweep() is called directly; its thread is never started.
"""
import os
import sys
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, OLD_RUN, RUN  # noqa: E402

goesview = fx.goesview

AWKWARD = bytes(range(256)) + b"\r\r\n\x00\x01ZCZC\r\r\nline two\n\n\x03"  # every byte value, odd line ends


def archive_rel(ts, run=RUN):
    return "/".join(p for p in (run, "EMWIN-archive", fx.day_name(ts) + ".zip") if p)


def zip_contents(path):
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


class ArchiverCase(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t = self.tree
        # Two days back is a finished day at any hour; a minute ago is always "today".
        self.d3, self.d2, self.today = self.now - 3 * DAY, self.now - 2 * DAY, self.now - 60
        self.day3 = [t.bulletin(self.d3, text=AWKWARD), t.bulletin(self.d3 + 5), t.bulletin(self.d3 + 9, ident="TORSEWWA")]
        self.day2 = [t.bulletin(self.d2), t.bulletin(self.d2 + 7, ext="txt")]
        self.fresh = [t.bulletin(self.today), t.bulletin(self.today + 20)]
        self.originals = {rel: t.read(rel) for rel in self.day3 + self.day2 + self.fresh}

    def start(self, scan=True, **options):
        self.app = fx.make_app(self, self.tree, scan=scan)
        self.archiver = fx.make_archiver(self.app, **options)
        return self.archiver

    def archive(self, ts, run=RUN):
        return self.tree.path(archive_rel(ts, run))

    def by_name(self, rels):
        return {rel.rsplit("/", 1)[1]: self.originals[rel] for rel in rels}


class PackingTests(ArchiverCase):
    def test_finished_days_are_packed_one_zip_per_day(self):
        """Each finished day's bulletins go into <run>/EMWIN-archive/DAY.zip, byte-identical,
        under their original names; then the loose files are removed."""
        self.start().sweep()
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))
        self.assertEqual(zip_contents(self.archive(self.d2)), self.by_name(self.day2))
        for rel in self.day3 + self.day2:
            self.assertFalse(self.tree.exists(rel), rel)
        self.assertEqual(sorted(os.listdir(self.tree.path(f"{RUN}/EMWIN-archive"))),
                         sorted([fx.day_name(self.d3) + ".zip", fx.day_name(self.d2) + ".zip"]))

    def test_todays_bulletins_stay_ordinary_files(self):
        """Today's bulletins stay as they are: SatDump may still be writing there."""
        self.start().sweep()
        for rel in self.fresh:
            self.assertEqual(self.tree.read(rel), self.originals[rel])
        self.assertFalse(os.path.exists(self.archive(self.today)))

    def test_a_day_is_packed_an_hour_after_it_ends(self):
        """README: "A day is packed an hour after it ends (UTC)." """
        t = self.tree
        midnight = self.now // DAY * DAY
        yesterday = t.bulletin(midnight - 3600)
        early_today = t.bulletin(midnight + 60)
        archiver = self.start()
        with mock.patch.object(goesview.time, "time", return_value=midnight + 1800):   # 00:30 UTC
            archiver.sweep()
        self.assertTrue(t.exists(yesterday), "packed inside the hour of grace")
        self.assertFalse(os.path.exists(self.archive(midnight - 3600)))
        with mock.patch.object(goesview.time, "time", return_value=midnight + 3660):   # 01:01 UTC
            archiver.sweep()
        self.assertFalse(t.exists(yesterday))
        self.assertIn(os.path.basename(yesterday), zip_contents(self.archive(midnight - 3600)))
        self.assertTrue(t.exists(early_today))

    def test_archives_are_plain_zip_files(self):
        """README: "unzip gets them back without GOESview": a sound zip, flat, nothing else in it."""
        self.start().sweep()
        with zipfile.ZipFile(self.archive(self.d3)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(sorted(zf.namelist()), sorted(self.by_name(self.day3)))
            self.assertTrue(all("/" not in name for name in zf.namelist()))

    def test_status_reports_what_was_done(self):
        archiver = self.start()
        self.assertEqual(archiver.status(), {"days": 0, "last": None, "dry": False})
        archiver.sweep()
        status = archiver.status()
        self.assertEqual(status["days"], 2)
        self.assertEqual((status["last"]["day"], status["last"]["n"]), (fx.day_name(self.d2), 2))
        self.assertGreater(status["last"]["freed"], 0)
        self.assertEqual(self.app.stats()["storage"]["archive"], status)

    def test_only_emwin_text_bulletins_are_touched(self):
        """It "touches nothing but EMWIN .txt bulletins"."""
        t, old = self.tree, self.d3
        bystanders = [
            t.bulletin(old, ident="RADNTHES", ext="GIF", text=b"GIF89a"),          # an EMWIN graphic
            t.write(f"{RUN}/EMWIN/notes.txt", mtime=old),                           # not SatDump's name
            t.write(f"{RUN}/EMWIN/{fx.emwin_name(old, serial=900)}.bak", mtime=old),
            t.write(f"{RUN}/EMWIN/saved/{fx.emwin_name(old, serial=901)}", mtime=old),  # in a folder of the user's
            t.write(f"{RUN}/Admin Messages/{fx.emwin_name(old, serial=902)}", mtime=old),
            t.write(f"{RUN}/IMAGES/NWS/{fx.emwin_name(old, serial=903)}", mtime=old),
            t.slot(old),
            t.chart(old),
            t.write(f"{RUN}/DCS/DCS_old.json", b"[]", mtime=old),
        ]
        link = f"{RUN}/EMWIN/{fx.emwin_name(old, serial=904)}"                     # a link is not a bulletin
        os.symlink(t.path(bystanders[1]), t.path(link))
        before = fx.snapshot(t.root)
        self.start().sweep()
        after = fx.snapshot(t.root)
        made = {rel for rel in after if rel.startswith(f"{RUN}/EMWIN-archive")}
        self.assertEqual(len(made), 3)  # the folder and one zip per finished day; nothing else is new
        self.assertOnlyRemoved(before, {rel: v for rel, v in after.items() if rel not in made},
                               self.day3 + self.day2)
        self.assertIn(link, after)
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))

    def test_dry_run_packs_nothing(self):
        """With --prune-dry-run it only reports what it would pack."""
        before = fx.snapshot(self.tree.root)
        archiver = self.start(dry_run=True)
        archiver.sweep()
        self.assertEqual(fx.snapshot(self.tree.root), before)
        self.assertEqual(archiver.status(), {"days": 0, "last": None, "dry": True})
        self.assertTrue(any("dry run" in line and "would pack 3 bulletins" in line for line in self.logs), self.logs)

    def test_late_arrivals_join_a_day_already_packed(self):
        """A bulletin for a packed day that turns up later is added to that day's zip."""
        archiver = self.start()
        archiver.sweep()
        late = self.tree.bulletin(self.d3 + 100, text="LATE ARRIVAL\r\n")
        expected = dict(self.by_name(self.day3), **{os.path.basename(late): b"LATE ARRIVAL\r\n"})
        archiver.sweep()
        self.assertEqual(zip_contents(self.archive(self.d3)), expected)
        self.assertFalse(self.tree.exists(late))
        with zipfile.ZipFile(self.archive(self.d3)) as zf:
            self.assertEqual(len(zf.namelist()), len(set(zf.namelist())), "a bulletin was stored twice")

    def test_second_sweep_with_nothing_new_changes_nothing(self):
        archiver = self.start()
        archiver.sweep()
        before = fx.snapshot(self.tree.root)
        archiver.sweep()
        self.assertEqual(fx.snapshot(self.tree.root), before)

    def test_leftover_temporary_file_does_not_get_in_the_way(self):
        """A .tmp left by a crash mid-pack is discarded, not trusted or appended to."""
        self.tree.write(archive_rel(self.d3) + ".tmp", b"half a zip, left by a crash")
        self.start().sweep()
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))
        self.assertFalse(os.path.exists(self.archive(self.d3) + ".tmp"))


class ScopeTests(ArchiverCase):
    def test_only_goes_hrit_runs_and_not_protected_ones(self):
        """It "only works inside GOESview's idea of a GOES HRIT run folder" and
        "honours --satdump-protect". Old runs are runs too."""
        t = self.tree
        in_old_run = t.bulletin(self.d3, run=OLD_RUN)
        elsewhere = [
            t.bulletin(self.d3, run="my goes pictures"),
            t.bulletin(self.d3, run="2026-11-01_08-00_noaa_hrpt_1.7 GHz"),
            t.bulletin(self.d3, run="2026-08-01_00-00_goes_hrit_protected"),
            t.write(f"EMWIN/{fx.emwin_name(self.d3, serial=77)}"),   # a loose EMWIN folder beside the runs
        ]
        before = fx.snapshot(t.root)
        self.start(protect=["2026-08-01_00-00_goes_hrit_protected"]).sweep()
        after = fx.snapshot(t.root)
        for rel in elsewhere:
            self.assertEqual(after.get(rel), before[rel], rel)
        self.assertFalse(t.exists(in_old_run))
        self.assertIn(os.path.basename(in_old_run), zip_contents(self.archive(self.d3, OLD_RUN)))
        for top in ("my goes pictures", "2026-11-01_08-00_noaa_hrpt_1.7 GHz", "2026-08-01_00-00_goes_hrit_protected"):
            self.assertFalse(t.exists(f"{top}/EMWIN-archive"), top)
        self.assertFalse(t.exists("EMWIN-archive"))

    def test_included_folder_is_packed(self):
        t = self.tree
        mine = t.bulletin(self.d3, run="my goes pictures", text="MINE\n")
        self.start(include=["my goes pictures"]).sweep()
        self.assertFalse(t.exists(mine))
        self.assertEqual(zip_contents(self.archive(self.d3, "my goes pictures")), {os.path.basename(mine): b"MINE\n"})

    def test_root_that_is_one_run_folder(self):
        """Pointed straight at a run folder, the archive sits in it and is still served."""
        rel = "EMWIN/" + os.path.basename(self.day3[0])
        app = fx.make_app(self, self.tree, root=self.tree.path(RUN))
        item = app.catalog.by_path["s/" + rel]
        fx.make_archiver(app).sweep()
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))
        vpath = f"z/EMWIN-archive/{fx.day_name(self.d3)}.zip/{os.path.basename(rel)}"
        self.assertEqual(item.path, vpath)
        self.assertEqual(app.catalog.read_bytes(vpath), AWKWARD)

    # Regression: the archiver once scanned and emptied <run>/EMWIN by path alone. With that folder a
    # link to somewhere else, the bulletins there were packed into the run and the loose files, outside
    # the data folder, deleted.
    def test_a_linked_emwin_folder_is_not_emptied(self):
        t = self.tree
        elsewhere = os.path.join(t.tmp, "elsewhere", "EMWIN")
        os.makedirs(elsewhere)
        name = fx.emwin_name(self.d3, serial=500)
        with open(os.path.join(elsewhere, name), "wb") as f:
            f.write(b"outside the data folder\n")
        t.slot(self.d3, run=OLD_RUN)
        os.symlink(elsewhere, t.path(f"{OLD_RUN}/EMWIN"))
        self.start().sweep()
        self.assertTrue(os.path.exists(os.path.join(elsewhere, name)))


class SafetyTests(ArchiverCase):
    """README: "Nothing is deleted until it is proven safe. ... If any step fails, they stay." """

    def assertNothingLost(self, rels):
        for rel in rels:
            self.assertTrue(self.tree.exists(rel), f"loose file deleted: {rel}")
            self.assertEqual(self.tree.read(rel), self.originals[rel])

    def test_archive_that_does_not_read_back_identically_is_discarded(self):
        archiver = self.start()
        with mock.patch.object(zipfile.ZipFile, "read", lambda zf, name, pwd=None: b"not what was written"):
            archiver.sweep()
        self.assertNothingLost(self.day3 + self.day2)
        self.assertFalse(self.tree.exists(f"{RUN}/EMWIN-archive/{fx.day_name(self.d3)}.zip"))
        self.assertFalse(self.tree.exists(f"{RUN}/EMWIN-archive/{fx.day_name(self.d3)}.zip.tmp"))
        self.assertTrue(any("NOT packed" in line for line in self.logs), self.logs)
        self.assertIsNone(archiver.last)
        # the catalog still points at the loose files
        self.assertIn("s/" + self.day3[0], self.app.catalog.by_path)

    def test_archive_that_cannot_be_moved_into_place_costs_nothing(self):
        archiver = self.start()
        with mock.patch.object(goesview.os, "replace", side_effect=OSError("disk full")):
            archiver.sweep()
        self.assertNothingLost(self.day3 + self.day2)
        self.assertEqual(os.listdir(self.tree.path(f"{RUN}/EMWIN-archive")), [])

    def test_archive_that_cannot_be_written_costs_nothing(self):
        archiver = self.start()
        real = zipfile.ZipFile.write
        calls = []

        def failing_write(zf, *args, **kwargs):
            calls.append(args)
            if len(calls) > 1:
                raise OSError("No space left on device")
            return real(zf, *args, **kwargs)
        with mock.patch.object(zipfile.ZipFile, "write", failing_write):
            archiver.sweep()
        self.assertNothingLost(self.day3 + self.day2)
        self.assertEqual(os.listdir(self.tree.path(f"{RUN}/EMWIN-archive")), [])

    def test_failure_while_adding_late_arrivals_leaves_the_existing_archive_alone(self):
        archiver = self.start()
        archiver.sweep()
        with open(self.archive(self.d3), "rb") as f:
            packed = f.read()
        late = self.tree.bulletin(self.d3 + 100, text="LATE\n")
        with mock.patch.object(zipfile.ZipFile, "read", lambda zf, name, pwd=None: b"corrupt"):
            archiver.sweep()
        with open(self.archive(self.d3), "rb") as f:
            self.assertEqual(f.read(), packed)
        self.assertEqual(self.tree.read(late), b"LATE\n")
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))

    def test_loose_file_differing_from_its_archived_namesake_is_kept(self):
        """Same name already in the day's zip, different bytes: neither copy may be lost."""
        archiver = self.start()
        archiver.sweep()
        name = os.path.basename(self.day3[1])
        self.tree.write(self.day3[1], b"a different bulletin under the same name\n", mtime=self.d3)
        archiver.sweep()
        self.assertEqual(self.tree.read(self.day3[1]), b"a different bulletin under the same name\n")
        self.assertEqual(zip_contents(self.archive(self.d3))[name], self.originals[self.day3[1]])

    def test_pack_interrupted_before_the_cleanup_is_finished_later(self):
        """Killed after the zip went into place but before the loose files were removed:
        the next sweep verifies them against the zip and only then removes them."""
        self.start(scan=False)
        os.makedirs(self.tree.path(f"{RUN}/EMWIN-archive"))
        with zipfile.ZipFile(self.archive(self.d3), "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in self.by_name(self.day3).items():
                zf.writestr(name, data)
        self.tree.age_dirs()
        self.app.catalog.scan()
        self.assertEqual(len(self.app.catalog.items), len(self.originals), "listed twice after an interrupted pack")
        self.archiver.sweep()
        for rel in self.day3:
            self.assertFalse(self.tree.exists(rel))
        self.assertEqual(zip_contents(self.archive(self.d3)), self.by_name(self.day3))
        with zipfile.ZipFile(self.archive(self.d3)) as zf:
            self.assertEqual(len(zf.namelist()), 3)
        self.assertEqual(len(self.app.catalog.items), len(self.originals))
        for rel in self.day3:
            vpath = f"z/{archive_rel(self.d3)}/{os.path.basename(rel)}"
            self.assertEqual(self.app.catalog.read_bytes(vpath), self.originals[rel])


class ViewerTests(ArchiverCase):
    """README: "Nothing changes in the viewer. Archived bulletins are listed, shown,
    linked and searched exactly as before." """

    def vpath(self, rel, ts):
        return f"z/{archive_rel(ts)}/{os.path.basename(rel)}"

    def listing(self, catalog):
        return sorted((item.type.id, int(item.time), item.name) for item in catalog.items)

    def test_same_items_now_read_from_the_archive(self):
        archiver = self.start()
        catalog = self.app.catalog
        feed_before = [(item.id, item.type.id, item.time, item.name) for item in catalog.feed(limit=100)[0]]
        ids = {rel: catalog.by_path["s/" + rel].id for rel in self.originals}
        archiver.sweep()
        self.assertEqual([(item.id, item.type.id, item.time, item.name) for item in catalog.feed(limit=100)[0]],
                         feed_before)
        for rel in self.day3:
            item = catalog.by_path[self.vpath(rel, self.d3)]
            self.assertEqual(item.id, ids[rel])
            self.assertNotIn("s/" + rel, catalog.by_path)
            self.assertEqual(catalog.read_bytes(item.path), self.originals[rel])
        for rel in self.fresh:
            self.assertEqual(catalog.by_path["s/" + rel].id, ids[rel])
        self.assertEqual(catalog.read_bytes(self.vpath(self.day3[0], self.d3), 16), AWKWARD[:16])

    def test_folder_checks_after_packing_do_not_list_anything_twice(self):
        archiver = self.start()
        catalog = self.app.catalog
        before = self.listing(catalog)
        archiver.sweep()
        for _ in range(3):
            catalog.poll()
        self.assertEqual(self.listing(catalog), before)
        self.assertEqual(len(catalog.items), len(self.originals))

    def test_restart_after_packing_lists_the_same_bulletins(self):
        """A fresh start finds archived bulletins under the same type, time and name."""
        archiver = self.start()
        before = self.listing(self.app.catalog)
        archiver.sweep()
        fresh = fx.make_app(self, self.tree).catalog
        self.assertEqual(self.listing(fresh), before)
        for rel in self.day3:
            self.assertEqual(fresh.read_bytes(self.vpath(rel, self.d3)), self.originals[rel])

    def test_late_arrival_is_readable_through_a_catalog_that_had_the_archive_open(self):
        archiver = self.start()
        catalog = self.app.catalog
        archiver.sweep()
        self.assertEqual(catalog.read_bytes(self.vpath(self.day3[0], self.d3)), AWKWARD)  # archive now held open
        time.sleep(0.02)  # let the replaced zip get a different modification time on any filesystem
        late = self.tree.bulletin(self.d3 + 100, text="LATE\n")
        self.tree.age_dirs()
        catalog.poll()
        self.assertIn("s/" + late, catalog.by_path)
        archiver.sweep()
        self.assertEqual(catalog.by_path[self.vpath(late, self.d3)].path, self.vpath(late, self.d3))
        self.assertEqual(catalog.read_bytes(self.vpath(late, self.d3)), b"LATE\n")
        self.assertEqual(catalog.read_bytes(self.vpath(self.day3[0], self.d3)), AWKWARD)

    def test_search_index_is_told_where_bulletins_went(self):
        archiver = self.start()
        self.app.text_index = mock.Mock()
        archiver.sweep()
        jobs = [call.args[0] for call in self.app.text_index.jobs.put.call_args_list]
        self.assertEqual([job[0] for job in jobs], ["move", "move"])
        moves = dict(jobs[0][1])
        self.assertEqual(moves, {"s/" + rel: self.vpath(rel, self.d3) for rel in self.day3})

    def test_unreadable_archive_paths_give_nothing(self):
        """read_bytes answers None, never an exception, for anything it cannot serve."""
        archiver = self.start()
        archiver.sweep()
        catalog = self.app.catalog
        day = fx.day_name(self.d3)
        for vpath in (f"z/{RUN}/EMWIN-archive/{day}.zip/no-such-bulletin.TXT",
                      f"z/{RUN}/EMWIN-archive/1999-01-01.zip/x.TXT",
                      f"z/{RUN}/EMWIN-archive/{day}.zip",
                      f"z/{RUN}/EMWIN/{day}.zip/x.TXT",
                      "z/", "z/x", "s/", "s/nothing/here.txt", "q/whatever"):
            with self.subTest(vpath=vpath):
                self.assertIsNone(catalog.read_bytes(vpath))

    def test_only_day_archives_inside_the_root_are_opened(self):
        """A "z/" path cannot be used to read a zip outside the data folder, or one
        that is not a day archive in an EMWIN-archive folder."""
        t = self.tree
        outside = os.path.join(t.tmp, "elsewhere", "EMWIN-archive")
        os.makedirs(outside)
        with zipfile.ZipFile(os.path.join(outside, "2026-01-01.zip"), "w") as zf:
            zf.writestr("secret.txt", "secret")
        t.mkdir(f"{RUN}/private")
        with zipfile.ZipFile(t.path(f"{RUN}/private/2026-01-01.zip"), "w") as zf:
            zf.writestr("secret.txt", "secret")
        t.mkdir(f"{RUN}/linked")
        os.symlink(outside, t.path(f"{RUN}/linked/EMWIN-archive"))
        catalog = self.start().catalog
        for vpath in ("z/../elsewhere/EMWIN-archive/2026-01-01.zip/secret.txt",
                      f"z/{RUN}/../../elsewhere/EMWIN-archive/2026-01-01.zip/secret.txt",
                      "z/" + outside + "/2026-01-01.zip/secret.txt",
                      "z//" + outside.lstrip("/") + "/2026-01-01.zip/secret.txt",
                      f"z/{RUN}/linked/EMWIN-archive/2026-01-01.zip/secret.txt",
                      f"z/{RUN}/private/2026-01-01.zip/secret.txt"):
            with self.subTest(vpath=vpath):
                self.assertIsNone(catalog.read_bytes(vpath))


if __name__ == "__main__":
    unittest.main()
