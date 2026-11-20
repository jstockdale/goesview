"""Edge cases of which folders retention and the archives may reach (goesview.goes_runs)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import goesview  # noqa: E402

RUN = "2026-11-17_20-11_goes_hrit_1.6941 GHz"


class Scope(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name

    def run_folder(self, name):
        os.makedirs(os.path.join(self.root, name, "IMAGES", "GOES-18"))
        return os.path.join(self.root, name)

    def test_hidden_folders_are_not_runs(self):
        """The viewer does not show dot-folders, so retention must not reach into them either."""
        shown = self.run_folder(RUN)
        self.run_folder(".old_goes_hrit_backup")
        self.run_folder(".trash_goes_hrit")
        runs, protected, others = goesview.goes_runs(self.root, (), (".trash_goes_hrit",))
        self.assertEqual((runs, protected, others), ([shown], [], []))

    def test_protect_applies_when_the_root_is_the_run(self):
        run = self.run_folder(RUN)
        self.assertEqual(goesview.goes_runs(run, (), ()), ([run], [], []))
        self.assertEqual(goesview.goes_runs(run, (RUN,), ()), ([], [run], []))

    def test_a_linked_run_folder_is_not_a_run(self):
        real = os.path.join(self.root, "elsewhere")
        os.makedirs(os.path.join(real, "IMAGES"))
        os.symlink(real, os.path.join(self.root, "2026-01-01_00-00_goes_hrit_link"))
        runs, _, others = goesview.goes_runs(self.root, (), ())
        self.assertEqual(runs, [])
        self.assertEqual(sorted(others), ["2026-01-01_00-00_goes_hrit_link", "elsewhere"])


if __name__ == "__main__":
    unittest.main()
