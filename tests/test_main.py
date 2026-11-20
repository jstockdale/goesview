"""main(): that the command-line options are wired to the retention machinery as
the README says, above all that SatDump's data is only ever subject to deletion
when a --satdump-* option was given.

main() is run for real, but with the HTTP server replaced by a stand-in whose
serve_forever() returns at once and with Thread.start() switched off, so no
background thread (pruner, archiver, watcher, preview warmer) ever runs. The
App it built is then read back from goesview.Handler.app.
"""
import contextlib
import io
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, OLD_RUN, RUN  # noqa: E402

goesview = fx.goesview


class MainCase(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t, ancient = self.tree, self.now - 400 * DAY
        self.old = [t.slot(ancient), t.slot(ancient, run=OLD_RUN), t.bulletin(ancient), t.chart(ancient)]
        t.slot(self.now - 600)
        t.write("notes.txt")
        t.write(f"{RUN}/DCS/DCS_old.json", b"[]")
        self.before = fx.snapshot(t.root)
        self.addCleanup(setattr, goesview.Handler, "app", goesview.Handler.app)

    def main(self, *options, root=None):
        argv = ["goesview.py", root or self.tree.root, "--cache", self.tree.cache, "--host", "127.0.0.1", "--port", "0",
                "--lut", "none", "--no-text-index", "--no-dcs", "--no-monitor", *options]
        self.server = mock.MagicMock()
        self.stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(goesview, "ThreadingHTTPServer", return_value=self.server), \
                mock.patch.object(threading.Thread, "start") as self.started, \
                contextlib.redirect_stderr(self.stderr):
            goesview.main()
        self.server.serve_forever.assert_called_once_with()
        return goesview.Handler.app

    def said(self, text):
        return any(text in line for line in self.logs)


class RetentionWiringTests(MainCase):
    def test_defaults_never_delete_satdump_data(self):
        """No --satdump-* option: the cache is kept 30 days, and nothing can delete SatDump's files."""
        app = self.main()
        pruner = app.pruner
        self.assertEqual(pruner.cache.keep, 30 * DAY)
        self.assertIsNone(pruner.cache.max_size)
        self.assertIsNone(pruner.cache.disk_cap)
        self.assertFalse(pruner.satdump)
        self.assertFalse(pruner.dcs)
        self.assertFalse(pruner.dry_run)
        self.assertIsNone(app.archiver)
        self.assertIsNone(app.dcs_archiver)
        self.assertTrue(self.said("retention, SatDump data: never deleted (no --satdump-* option given)"))
        self.assertTrue(self.said("bulletin archive: off"))
        for sweep in (pruner.sweep_cache, pruner.sweep_satdump, pruner.sweep_dcs):
            sweep()
        self.assertEqual(fx.snapshot(self.tree.root), self.before)

    def test_startup_itself_leaves_the_satdump_folder_alone(self):
        self.main()
        self.assertEqual(fx.snapshot(self.tree.root), self.before)
        self.assertEqual(len(goesview.Handler.app.catalog.items), 8)

    def test_readme_example_with_dry_run(self):
        """--satdump-keep 3mo --satdump-disk-cap 90% --prune-dry-run"""
        app = self.main("--satdump-keep", "3mo", "--satdump-disk-cap", "90%", "--prune-dry-run")
        pruner = app.pruner
        self.assertEqual((pruner.satdump.keep, pruner.satdump.max_size, pruner.satdump.disk_cap), (90 * DAY, None, 0.9))
        self.assertTrue(pruner.dry_run)
        self.assertEqual(pruner.scope["runs"], [OLD_RUN, RUN])
        self.assertTrue(self.said("OLDER DATA IS DELETED (dry run)"))
        pruner.sweep_satdump()
        self.assertEqual(fx.snapshot(self.tree.root), self.before)
        self.assertTrue(pruner.last["satdump"]["dry"])

    def test_satdump_keep_deletes_when_asked(self):
        app = self.main("--satdump-keep", "30d")
        self.assertTrue(self.said("OLDER DATA IS DELETED"))
        app.pruner.sweep_satdump()
        self.assertOnlyRemoved(self.before, fx.snapshot(self.tree.root), self.old)

    def test_protect_and_include_reach_the_pruner_and_the_archiver(self):
        app = self.main("--satdump-max-size", "200G", "--satdump-protect", OLD_RUN, "--satdump-protect", "another",
                        "--satdump-include", "my goes pictures", "--archive-bulletins")
        self.assertEqual(app.pruner.satdump.max_size, 200 * 1024 ** 3)
        self.assertEqual(app.pruner.protect, {OLD_RUN, "another"})
        self.assertEqual(app.pruner.include, {"my goes pictures"})
        self.assertEqual(app.pruner.scope["protected"], [OLD_RUN])
        self.assertEqual(app.archiver.protect, {OLD_RUN, "another"})
        self.assertEqual(app.archiver.include, {"my goes pictures"})
        self.assertFalse(app.archiver.dry_run)

    def test_cache_options_do_not_become_satdump_limits(self):
        app = self.main("--cache-keep", "14d", "--cache-max-size", "500M", "--cache-disk-cap", "90%")
        cache = app.pruner.cache
        self.assertEqual((cache.keep, cache.max_size, cache.disk_cap), (14 * DAY, 500 * 1024 ** 2, 0.9))
        self.assertFalse(app.pruner.satdump)
        self.assertIsNone(app.pruner.scope)

    def test_cache_keep_off_with_nothing_else_means_no_pruner(self):
        app = self.main("--cache-keep", "off")
        self.assertIsNone(app.pruner)
        self.assertTrue(self.said("retention, GOESview cache: kept forever"))
        self.assertEqual(app.stats()["storage"]["last"], {})

    def test_dcs_limits_are_a_policy_of_their_own(self):
        """--satdump-dcs-keep must not make the rest of SatDump's data deletable."""
        app = self.main("--satdump-dcs-keep", "15d")
        self.assertEqual(app.pruner.dcs.keep, 15 * DAY)
        self.assertFalse(app.pruner.satdump)
        app.pruner.sweep_satdump()
        for rel in self.old:
            self.assertTrue(self.tree.exists(rel), rel)

    def test_archive_bulletins_is_off_unless_asked_and_honours_dry_run(self):
        self.assertIsNone(self.main().archiver)
        app = self.main("--archive-bulletins", "--prune-dry-run")
        self.assertTrue(app.archiver.dry_run)
        self.assertTrue(self.said("bulletin archive: on (dry run)"))
        app.archiver.sweep()
        self.assertEqual(fx.snapshot(self.tree.root), self.before)

    def test_no_background_thread_was_left_running(self):
        """(A check on this test's own harness: every thread main() makes is started
        through the switched-off Thread.start.)"""
        before = threading.active_count()
        self.main("--satdump-keep", "30d", "--archive-bulletins")
        self.assertGreaterEqual(self.started.call_count, 3)   # pruner, archiver, watcher
        self.assertEqual(threading.active_count(), before)


class RefusalTests(MainCase):
    def refused(self, *options, root=None):
        with self.assertRaises(SystemExit) as stopped:
            self.main(*options, root=root)
        self.server.serve_forever.assert_not_called()
        return str(stopped.exception.code), self.stderr.getvalue()

    def test_early_option_names_are_refused_not_guessed(self):
        """--keep, --max-size and --min-free did not say whose files they deleted;
        they now stop with a pointer to both families instead of doing either."""
        for options in (("--keep", "30d"), ("--max-size", "10G"), ("--min-free", "5G")):
            with self.subTest(options=options):
                _, stderr = self.refused(*options)
                self.assertIn("--cache-keep", stderr)
                self.assertIn("--satdump-keep", stderr)
        self.assertEqual(fx.snapshot(self.tree.root), self.before)

    def test_limits_that_cannot_be_read_stop_the_start(self):
        for options in (("--satdump-keep", "2h"), ("--satdump-keep", "soon"), ("--satdump-max-size", "200"),
                        ("--satdump-disk-cap", "100%"), ("--cache-keep", "1h"), ("--satdump-keep",)):
            with self.subTest(options=options):
                code, _ = self.refused(*options)
                self.assertEqual(code, "2")

    def test_root_must_be_a_folder(self):
        code, _ = self.refused(root=self.tree.path("notes.txt"))
        self.assertIn("Not a folder", code)


if __name__ == "__main__":
    unittest.main()
