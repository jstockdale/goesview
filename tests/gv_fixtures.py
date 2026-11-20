"""Shared fixtures for the goesview.py / pair_false_color.py tests.

Everything here builds a fake SatDump output tree inside a temporary directory,
using SatDump's real naming, and wires up goesview's objects the way main() does
but without starting any of their background threads.

Not a test module itself (unittest discovery only loads test_*.py).
"""
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import warnings
import zlib
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TESTS)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import goesview  # noqa: E402

HAVE_PIL = goesview.Image is not None
HAVE_FC = goesview.fc is not None  # numpy and Pillow both present, pair_false_color imported

HOUR, DAY = 3600, 86400
RUN = "2026-11-17_20-11_goes_hrit_1.6941 GHz"       # the name SatDump gives a live GOES HRIT run
OLD_RUN = "2026-09-02_07-45_goes_hrit_1.6941 GHz"   # an earlier run: SatDump starts a new one per restart
FULL_DISK = "IMAGES/GOES-18/Full Disk"
MESO = "IMAGES/GOES-18/Mesoscale 1"


def have_fts5():
    """Does this Python's SQLite have the full-text extension the search index needs?"""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(a, content='', detail=none)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        db.close()


# ------------------------------------------------------------------ SatDump names

def slot_name(ts):
    """A timestamp folder, as SatDump names one scan: 2026-11-17_20-10-21."""
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(ts))


def band_name(ts, band=2, sat="G18", ext="png"):
    """One instrument channel: G18_2_20261117T201021Z.png."""
    return f"{sat}_{band}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(ts))}.{ext}"


def emwin_name(ts, ident="AFDSEWWA", wmo="FXUS66", office="KSEW", serial=1, ext="TXT"):
    """An EMWIN product: A_FXUS66KSEW172011_C_KWIN_20261117201100_000001-2-AFDSEWWA.TXT."""
    g = time.gmtime(ts)
    return (f"A_{wmo}{office}{time.strftime('%d%H%M', g)}_C_KWIN_"
            f"{time.strftime('%Y%m%d%H%M%S', g)}_{serial:06d}-2-{ident}.{ext}")


def nws_name(ts, chart="pac_sfc_analysis", ext="png"):
    """An NWS chart: 20261117201100-pac_sfc_analysis.png."""
    return f"{time.strftime('%Y%m%d%H%M%S', time.gmtime(ts))}-{chart}.{ext}"


def day_name(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def png_bytes(width=4, height=4, value=128):
    """A real greyscale PNG, made without Pillow so fixtures exist under any interpreter."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([value]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------- tree

class Tree:
    """A temporary folder holding a fake SatDump output folder (`root`), a place for
    GOESview's cache (`cache`), and room beside them for things that must stay out of
    reach (`tmp`). All relative paths use "/" and are relative to `root`, so the
    catalog's virtual path for a file written with rel is "s/" + rel."""

    def __init__(self, test):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="goesview-test-"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "satdump")
        self.cache = os.path.join(self.tmp, "cache")
        os.mkdir(self.root)
        self.serial = 0

    def path(self, rel=""):
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def exists(self, rel):
        return os.path.lexists(self.path(rel))

    def read(self, rel):
        with open(self.path(rel), "rb") as f:
            return f.read()

    def mkdir(self, rel):
        os.makedirs(self.path(rel), exist_ok=True)
        return rel

    def write(self, rel, data=b"not SatDump's\n", mtime=None):
        """Write a file. Its mtime defaults to a minute ago, so it counts as fully written."""
        full = self.path(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        mtime = time.time() - 60 if mtime is None else mtime
        os.utime(full, (mtime, mtime))
        return rel

    def png(self, rel, mtime=None, value=128):
        return self.write(rel, png_bytes(value=value), mtime)

    @staticmethod
    def arrival(ts):
        """When a product dated ts would have landed: just after, but never in the future."""
        return min(ts + 30, time.time() - 30)

    def slot(self, ts, bands=(2, 13), run=RUN, group=FULL_DISK, extra=(), mtime=None, sat="G18"):
        """One scan: a timestamp folder with a PNG per band (plus `extra` file names)."""
        rel = "/".join(p for p in (run, group, slot_name(ts)) if p)
        mtime = self.arrival(ts) if mtime is None else mtime
        self.mkdir(rel)
        for band in bands:
            self.png(f"{rel}/{band_name(ts, band, sat)}", mtime)
        for name in extra:
            self.png(f"{rel}/{name}", mtime)
        return rel

    def bulletin(self, ts, ident="AFDSEWWA", text=None, run=RUN, ext="TXT", mtime=None, **names):
        """One EMWIN product in <run>/EMWIN/."""
        self.serial += 1
        name = emwin_name(ts, ident, serial=names.pop("serial", self.serial), ext=ext, **names)
        if text is None:
            text = f"000\r\nFXUS66 KSEW\r\n{ident}\r\n\r\nBulletin number {self.serial} for testing.\r\n"
        data = text if isinstance(text, bytes) else text.encode("latin-1")
        rel = "/".join(p for p in (run, "EMWIN", name) if p)
        return self.write(rel, data, self.arrival(ts) if mtime is None else mtime)

    def chart(self, ts, chart="pac_sfc_analysis", run=RUN, mtime=None):
        """One NWS chart in <run>/IMAGES/NWS/."""
        rel = "/".join(p for p in (run, "IMAGES/NWS", nws_name(ts, chart)) if p)
        return self.png(rel, self.arrival(ts) if mtime is None else mtime)

    def age_dirs(self, seconds=120):
        """Give every folder an mtime in the past, so that the next file created in one
        is certain to change it (some filesystems only tick their clock every few ms)."""
        when = time.time() - seconds
        for base, _, _ in os.walk(self.tmp):
            os.utime(base, (when, when))


def snapshot(folder):
    """Everything under a folder: {relative path: file bytes | ("link", target) | "dir"}."""
    out = {}
    for base, dirs, names in os.walk(folder):
        for name in dirs + names:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            if os.path.islink(full):
                out[rel] = ("link", os.readlink(full))
            elif os.path.isdir(full):
                out[rel] = "dir"
            else:
                with open(full, "rb") as f:
                    out[rel] = f.read()
    return out


# ----------------------------------------------------------------------- wiring

def make_app(test, tree, scan=True, settle=0.0, root=None):
    """Catalog + Previews + App, wired as in goesview.main(), nothing running.
    `root` points GOESview somewhere other than the top of the tree (at one run, say)."""
    catalog = goesview.Catalog(root or tree.root, os.path.join(tree.cache, "derived"), settle=settle)
    previews = goesview.Previews(catalog, os.path.join(tree.cache, "previews"), 1)
    app = goesview.App(catalog, previews, None, False)

    def close_zips():
        for _, zf in catalog.zips.values():
            zf.close()
    test.addCleanup(close_zips)
    if scan:
        tree.age_dirs()
        catalog.scan()
    return app


def make_pruner(app, cache=None, satdump=None, dry_run=False, protect=(), include=()):
    """A Pruner as main() builds one; it is a thread, but it is never started here."""
    pruner = goesview.Pruner(app, cache or goesview.Limits(), satdump or goesview.Limits(), 600, dry_run,
                             protect, include, goesview.Limits())
    app.pruner = pruner
    return pruner


def make_archiver(app, dry_run=False, protect=(), include=()):
    archiver = goesview.Archiver(app, protect, include, dry_run)
    app.archiver = archiver
    return archiver


def paths(items):
    return [item.path for item in items]


class GoesviewCase(unittest.TestCase):
    """A temp tree per test, and goesview's log captured in self.logs instead of printed."""

    def setUp(self):
        # goesview.goes_runs() leaves a scandir iterator for the garbage collector when it
        # finds what it is looking for early; harmless, and not what these tests are about.
        quiet = warnings.catch_warnings()
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        warnings.filterwarnings("ignore", "unclosed scandir iterator", ResourceWarning)
        self.logs = []
        patcher = mock.patch.object(goesview, "log", lambda *parts: self.logs.append(" ".join(map(str, parts))))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tree = Tree(self)
        self.now = int(time.time())

    def assertOnlyRemoved(self, before, after, doomed):
        """Between two snapshots, exactly the `doomed` paths (and everything inside
        them) disappeared, and nothing else was added, removed or changed."""
        doomed = list(doomed)

        def is_doomed(rel):
            return any(rel == d or rel.startswith(d + "/") for d in doomed)
        added = sorted(set(after) - set(before))
        self.assertEqual(added, [], "files appeared inside the folder")
        wrongly_removed = sorted(rel for rel in before if rel not in after and not is_doomed(rel))
        self.assertEqual(wrongly_removed, [], "removed, but should have been left alone")
        survivors = sorted(rel for rel in after if is_doomed(rel))
        self.assertEqual(survivors, [], "should have been removed")
        changed = sorted(rel for rel in after if after[rel] != before[rel])
        self.assertEqual(changed, [], "contents changed")
        for d in doomed:
            self.assertIn(d, before, f"fixture error: {d} never existed")
