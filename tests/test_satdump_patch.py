"""satdump-patch/: the script that installs the SatDump DCS fix, and the patch files beside it.

SatDump's source is not part of this repository, so the patching, building and installing
are not exercised here (they were, by hand, on a real SatDump build: see the README in that
folder). What is checked is everything that can go wrong without one: that the script
parses, refuses what it should, that the patch files are well formed and agree with each
other, and the one part that edits a user's file, settings.json.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(os.path.dirname(HERE), "satdump-patch")
SCRIPT = os.path.join(FOLDER, "fix-satdump-dcs.sh")
PATCHES = ("satdump-1.2.3-dcs.patch", "satdump-2.0-dcs.patch")
ADDRESS = "https://dadds-dcs-api.data.nesdis.noaa.gov/pdts/PDTS_COMPRESSED.txt"
OLD = "https://dcs1.noaa.gov/PDTS_COMPRESSED.txt"
DCS = "plugins/goes_support/goes/hrit/dcs/"

CAN_RUN = sys.platform != "win32" and all(shutil.which(tool) for tool in ("bash", "git", "python3"))


@unittest.skipUnless(CAN_RUN, "needs bash, git and python3")
class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goesview-satdump-patch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "config")
        self.settings = os.path.join(self.config, "satdump", "settings.json")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.satdump_is_running(False)

    def satdump_is_running(self, running):
        """Stand in for pgrep, so the tests do not depend on what this machine is running."""
        path = os.path.join(self.bin, "pgrep")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit %d\n" % (0 if running else 1))
        os.chmod(path, 0o755)

    def run_script(self, *args):
        env = dict(os.environ, XDG_CONFIG_HOME=self.config, PATH=self.bin + os.pathsep + os.environ["PATH"])
        env.pop("DESTDIR", None)
        done = subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env, cwd=self.tmp)
        return done.returncode, done.stdout + done.stderr

    def write_settings(self, goes_support):
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        content = {"user_interface": {"theme": {"value": "dark"}}, "plugin_settings": {"goes_support": goes_support},
                   "satdump_directories": {"default_output": "/somewhere/é"}}
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump(content, f, indent=4)
        return content

    def read_settings(self, suffix=""):
        with open(self.settings + suffix, encoding="utf-8") as f:
            return json.load(f)


class Basics(ScriptCase):
    def test_the_script_parses_and_is_executable(self):
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_help_lists_every_option_the_script_takes(self):
        code, out = self.run_script("--help")
        self.assertEqual(code, 0)
        for option in ("--check", "--patch-only", "--full-build", "--no-settings", "--settings-only", "--undo", "--force"):
            self.assertIn(option, out)

    def test_it_refuses_what_is_not_satdump(self):
        for args, why in (((), "give the folder"), (("--bogus", self.tmp), "unknown option"),
                          ((os.path.join(self.tmp, "missing"),), "is not a folder"),
                          ((self.tmp,), "does not look like SatDump's source")):
            code, out = self.run_script(*args)
            self.assertEqual(code, 1, out)
            self.assertIn(why, out)
        self.assertEqual(os.listdir(self.tmp), ["bin"])   # and left nothing behind

    def test_source_that_fits_neither_patch_is_left_alone(self):
        """Release 1.2.2 has a DCS reader, but not the one with the bug."""
        folder = os.path.join(self.tmp, "SatDump", *DCS.split("/"))
        os.makedirs(folder)
        with open(os.path.join(folder, "dcs_decoder.cpp"), "w") as f:
            f.write("// some other version\n")
        code, out = self.run_script(os.path.join(self.tmp, "SatDump"))
        self.assertEqual(code, 1, out)
        self.assertIn("neither patch fits", out)
        self.assertEqual(os.listdir(folder), ["dcs_decoder.cpp"])

    def test_source_with_the_bug_that_no_patch_fits_is_named_as_such(self):
        folder = os.path.join(self.tmp, "SatDump", *DCS.split("/"))
        os.makedirs(folder)
        with open(os.path.join(folder, "dcs_decoder.cpp"), "w") as f:
            f.write("dcp_mtx.lock();\ndcp_list[std::stoul(pdt_line.substr(field_offsets[1], 8), nullptr, 16)] = new_dcp;\n")
        code, out = self.run_script("--check", os.path.join(self.tmp, "SatDump"))
        self.assertEqual(code, 1, out)
        self.assertIn("has the bug, but neither patch fits", out)


class Patches(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(FOLDER, name), encoding="utf-8") as f:
            return f.read()

    def new_file(self, text, name):
        """The lines a patch adds as a brand-new file."""
        start = text.index(f"+++ b/{DCS}{name}")
        body = text[start:].split("\n")[2:]
        return [line[1:] for line in body if line.startswith("+")]

    def test_each_patch_touches_only_the_dcs_reader(self):
        for name in PATCHES:
            touched = sorted({line.split(" b/")[1] for line in self.read(name).split("\n") if line.startswith("diff --git")})
            self.assertEqual(touched, [DCS + "dcs_decoder.cpp", DCS + "dcs_settings.cpp", DCS + "dcs_tables.h"], name)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_each_patch_is_well_formed(self):
        for name in PATCHES:
            done = subprocess.run(["git", "apply", "--stat", os.path.join(FOLDER, name)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("3 files changed", done.stdout)

    def test_both_versions_carry_the_same_downloader(self):
        """dcs_tables.h has no SatDump dependencies, so it is the same file for 1.2.3 and 2.0."""
        old, new = (self.new_file(self.read(name), "dcs_tables.h") for name in PATCHES)
        self.assertGreater(len(old), 150)
        self.assertEqual(old, new)

    def test_both_versions_make_the_fix_and_add_the_address(self):
        for name in PATCHES:
            text = self.read(name)
            self.assertIn("+" + " " * 24 + "std::lock_guard<std::mutex>", text, name)
            self.assertNotIn("\n+" + " " * 24 + "dcp_mtx.lock();", text, name)
            self.assertIn(ADDRESS, text, name)
            self.assertIn('if (url.rfind("https://", 0) != 0)', text, name)   # nothing but https is fetched

    def test_the_script_adds_the_same_address_the_patches_do(self):
        with open(SCRIPT, encoding="utf-8") as f:
            self.assertIn(f'NEW_ADDRESS="{ADDRESS}"', f.read())


class Settings(ScriptCase):
    """--settings-only: the one step that edits a file of the user's."""

    def test_the_address_goes_first_and_everything_else_stays(self):
        before = self.write_settings({"pdt_urls": [OLD, "https://dcs2.noaa.gov/PDTS_COMPRESSED.txt"], "update_interval": 14400})
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 0, out)
        self.assertIn("Added NOAA's new address", out)
        after = self.read_settings()
        self.assertEqual(after["plugin_settings"]["goes_support"]["pdt_urls"],
                         [ADDRESS, OLD, "https://dcs2.noaa.gov/PDTS_COMPRESSED.txt"])
        after["plugin_settings"]["goes_support"]["pdt_urls"].pop(0)
        self.assertEqual(after, before)
        self.assertEqual(list(after), list(before))   # same order, as SatDump wrote it
        self.assertEqual(self.read_settings(".before-dcs-fix"), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.settings))), ["settings.json", "settings.json.before-dcs-fix"])

    def test_running_it_again_changes_nothing(self):
        self.write_settings({"pdt_urls": [OLD]})
        self.run_script("--settings-only")
        once = self.read_settings()
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 0, out)
        self.assertIn("already has NOAA's new address", out)
        self.assertEqual(self.read_settings(), once)
        self.assertEqual(self.read_settings(".before-dcs-fix")["plugin_settings"]["goes_support"]["pdt_urls"], [OLD])

    def test_settings_without_an_address_list_are_left_alone(self):
        """SatDump's patched default then applies, and that has the new address."""
        before = self.write_settings({"update_interval": -1})
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 0, out)
        self.assertIn("no PDT address list of its own", out)
        self.assertEqual(self.read_settings(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.settings)), ["settings.json"])

    def test_no_settings_file_is_not_an_error(self):
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.settings))

    def test_settings_are_never_edited_while_satdump_runs(self):
        """SatDump rewrites settings.json when it closes: an edit made meanwhile would be lost."""
        before = self.write_settings({"pdt_urls": [OLD]})
        self.satdump_is_running(True)
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 1, out)
        self.assertIn("SatDump is running", out)
        self.assertEqual(self.read_settings(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.settings)), ["settings.json"])

    def test_a_settings_file_that_is_not_json_is_left_alone(self):
        os.makedirs(os.path.dirname(self.settings))
        with open(self.settings, "w") as f:
            f.write("{ half written")
        code, out = self.run_script("--settings-only")
        self.assertEqual(code, 0, out)
        self.assertIn("Could not read", out)
        with open(self.settings) as f:
            self.assertEqual(f.read(), "{ half written")


if __name__ == "__main__":
    unittest.main()
