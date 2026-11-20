#!/usr/bin/env python3
"""goesview: a live viewer and file server for SatDump GOES HRIT output.

    goesview.py /path/to/satdump/output
    goesview.py /path/to/satdump/output --port 1234 --lut wxstar

Point it at a SatDump live output folder (the one holding IMAGES/, EMWIN/, L2/)
or at a folder of several such runs, then open http://<host>:8000/.

What it does:
  * watches the folder and keeps an in-memory catalog of every image and text
    product, grouped by type, pushing new arrivals to browsers as they land
  * serves small cached JPEG previews so 5424x5424 frames load instantly
  * builds its own false-colour image for every GOES slot with the chosen
    colour table, pairing band 2 and band 13 across timestamp folders when the
    feed splits them (see pair_false_color.py)

  * keeps its own cache in check (--cache-* options; 30 days by default)
  * never deletes anything SatDump received, unless you explicitly give a
    --satdump-* retention option

Standard library only for serving; Pillow for previews, numpy for false colour.
Derived files go to --cache. The SatDump folder is only ever read, unless a
--satdump-* option is given.
"""
import argparse
import bisect
import calendar
import collections
import gzip
import hashlib
import json
import mimetypes
import os
import posixpath
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import timedelta
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dcs
import monitor

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

try:
    from PIL import Image, ImageFile
    Image.MAX_IMAGE_PIXELS = None  # full disk frames are large on purpose
    ImageFile.LOAD_TRUNCATED_IMAGES = True  # show what arrived of a frame cut short by a dropout
except ImportError:
    Image = None
try:
    import numpy as np
    import pair_false_color as fc
except ImportError:
    np = fc = None


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# ------------------------------------------------------------- classification

ANCHORS = ("IMAGES", "L2", "EMWIN", "Admin Messages")
IMAGE_EXT = {"png", "jpg", "jpeg", "gif"}
TEXT_EXT = {"txt"}
ARCHIVE_DIR = "EMWIN-archive"  # in a run folder: one zip of text bulletins per finished day
DAY_ZIP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.zip$")
SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
BAND_RE = re.compile(r"^(G\d\d|HIM)_(\w+?)_\d{8}T\d{6}Z(?:_\d+)?\.\w+$")
COMPOSITE_RE = re.compile(r"^[a-z]+_rgb_(.+?)(_map)?\.\w+$")
EMWIN_RE = re.compile(r"^[A-Z]_(\w{6})(\w{4})\d{6}(?:\w{3})?_C_\w{4}_(\d{14})_\d+-\d+-(\w{8})\.\w+$", re.I)
NWS_RE = re.compile(r"^(\d{14})-(.+?)(?:_latest\w*)?(?:\.lrit)?\.\w+$", re.I)

BANDS = {
    "G": {1: "Blue", 2: "Red visible", 3: "Veggie", 4: "Cirrus", 5: "Snow/ice", 6: "Cloud particle size",
          7: "Shortwave IR", 8: "Upper-level water vapor", 9: "Mid-level water vapor",
          10: "Lower-level water vapor", 11: "Cloud-top phase", 12: "Ozone", 13: "Clean longwave IR",
          14: "Longwave IR", 15: "Dirty longwave IR", 16: "CO2 longwave IR"},
    "H": {3: "Red visible", 7: "Shortwave IR", 8: "Upper-level water vapor", 9: "Mid-level water vapor",
          13: "Clean longwave IR", 14: "Longwave IR", 15: "Dirty longwave IR"},
}
L2_CODES = {"ACHA": "Cloud top height", "ACHT": "Cloud top temperature", "DSI": "Stability indices",
            "LST": "Land surface temperature", "SST": "Sea surface temperature",
            "TPW": "Total precipitable water", "RRQPE": "Rainfall rate"}
EMWIN_CODES = {
    "AFD": "Area forecast discussion", "AFM": "Area forecast matrices", "AIR": "Aviation reports",
    "CF6": "Monthly climate data", "CLI": "Daily climate report", "CWF": "Coastal waters forecast",
    "FFA": "Flood watch", "FFS": "Flash flood statement", "FFW": "Flash flood warning",
    "FLS": "Flood statement", "FLW": "Flood warning", "FWF": "Fire weather forecast",
    "FWS": "Spot fire forecast", "HLS": "Hurricane local statement", "HWO": "Hazardous weather outlook",
    "HYD": "Hydrometeorological data", "LSR": "Local storm report", "MIS": "Miscellaneous",
    "MWS": "Marine weather statement", "MWW": "Marine weather message", "NOW": "Short-term forecast",
    "NPW": "Non-precipitation warning", "NSH": "Nearshore marine forecast", "OBS": "Observations",
    "OFF": "Offshore waters forecast", "OMR": "Marine reports", "PFM": "Point forecast matrices",
    "PNS": "Public information statement", "REC": "Recreation forecast", "RER": "Record event report",
    "RFW": "Red flag warning", "RRM": "Rainfall and hydromet data", "RTP": "Regional temperature and precip",
    "RVA": "River summary", "RVD": "Daily river forecast", "RVF": "River forecast",
    "RVM": "River data", "RVS": "River statement", "RWR": "Regional weather roundup",
    "SAH": "Hourly surface observations", "SFP": "State forecast", "SFT": "State forecast table",
    "SMW": "Special marine warning", "SPS": "Special weather statement", "SRF": "Surf zone forecast",
    "SVR": "Severe thunderstorm warning", "SVS": "Severe weather statement", "TAF": "Terminal forecast",
    "TCD": "Tropical cyclone discussion", "TCP": "Tropical cyclone advisory", "TID": "Tide data",
    "TOR": "Tornado warning", "TST": "Tsunami message", "TSU": "Tsunami warning",
    "TWO": "Tropical weather outlook", "WSW": "Winter storm message", "ZFP": "Zone forecast",
}


class Spec:
    """What a file is: where it sits in the interface and when its data is from."""
    __slots__ = ("section", "group", "label", "kind", "rank", "time", "is_map", "name", "slot", "cheap", "raw")

    def __init__(self, section, group, label, kind, rank=5.0, time=None, is_map=False, name=None, slot=None,
                 cheap=False, raw=False):
        self.section, self.group, self.label, self.kind, self.rank = section, group, label, kind, rank
        self.time, self.is_map, self.name, self.slot, self.cheap = time, is_map, name, slot, cheap
        self.raw = raw  # an instrument channel or data field as received, not a rendered composite


def stamp(text, fmt):
    try:
        return calendar.timegm(time.strptime(text, fmt))
    except ValueError:
        return None


def classify(parts):
    """Map path components (relative to the data root) to a Spec, or None to ignore."""
    anchor = next((i for i, p in enumerate(parts[:-1]) if p in ANCHORS), None)
    if anchor is None:
        return None
    top, inner, fname = parts[anchor], list(parts[anchor + 1:-1]), parts[-1]
    stem, _, ext = fname.rpartition(".")
    ext = ext.lower()
    if ext in IMAGE_EXT:
        kind = "image"
    elif ext in TEXT_EXT:
        kind = "text"
    else:
        return None

    slot = when = None
    if inner and SLOT_RE.match(inner[-1]):
        slot = inner.pop()
        when = stamp(slot, "%Y-%m-%d_%H-%M-%S")

    if top == "EMWIN":
        m = EMWIN_RE.match(fname)
        if not m:
            return Spec("text" if kind == "text" else "weather", "EMWIN", "Other", kind, name=stem)
        wmo, office, issued, ident = m.group(1).upper(), m.group(2).upper(), m.group(3), m.group(4).upper()
        when = stamp(issued, "%Y%m%d%H%M%S")
        if kind == "image":
            return Spec("weather", "EMWIN graphics", ident, kind, time=when, cheap=True)
        code = ident[:3]
        label = f"{code} · {EMWIN_CODES[code]}" if code in EMWIN_CODES else code
        return Spec("text", "EMWIN", label, kind, time=when, name=f"{ident} · {wmo} {office}", cheap=True)

    if top == "IMAGES" and inner[:1] == ["NWS"]:
        m = NWS_RE.match(fname)
        if m:
            return Spec("weather", "NWS charts", m.group(2), kind, time=stamp(m.group(1), "%Y%m%d%H%M%S"))
        return Spec("weather", "NWS charts", stem, kind)

    if top in ("IMAGES", "L2") and kind == "image":
        section = "imagery" if top == "IMAGES" else "weather"
        group = " · ".join(inner) or top
        if top == "L2":
            group += " · Level 2"
        m = COMPOSITE_RE.match(fname)
        if m:
            label = m.group(1).replace("_", " ")
            return Spec(section, group, label, kind, 1.0 if "False Color" in label else 2.0,
                        when, bool(m.group(2)), slot=slot)
        m = BAND_RE.match(fname)
        if m and m.group(2).isdigit():
            band = int(m.group(2))
            desc = BANDS[m.group(1)[0]].get(band)
            label = f"Band {band} · {desc}" if desc else f"Band {band}"
            return Spec(section, group, label, kind, 3.0 + band / 100, when, slot=slot, raw=True)
        if m:
            code = m.group(2)
            return Spec(section, group, f"{code} · {L2_CODES.get(code, 'data')} (raw)", kind, 4.0, when,
                        slot=slot, raw=True)
        return Spec(section, group, stem if slot else "Other images", kind, 5.0, when, slot=slot)

    return Spec("text" if kind == "text" else "weather", top, top, kind, name=stem)


# -------------------------------------------------------------------- catalog

def loose_twin(vpath):
    """Where an archived bulletin ("z/<run>/EMWIN-archive/DAY.zip/NAME") sat as a loose file."""
    head, _, name = vpath[2:].rpartition("/")
    run = head.rsplit("/", 2)[0] + "/" if head.count("/") >= 2 else ""
    return f"s/{run}EMWIN/{name}"


class Item:
    __slots__ = ("id", "path", "kind", "type", "time", "recv", "size", "map", "note", "name")

    def as_dict(self):
        d = {"id": self.id, "p": self.path, "k": self.kind[0], "ty": self.type.id,
             "t": int(self.time), "r": int(self.recv)}
        if self.size:
            d["s"] = self.size
        if self.map:
            d["m"] = self.map
        if self.note:
            d["n"] = self.note
        if self.name:
            d["nm"] = self.name
        return d


class Type:
    __slots__ = ("id", "section", "group", "label", "kind", "rank", "raw", "items", "arrivals")

    def __init__(self, spec):
        self.id = f"{spec.section}/{spec.group}/{spec.label}"
        self.section, self.group, self.label = spec.section, spec.group, spec.label
        self.kind, self.rank, self.raw = spec.kind, spec.rank, spec.raw
        self.items = []     # by data time, for timelines
        self.arrivals = []  # by arrival, for feeds

    def as_dict(self):
        return {"id": self.id, "sec": self.section, "grp": self.group, "label": self.label,
                "kind": self.kind[0], "raw": int(self.raw), "n": len(self.items),
                "last": self.items[-1].as_dict() if self.items else None}


class DirState:
    __slots__ = ("mtime", "known", "pending")

    def __init__(self):
        self.mtime, self.known, self.pending = None, set(), False


class Catalog:
    """Everything received so far, kept current by polling directory mtimes."""

    def __init__(self, root, derived_root, settle=4.0):
        self.root = os.path.realpath(root)
        self.derived_root = os.path.realpath(derived_root)
        self.settle = settle
        self.lock = threading.RLock()
        self.items = []
        self.types = {}
        self.by_path = {}
        self.pending_maps = {}
        self.slots = {}  # (section, group) -> {slot name: (dir relative to root, set of file names)}
        self.dirs = {self.root: DirState()}
        self.zips = {}                               # open bulletin archives: path -> (mtime, ZipFile)
        self.zip_lock = threading.Lock()
        self.ids = {}                                # item id -> item, for feed cursors
        self.recent = collections.deque(maxlen=400)  # latest live arrivals, replayed to reconnecting tabs
        self.subscribers = set()
        self.listeners = []
        self.tick = 0
        self.last_id = 0
        self._staging = None

    # -- paths

    def resolve(self, vpath):
        """Virtual path ("s/..." source, "d/..." derived) to a real file, or None."""
        prefix, _, rel = vpath.partition("/")
        base = {"s": self.root, "d": self.derived_root}.get(prefix)
        rel = posixpath.normpath(rel)
        if base is None or rel.startswith(("/", "..")) or rel == ".":
            return None
        full = os.path.realpath(os.path.join(base, *rel.split("/")))
        return full if full.startswith(base + os.sep) else None

    def archive_of(self, vpath):
        """("z/.../EMWIN-archive/DAY.zip/NAME") -> (real path of the zip, member name), or (None, None)."""
        cut = vpath.find(".zip/")
        if not vpath.startswith("z/") or cut < 0:
            return None, None
        rel, member = posixpath.normpath(vpath[2:cut + 4]), vpath[cut + 5:]
        parts = rel.split("/")
        if rel.startswith(("/", "..")) or len(parts) < 2 or parts[-2] != ARCHIVE_DIR or not DAY_ZIP_RE.match(parts[-1]):
            return None, None
        full = os.path.realpath(os.path.join(self.root, *parts))
        return (full, member) if full.startswith(self.root + os.sep) else (None, None)

    def read_bytes(self, vpath, limit=None):
        """The contents of a text item, whether it is a loose file or packed in a day's archive."""
        try:
            if vpath.startswith("z/"):
                path, member = self.archive_of(vpath)
                if path is None:
                    return None
                mtime = os.stat(path).st_mtime_ns
                with self.zip_lock:  # one reader at a time per process is plenty for small bulletins
                    held = self.zips.get(path)
                    if held is None or held[0] != mtime:
                        if held:
                            held[1].close()
                        while len(self.zips) >= 12:
                            self.zips.pop(next(iter(self.zips)))[1].close()
                        held = self.zips[path] = (mtime, zipfile.ZipFile(path))
                    data = held[1].read(member)
                return data if limit is None else data[:limit]
            path = self.resolve(vpath)
            if path is None:
                return None
            with open(path, "rb") as f:
                return f.read() if limit is None else f.read(limit)
        except (OSError, KeyError, zipfile.BadZipFile):
            return None

    def repath(self, moves):
        """Bulletins were packed: point their items at the archive. Same items, same ids."""
        with self.lock:
            doubled = set()
            for old, new in moves:
                item = self.by_path.get(old)
                if item is None:
                    continue
                if new in self.by_path:  # already listed from the zip (a pack that was interrupted)
                    doubled.add(old)
                    continue
                del self.by_path[old]
                item.path = new
                self.by_path[new] = item
            if doubled:
                self.forget(set(), doubled)

    def members(self, prefix):
        with self.lock:
            return [v for v in self.by_path if v.startswith(prefix)]

    def _load_archive(self, path):
        """List a day's archive: every bulletin in it becomes an item, as if still a loose file."""
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            log(f"archive: cannot read {path}: {exc}")
            return True
        for name in names:
            vpath = f"z/{rel}/{name}"
            loose = loose_twin(vpath)
            spec = classify(loose[2:].split("/"))
            if spec is None or spec.time is None:
                continue
            entry = (vpath, spec, spec.time, spec.time, None, None)
            if self._staging is not None:
                self._staging.append(entry)
            else:
                with self.lock:
                    if loose not in self.by_path:  # else the archiver is about to repoint that item
                        self._commit(*entry, live=False)
        return True

    # -- scanning

    def scan(self):
        """First pass: read everything, then number items in arrival order."""
        started = time.time()
        self._staging = []
        self._rescan(self.root, 0)
        staged, self._staging = self._staging, None
        # A pack that was interrupted can leave a bulletin both loose and archived: list it once.
        loose = {entry[0] for entry in staged if entry[0].startswith("s/")}
        staged = [e for e in staged if not (e[0].startswith("z/") and loose_twin(e[0]) in loose)]
        staged.sort(key=lambda entry: entry[3])
        with self.lock:
            for entry in staged:
                self._commit(*entry, live=False)
        log(f"catalog: {len(self.items)} items in {len(self.types)} types, "
            f"{len(self.dirs)} folders, {time.time() - started:.1f}s")

    def poll(self):
        """Look for new files. Old timestamp folders are checked only now and then."""
        self.tick += 1
        now = time.time()
        for path in list(self.dirs):
            state = self.dirs.get(path)
            if state is None:  # pruned since the loop began
                continue
            if (self.tick % 40 and not state.pending and state.mtime
                    and now - state.mtime / 1e9 > 7200 and SLOT_RE.match(os.path.basename(path))):
                continue
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue
            if mtime != state.mtime or state.pending:
                self._rescan(path, path[len(self.root):].count(os.sep), mtime)

    def _rescan(self, path, depth, mtime=None):
        try:
            if mtime is None:
                mtime = os.stat(path).st_mtime_ns
            entries = list(os.scandir(path))
        except OSError:
            return
        state = self.dirs.setdefault(path, DirState())
        state.pending = False
        now = time.time()
        for entry in entries:
            name = entry.name
            if name in state.known:
                continue
            if entry.is_dir(follow_symlinks=False):
                state.known.add(name)
                if not name.startswith(".") and depth < 8 and entry.path != self.derived_root:
                    self._rescan(entry.path, depth + 1)
            elif self._consider(entry, now):
                state.known.add(name)
            else:
                state.pending = True  # still being written; look again next poll
        state.mtime = mtime

    def _consider(self, entry, now):
        if DAY_ZIP_RE.match(entry.name) and os.path.basename(os.path.dirname(entry.path)) == ARCHIVE_DIR:
            return self._load_archive(entry.path)
        rel = os.path.relpath(entry.path, self.root).replace(os.sep, "/")
        spec = classify(rel.split("/"))
        if spec is None:
            return True
        if self._staging is not None and spec.cheap and spec.time is not None:
            recv, size = spec.time, None  # first pass over a huge flat folder: skip the stat
        else:
            try:
                st = entry.stat()
            except OSError:
                return True
            if now - st.st_mtime < self.settle:
                return False
            recv, size = st.st_mtime, st.st_size
        entry_tuple = ("s/" + rel, spec, spec.time if spec.time is not None else recv, recv, size, None)
        if self._staging is not None:
            self._staging.append(entry_tuple)
        else:
            with self.lock:
                self._commit(*entry_tuple, live=True)
        return True

    def _commit(self, vpath, spec, when, recv, size, note, live):
        if vpath in self.by_path:
            return None
        if spec.slot and vpath.startswith("s/"):
            folder, fname = posixpath.split(vpath[2:])
            slots = self.slots.setdefault((spec.section, spec.group), {})
            slots.setdefault(spec.slot, (folder, set()))[1].add(fname)
        if spec.is_map:
            base = re.sub(r"_map(\.\w+)$", r"\1", vpath)
            if base in self.by_path:
                self.by_path[base].map = vpath
            else:
                self.pending_maps[base] = vpath
            self.by_path[vpath] = None
            return None
        kind = self.types.get(f"{spec.section}/{spec.group}/{spec.label}")
        if kind is None:
            kind = Type(spec)
            self.types[kind.id] = kind
        item = Item()
        self.last_id += 1
        item.id, item.path, item.kind, item.type = self.last_id, vpath, spec.kind, kind
        item.time, item.recv, item.size, item.note, item.name = when, recv, size, note, spec.name
        item.map = self.pending_maps.pop(vpath, None)
        # Feeds run in arrival order. Almost everything is appended; a colour frame
        # registered late (after a restart, or built for an old slot) slots in by time.
        arrival = lambda i: (i.recv, i.id)
        for feed in (self.items, kind.arrivals):
            if not feed or arrival(feed[-1]) <= arrival(item):
                feed.append(item)
            else:
                bisect.insort(feed, item, key=arrival)
        self.by_path[vpath] = item
        self.ids[item.id] = item
        bisect.insort(kind.items, item, key=lambda i: (i.time, i.id))
        if live:
            self.recent.append(item)
            self.publish("item", item.as_dict(), item.id)
        for listener in self.listeners:
            listener(item, live)
        return item

    def add_derived(self, vpath, spec, when, recv, size, note, live):
        with self.lock:
            return self._commit(vpath, spec, when, recv, size, note, live=live)

    # -- live events

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def publish(self, event, data, event_id=None):
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait((event, data, event_id))
                except queue.Full:
                    self.subscribers.discard(q)  # stalled client; it will reconnect

    # -- queries

    def type_list(self):
        def order(k):  # satellite products first, EMWIN's many graphics last
            lead = 0 if "Level 2" in k.group else 2 if k.group.startswith("EMWIN") else 1
            return (k.section, lead, k.group, k.rank, k.label)
        with self.lock:
            kinds = sorted(self.types.values(), key=order)
            return [k.as_dict() for k in kinds]

    def feed(self, section=None, type_id=None, kind=None, before=None, limit=50, query=None, raw=None, group=None):
        with self.lock:
            source = self.types[type_id].arrivals if type_id in self.types else ([] if type_id else self.items)
            hi = len(source)
            anchor = self.ids.get(before) if before else None
            if anchor:  # continue from just before the last item the page was given
                hi = bisect.bisect_left(source, (anchor.recv, anchor.id), key=lambda i: (i.recv, i.id))
            query = query.lower() if query else None
            out = []
            for i in range(hi - 1, -1, -1):
                item = source[i]
                if section and item.type.section != section:
                    continue
                if kind and item.kind != kind:
                    continue
                if raw is not None and item.type.raw != raw:
                    continue
                if group and item.type.group != group:
                    continue
                if query and query not in (item.name or item.type.label).lower():
                    continue
                out.append(item)
                if len(out) > limit:
                    break
            return out[:limit], len(out) > limit

    def search_text(self, index, text, type_id=None, before=None, limit=30):
        """Full-text matches newest first, as ([(item, excerpt)], cursor, more)."""
        match, words, phrases = parse_query(text)
        out, cursor, more = [], before, False
        if not match:
            return out, cursor, more
        for _ in range(25):  # filters and phrase checks may discard most candidates; look further, within reason
            rows = index.search(match, cursor, 200)
            for rowid, path in rows:
                with self.lock:
                    item = self.by_path.get(path) if path else None
                if item is not None and (not type_id or item.type.id == type_id):
                    body = index.text_of(item.path)
                    excerpt = passage(body, words, phrases) if body is not None else None
                    if excerpt is not None:  # None: a quoted phrase is not actually in this one
                        if len(out) == limit:
                            return out, cursor, True
                        out.append((item, excerpt))
                cursor = rowid
            more = len(rows) == 200
            if not more:
                break
        return out, cursor, more

    def since(self, last_id, limit=300):
        with self.lock:
            return [i.as_dict() for i in self.recent if i.id > last_id][-limit:]

    def forget(self, dead_dirs, dead_files):
        """Drop everything in the given timestamp folders (paths relative to the
        root) and the given individual files (virtual paths). Returns the count."""
        def gone(vpath):
            return vpath in dead_files or posixpath.dirname(vpath)[2:] in dead_dirs
        with self.lock:
            dead = {i.id for i in self.items if gone(i.path)}
            self.items = [i for i in self.items if i.id not in dead]
            self.recent = collections.deque((i for i in self.recent if i.id not in dead), maxlen=self.recent.maxlen)
            for item_id in dead:
                self.ids.pop(item_id, None)
            for kind in list(self.types.values()):
                kind.items = [i for i in kind.items if i.id not in dead]
                kind.arrivals = [i for i in kind.arrivals if i.id not in dead]
                if not kind.items:
                    del self.types[kind.id]
            for table in (self.by_path, self.pending_maps):
                for vpath in [v for v in table if gone(v)]:
                    del table[vpath]
            for slots in self.slots.values():
                for name in [n for n, (folder, _) in slots.items() if folder in dead_dirs]:
                    del slots[name]
            # let the scanner notice if SatDump ever writes these names again
            for rel in list(dead_dirs) + [v[2:] for v in dead_files]:
                full = os.path.join(self.root, *rel.split("/"))
                self.dirs.pop(full, None)
                parent = self.dirs.get(os.path.dirname(full))
                if parent:
                    parent.known.discard(os.path.basename(full))
            return len(dead)

    def timeline(self, type_id):
        with self.lock:
            kind = self.types.get(type_id)
            if kind is None:
                return None
            frames = [[int(i.time), i.path, i.map or 0, i.note or 0, i.size or 0] for i in kind.items]
            return {"type": kind.as_dict(), "frames": frames}

    def slot_snapshot(self, section="imagery"):
        with self.lock:
            return [(group, {name: (folder, set(files)) for name, (folder, files) in slots.items()})
                    for (sec, group), slots in self.slots.items() if sec == section]

    def stats(self):
        now = time.time()
        with self.lock:
            rate, total, last = {}, {}, {}
            for kind in self.types.values():
                total[kind.section] = total.get(kind.section, 0) + len(kind.items)
                if kind.arrivals:  # newest arrival per section, however long ago
                    last[kind.section] = max(last.get(kind.section, 0), int(kind.arrivals[-1].recv))
            for item in reversed(self.items):
                age = now - item.recv
                if age >= 3600:
                    break
                sec = item.type.section
                rate.setdefault(sec, [0] * 60)[59 - max(0, int(age // 60))] += 1
            scans = []
            for (sec, group), slots in sorted(self.slots.items()):
                if sec != "imagery" or "Full Disk" not in group:
                    continue
                names = sorted(slots)
                horizon = (stamp(names[-1], "%Y-%m-%d_%H-%M-%S") or 0) - 86400 if names else 0
                recent = []
                for name in names:  # the day leading up to the newest scan
                    when = stamp(name, "%Y-%m-%d_%H-%M-%S")
                    if when is None or when < horizon:
                        continue
                    bands = sorted({int(m.group(2)) for f in slots[name][1]
                                    if (m := BAND_RE.match(f)) and m.group(2).isdigit()})
                    recent.append({"t": when, "b": bands})
                scans.append({"g": group, "slots": recent})
            return {"now": int(now), "items": len(self.items), "total": total,
                    "rate": rate, "last": last, "scans": scans}


# ------------------------------------------------------------------- previews

class Previews:
    """Downscaled JPEG copies, made on demand and kept on disk."""
    SIZES = (320, 960, 1920)

    def __init__(self, catalog, folder, workers):
        self.catalog = catalog
        self.folder = folder
        self.gate = threading.Semaphore(workers)
        self.locks = {}
        self.locks_guard = threading.Lock()
        self.broken = set()
        self.jobs = queue.PriorityQueue()

    def _cache_path(self, vpath, st, size):
        key = hashlib.sha1(f"{vpath}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
        return os.path.join(self.folder, str(size), key[:2], key[2:] + ".jpg")

    def cached(self, vpath, st):
        """Every cache file that may exist for a source file with this stat."""
        return [self._cache_path(vpath, st, size) for size in self.SIZES]

    def get(self, vpath, size):
        """Path of the cached preview, generating it (and any smaller ones) if needed."""
        source = self.catalog.resolve(vpath)
        if Image is None or source is None or size not in self.SIZES:
            return None
        try:
            st = os.stat(source)
        except OSError:
            return None
        target = self._cache_path(vpath, st, size)
        if os.path.exists(target):
            return target
        if target in self.broken:
            return None
        with self.locks_guard:
            lock = self.locks.setdefault(vpath, threading.Lock())
        with lock:
            if not os.path.exists(target):
                with self.gate:
                    self._render(source, {s: self._cache_path(vpath, st, s) for s in self.SIZES if s <= size})
        with self.locks_guard:
            self.locks.pop(vpath, None)
        return target if os.path.exists(target) else None

    def _render(self, source, targets):
        try:
            with Image.open(source) as im:
                im.draft("RGB", (max(targets), max(targets)))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    flat = Image.new("RGB", im.size, (0, 0, 0))
                    flat.paste(im, mask=im.getchannel("A"))
                    im = flat
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                for size in sorted(targets, reverse=True):
                    if os.path.exists(targets[size]):
                        continue
                    im.thumbnail((size, size), Image.LANCZOS, reducing_gap=2.0)
                    os.makedirs(os.path.dirname(targets[size]), exist_ok=True)
                    tmp = f"{targets[size]}.{threading.get_ident()}.tmp"
                    im.save(tmp, "JPEG", quality=84)
                    os.replace(tmp, targets[size])
        except Exception as exc:  # an undecodable file must not take the server down
            self.broken.update(targets.values())
            log(f"preview failed for {source}: {exc}")

    # -- background warm-up so the wall and timelines are instant

    def enqueue(self, item, live):
        if item.kind == "image":
            self.jobs.put((0 if live else 1, -item.recv, item.path))

    def warm_forever(self, max_age):
        while True:
            _, neg_recv, vpath = self.jobs.get()
            if time.time() + neg_recv <= max_age:
                self.get(vpath, 960)


# --------------------------------------------------------------- false colour

class Colourist(threading.Thread):
    """Builds a false-colour frame for every GOES slot, pairing split bands."""

    def __init__(self, catalog, lut_name, lut_path, curve_path, max_age):
        super().__init__(daemon=True, name="colourist")
        self.catalog, self.lut_name, self.max_age = catalog, lut_name, max_age
        table = fc.lut_table(lut_path, curve_path)
        self.rgb = np.ascontiguousarray(table[..., :3])
        self.rgb[255, 0] = 0  # space
        self.failed = set()
        self.wake = threading.Event()
        catalog.listeners.append(lambda item, live: live and item.kind == "image" and self.wake.set())

    def run(self):
        while True:
            try:
                busy = self.build_next()
            except Exception:
                traceback.print_exc()
                busy = False
            self.wake.wait(1 if busy else 30)  # a new band arriving wakes it at once
            self.wake.clear()

    def build_next(self):
        """Register finished frames and build at most one new one (newest first)."""
        todo = []
        for group, slots in self.catalog.slot_snapshot():
            listing = [fc.Slot(name, files) for name, (_, files) in sorted(slots.items())]
            if not any(fc.VISIBLE in s.bands for s in listing):
                continue
            window = timedelta(seconds=90) if "Meso" in group else timedelta(minutes=11)
            for target, vis, ir in fc.find_pairs(listing, window)[0]:
                todo.append((target.name, group, slots, target, vis, ir))
        todo.sort(key=lambda job: job[0], reverse=True)

        now = time.time()
        for _, group, slots, target, vis, ir in todo:
            rel = f"{slots[target.name][0]}/False_Color_{self.lut_name}.jpg"
            vpath = "d/" + rel
            if vpath in self.catalog.by_path or vpath in self.failed:
                continue
            when = stamp(target.name, "%Y-%m-%d_%H-%M-%S")
            out = os.path.join(self.catalog.derived_root, *rel.split("/"))
            built = False
            if not os.path.exists(out):
                if now - when > self.max_age:
                    continue
                if not self.render(slots, vis, ir, out):
                    self.failed.add(vpath)
                    continue
                built = True
            note = None
            if vis is not ir:
                odd, band = (vis, 2) if ir is target else (ir, 13)
                note = f"band {band} from the {odd.name[11:16].replace('-', ':')} scan"
            st = os.stat(out)
            spec = Spec("imagery", group, f"False Color · {self.lut_name}", "image", 0.0, when)
            # A colour frame "arrives" when the later of its two bands did, wherever and
            # whenever it was actually built, so it sits beside them in the feeds.
            bands = [self.catalog.by_path.get(f"s/{slots[s.name][0]}/{s.bands[b]}")
                     for s, b in ((vis, fc.VISIBLE), (ir, fc.INFRARED))]
            arrived = max((item.recv for item in bands if item), default=st.st_mtime)
            # Only announce it to open pages as a new arrival if it really is one.
            self.catalog.add_derived(vpath, spec, when, arrived, st.st_size, note, live=now - arrived < 900)
            if built:
                log(f"false colour: {group} {target.name}" + (f" ({note})" if note else ""))
                return True
        return False

    def render(self, slots, vis, ir, out):
        def band(slot, number):
            return Image.open(self.catalog.resolve(f"s/{slots[slot.name][0]}/{slot.bands[number]}"))
        try:
            with band(vis, fc.VISIBLE) as vis_image, band(ir, fc.INFRARED) as ir_image:
                v, i = fc.load_pair(vis_image, ir_image)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            Image.fromarray(self.rgb[v, i]).save(out + ".tmp", "JPEG", quality=90)
            os.replace(out + ".tmp", out)
            return True
        except Exception as exc:
            log(f"false colour failed for {out}: {exc}")
            return False


# ------------------------------------------------------------------ telemetry

def flatten(value, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(value, dict):
        for key, inner in value.items():
            flatten(inner, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, (int, float, str, bool)) and len(out) < 60:
        out[prefix] = round(value, 4) if isinstance(value, float) else value
    return out


def poll_satdump(url, state):
    """Mirror SatDump's own HTTP stats (satdump live ... --http_server host:port)."""
    while True:
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                state["satdump"] = flatten(json.load(response))
        except Exception:
            state.pop("satdump", None)
        time.sleep(2)


# ---------------------------------------------------------------- text search

WORD = "0-9A-Za-z"


def parse_query(text):
    """What someone typed, as (FTS5 query, words, phrases).

    Every word must appear, matching from the start of a word. A "quoted phrase" must
    appear exactly; the index only knows which bulletins hold each word, so it narrows
    the field to those holding all the phrase's words and the phrase itself is then
    checked against each bulletin's text.
    """
    words, phrases, match = [], [], []
    for quoted, bare in re.findall(r'"([^"]+)"|(\S+)', text):
        parts = re.sub(rf"[^{WORD}]+", " ", quoted or bare).split()
        if quoted and parts:
            phrases.append(parts)
            match += [f'"{w}"' for w in parts]
        else:
            words += parts
            match += [f'"{w}"*' for w in parts]
    return " ".join(match), words, phrases


def passage(text, words, phrases):
    """The stretch of `text` around the first match, with every match marked off by
    \x02 and \x03. None if a quoted phrase is missing; "" if nothing in the text matched
    (the hit was on the product or office code)."""
    sep = f"[^{WORD}]+"
    exact = [rf"(?<![{WORD}]){sep.join(map(re.escape, p))}(?![{WORD}])" for p in phrases]
    if any(not re.search(pattern, text, re.I) for pattern in exact):
        return None
    patterns = exact + [rf"(?<![{WORD}]){re.escape(w)}[{WORD}]*" for w in words]
    if not patterns:
        return ""
    finder = re.compile("|".join(patterns), re.I)
    first = finder.search(text)
    if not first:
        return ""
    start, end = max(0, first.start() - 90), min(len(text), first.start() + 200)
    if start:
        start = text.find(" ", start) + 1 or start
    if end < len(text):
        end = text.rfind(" ", start, end) if text.rfind(" ", start, end) > first.end() else end
    marked = finder.sub(lambda m: f"\x02{m.group(0)}\x03", text[start:end])
    return ("… " if start else "") + marked.strip() + (" …" if end < len(text) else "")


class TextIndex(threading.Thread):
    """Full-text search over the text bulletins: an SQLite FTS5 index in the cache folder.

    The index records only which bulletins contain which words: no copy of the text and
    no word positions, which makes it a few percent of the size of the bulletins.
    Excerpts and phrase checks read the bulletin itself. Row ids encode arrival time,
    so "newest first" is the index's natural order.

    An index like this cannot forget a single bulletin, so deleting one only removes it
    from the `files` table and its words stay behind, unreachable. When enough of those
    have built up the whole index is rebuilt in the background, which is quick.
    """
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, id INTEGER UNIQUE);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(name, body, content='', detail=none);
    """
    LIMIT = 100_000  # bytes of a bulletin that are indexed; a few data tables run far longer

    def __init__(self, catalog, folder):
        super().__init__(daemon=True, name="text-index")
        self.catalog = catalog
        self.path = os.path.join(folder, "text-index-2.sqlite3")
        for stale in ("text-index.sqlite3", "text-index.sqlite3-wal", "text-index.sqlite3-shm"):
            try:  # the first design kept a second copy of every bulletin; it is not migrated, just rebuilt
                os.unlink(os.path.join(folder, stale))
            except OSError:
                pass
        self.jobs = queue.Queue()
        self.total = self.done = 0  # progress of the catch-up after start
        self.ready = False
        self.indexed = self.dropped = 0
        db = self.connect()
        db.executescript(self.SCHEMA)  # raises here if this SQLite has no FTS5
        db.close()
        catalog.listeners.append(self.on_item)

    def connect(self, path=None):
        db = sqlite3.connect(path or self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")  # readers keep working while it writes
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass  # some filesystems cannot do WAL; the default journal still works
        return db

    def on_item(self, item, live):
        if live and item.kind == "text":
            self.jobs.put(("add", item.path, item.recv, item.name or ""))

    def forget(self, paths):
        self.jobs.put(("drop", [p for p in paths if p.lower().endswith(".txt")]))

    def text_of(self, path):
        raw = self.catalog.read_bytes(path, self.LIMIT)
        return None if raw is None else CONTROL_RE.sub(" ", raw.decode("latin-1"))

    # -- writing: one thread owns it

    def run(self):
        db = self.connect()
        self.indexed = (db.execute("SELECT value FROM meta WHERE key = 'indexed'").fetchone() or [0])[0]
        self.dropped = self.indexed - db.execute("SELECT count(*) FROM files").fetchone()[0]
        try:
            self.catch_up(db)
        except Exception:
            traceback.print_exc()
        self.ready = True
        while True:
            self.drain(db, block=True)
            if self.dropped > max(5000, self.indexed * 0.3):
                try:
                    db = self.rebuild(db)
                except Exception:
                    traceback.print_exc()
                    self.dropped = 0  # do not retry in a tight loop

    def save(self, db):
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('indexed', ?)", (self.indexed,))
        db.commit()

    def drain(self, db, block=False):
        try:
            job = self.jobs.get(block=block)
        except queue.Empty:
            return
        for _ in range(500):
            try:
                if job[0] == "add":
                    self.add(db, *job[1:])
                elif job[0] == "move":
                    for old, new in job[1]:
                        try:
                            db.execute("UPDATE files SET path = ? WHERE path = ?", (new, old))
                        except sqlite3.IntegrityError:
                            self.drop(db, old)  # the new path is already indexed
                else:
                    for path in job[1]:
                        self.drop(db, path)
            except Exception as exc:
                log(f"text index: {exc}")
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
        self.save(db)

    def fill(self, db, items):
        """Index catalog items, staying out of the way of everything else.

        Each item's path is read at the moment it is indexed, not when the list was made:
        the archiver may pack a bulletin into its day's zip in between, and it must then
        be read from, and filed under, where it is now.
        """
        for start in range(0, len(items), 500):
            for item in items[start:start + 500]:
                for _ in range(3):  # moved while being read: go again with its new path
                    path = item.path
                    if self.add(db, path, item.recv, item.name or "") or item.path == path:
                        break
            self.save(db)
            self.done = min(len(items), start + 500)
            time.sleep(0.02)
        db.execute("INSERT INTO docs (docs) VALUES ('optimize')")  # merge the index into its compact form
        db.commit()

    def catch_up(self, db):
        """Index what arrived while GOESview was not running, newest first, and forget
        what has since been deleted."""
        known = {row[0] for row in db.execute("SELECT path FROM files")}
        with self.catalog.lock:
            texts = [i for i in self.catalog.items if i.kind == "text"]
            present = {i.path for i in texts}
            todo = [i for i in reversed(texts) if i.path not in known]
        for path in known - present:
            self.drop(db, path)
        self.save(db)
        self.total = len(todo)
        if todo:
            log(f"text index: adding {len(todo)} bulletins")
            started = time.time()
            self.fill(db, todo)
            log(f"text index: caught up in {time.time() - started:.0f}s, "
                f"{human(os.path.getsize(self.path))} for {self.indexed - self.dropped} bulletins")

    def rebuild(self, db):
        """Start again from the bulletins that still exist, then swap the new index in."""
        log(f"text index: rebuilding to shed {self.dropped} deleted bulletins")
        started = time.time()
        fresh_path = self.path + ".new"
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(fresh_path + ext):
                os.unlink(fresh_path + ext)
        fresh = self.connect(fresh_path)
        fresh.executescript(self.SCHEMA)
        with self.catalog.lock:
            texts = [i for i in reversed(self.catalog.items) if i.kind == "text"]
        self.indexed = self.dropped = 0
        self.fill(fresh, texts)
        fresh.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        fresh.close()
        db.close()
        os.replace(fresh_path, self.path)  # searches open the file per query, so they just see the new one
        for ext in ("-wal", "-shm"):
            for leftover in (self.path + ext, fresh_path + ext):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
        log(f"text index: rebuilt in {time.time() - started:.0f}s, {human(os.path.getsize(self.path))}")
        return self.connect()

    def add(self, db, path, recv, name):
        """Index one bulletin. False if its text could not be read."""
        if db.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone():
            return True
        body = self.text_of(path)
        if body is None:
            return False
        # "AFDSEWWA · FXUS66 KSEW" also answers to its parts: AFD, SEW, WA
        ident = name.split(" ")[0] if name else ""
        keys = f"{name} {ident[:3]} {ident[3:6]} {ident[6:]}" if len(ident) == 8 else name
        rowid = int(recv) * 100_000 + zlib.crc32(path.encode()) % 100_000
        while True:  # bulletins landing in the same second share a prefix; step past any clash
            try:
                db.execute("INSERT INTO files (path, id) VALUES (?, ?)", (path, rowid))
                break
            except sqlite3.IntegrityError:
                rowid += 1
        db.execute("INSERT INTO docs (rowid, name, body) VALUES (?, ?, ?)", (rowid, keys, body))
        self.indexed += 1
        return True

    def drop(self, db, path):
        if db.execute("DELETE FROM files WHERE path = ?", (path,)).rowcount:
            self.dropped += 1

    # -- reading: any thread, its own connection

    def search(self, match, before=None, limit=200):
        """Bulletins holding every word, newest first, as (row id, path or None).
        A path of None is a deleted bulletin whose words are still in the index."""
        db = self.connect()
        try:
            ids = [row[0] for row in db.execute(
                "SELECT rowid FROM docs WHERE docs MATCH ?" + (" AND rowid < ?" if before else "")
                + " ORDER BY rowid DESC LIMIT ?", (match, before, limit) if before else (match, limit))]
            paths = dict(db.execute(f"SELECT id, path FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)) if ids else {}
            return [(rowid, paths.get(rowid)) for rowid in ids]
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()

    def progress(self):
        return {"ready": self.ready, "done": self.done, "total": self.total}


# ------------------------------------------------------------------ retention
#
# Two kinds of files, kept strictly apart:
#
#   cache     GOESview's own previews and false-colour frames. They are rebuilt from
#             SatDump's data on demand, so deleting them loses nothing. Governed by
#             the --cache-* options.
#   satdump   The data SatDump received. Once deleted it is gone. Only the --satdump-*
#             options ever touch it, and none of them has a default: unless one is
#             given, GOESview never deletes a single file SatDump wrote.

PRUNE_ANCHORS = ("IMAGES", "L2", "EMWIN")
PRUNE_FLOOR = 3 * 3600  # nothing newer than this is ever deleted, whatever the limits say
RUN_NAME_RE = re.compile(r"goes[_-]?hrit", re.I)  # SatDump names a run folder after its pipeline
GOES_DIR_RE = re.compile(r"^GOES-\d+$")


def goes_runs(root, protect=(), include=()):
    """Sort what is under `root` into (runs, protected, others).

    `runs` are the folders SatDump retention may delete from. A folder directly under
    the root qualifies only if it holds IMAGES/, L2/ or EMWIN/ and carries SatDump's
    own name for a GOES HRIT run ("..._goes_hrit_..."), or the user named it with
    --satdump-include. Everything else under the root (other satellites, other
    pipelines, folders you made yourself, loose files) lands in `others` and is never
    touched. `protected` are GOES runs the user excluded by name.

    If the root is itself one run folder, that folder is the only run; there a
    differently named folder also counts when it holds IMAGES/GOES-nn, since the
    user pointed straight at it.
    """
    def has_data(path):
        return any(os.path.isdir(os.path.join(path, anchor)) for anchor in PRUNE_ANCHORS)

    def holds_goes(path):
        try:
            return any(GOES_DIR_RE.match(e.name) and e.is_dir(follow_symlinks=False)
                       for e in os.scandir(os.path.join(path, "IMAGES")))
        except OSError:
            return False

    if has_data(root) and (RUN_NAME_RE.search(os.path.basename(root)) or holds_goes(root)):
        return [root], [], []
    runs, protected, others = [], [], []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return runs, protected, others
    for entry in entries:
        named = RUN_NAME_RE.search(entry.name) or entry.name in include
        if named and not entry.is_symlink() and entry.is_dir(follow_symlinks=False) and has_data(entry.path):
            (protected if entry.name in protect else runs).append(entry.path)
        elif not entry.name.startswith("."):
            others.append(entry.name)
    return runs, protected, others


def satdump_product(name):
    """Is this a file SatDump writes into a timestamp folder?"""
    return name == "product.cbor" or bool(BAND_RE.match(name) or COMPOSITE_RE.match(name))


def parse_age(text):
    """'36h', '14d', '2w', '3mo', '1y' to seconds; a bare number means days."""
    units = {"h": 3600, "d": 86400, "": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:(h|d|w|mo|y)[a-z]*)?\s*", text.lower())
    if not m or float(m.group(1)) * units[m.group(2) or ""] < PRUNE_FLOOR:
        raise argparse.ArgumentTypeError(f"{text!r}: give an age of at least 3h, such as 36h, 14d, 2w or 3mo")
    return float(m.group(1)) * units[m.group(2) or ""]


def parse_age_or_off(text):
    return None if text.strip().lower() in ("off", "none", "never", "0") else parse_age(text)


def parse_size(text):
    """'500M', '200G', '1.5T' to bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt])i?b?\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: give a size such as 500M, 200G or 1.5T")
    return int(float(m.group(1)) * 1024 ** " kmgt".index(m.group(2)))


def parse_cap(text):
    """'90%' to 0.9: how full the disk may get."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", text)
    if not m or not 1 <= float(m.group(1)) <= 99:
        raise argparse.ArgumentTypeError(f"{text!r}: give how full the disk may get, such as 90%")
    return float(m.group(1)) / 100


def count(n, noun):
    return f"{n:g} {noun}" + ("" if n == 1 else "s")


def human(size):
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Limits:
    """One set of limits: an age, a total size, and how full the disk may get."""

    def __init__(self, keep=None, max_size=None, disk_cap=None):
        self.keep, self.max_size, self.disk_cap = keep, max_size, disk_cap

    def __bool__(self):
        return bool(self.keep or self.max_size or self.disk_cap)

    def describe(self):
        parts = []
        if self.keep:
            parts.append(f"the last {count(round(self.keep / 86400, 2), 'day')}")
        if self.max_size:
            parts.append(f"at most {human(self.max_size)}")
        if self.disk_cap:
            parts.append(f"disk under {self.disk_cap:.0%} full")
        return "keeping " + ", ".join(parts)


def on_disk(st):
    """Space a file really occupies. On drives with large clusters a small file takes
    far more than its length, and that is what deleting it gives back."""
    return st.st_blocks * 512 if getattr(st, "st_blocks", 0) else st.st_size


def tree_size(folder):
    total = 0
    for base, _, names in os.walk(folder):
        for name in names:
            try:
                total += on_disk(os.lstat(os.path.join(base, name)))
            except OSError:
                pass
    return total


class Pruner(threading.Thread):
    """Deletes the oldest files to stay within the cache limits and, separately and
    only if asked, the SatDump limits. Always oldest first."""

    def __init__(self, app, cache, satdump, every, dry_run, protect=(), include=(), dcs_limits=None):
        super().__init__(daemon=True, name="pruner")
        self.app, self.catalog = app, app.catalog
        self.cache, self.satdump, self.dcs = cache, satdump, dcs_limits
        self.protect, self.include = set(protect), set(include)
        self.scope = None  # what SatDump retention applies to, for the log and the Live page
        if satdump:
            self.survey()
        self.every, self.dry_run = every, dry_run
        self.last = {"cache": None, "satdump": None, "dcs": None}
        self.cache_swept = 0
        self.cache_root = os.path.dirname(self.catalog.derived_root)
        os.makedirs(self.cache_root, exist_ok=True)

    def run(self):
        while True:
            for sweep in (self.sweep_cache, self.sweep_satdump, self.sweep_dcs):
                try:
                    sweep()
                except Exception:
                    traceback.print_exc()
            time.sleep(self.every)

    @staticmethod
    def size(unit):
        """A unit's size, measured the first time it is needed and then remembered.

        Units are [time, size or None, path, is_folder]. Measuring everything up front
        means a stat of every file on every sweep; in practice only the few oldest
        units ever need it. Anything measured here is older than 3 hours, so it is
        long since closed and its size is final.
        """
        if unit[1] is None:
            try:
                unit[1] = tree_size(unit[2]) if unit[3] else on_disk(os.lstat(unit[2]))
            except OSError:
                unit[1] = 0
        return unit[1]

    def choose(self, units, limits, now, disk_path, label):
        """How many of `units` (oldest first) must go to satisfy `limits`."""
        protected = now - PRUNE_FLOOR
        cut = 0  # units[:cut] go
        if limits.keep:
            while cut < len(units) and units[cut][0] < now - limits.keep:
                cut += 1
        if limits.max_size:  # a total needs every unit measured: the one slow rule
            total = sum(self.size(unit) for unit in units[cut:])
            while total > limits.max_size and cut < len(units) and units[cut][0] < protected:
                total -= self.size(units[cut])
                cut += 1
            if total > limits.max_size:
                log(f"prune {label}: still {human(total)} after removing everything older than 3 hours")
        if limits.disk_cap:
            disk = shutil.disk_usage(disk_path)
            excess = (disk.total - disk.free) - limits.disk_cap * disk.total
            if excess > 0:  # over the cap: measure only what is about to go, oldest first
                excess -= sum(self.size(unit) for unit in units[:cut])
                while excess > 0 and cut < len(units) and units[cut][0] < protected:
                    excess -= self.size(units[cut])
                    cut += 1
                if excess > 0:
                    log(f"prune {label}: the disk is still {human(excess)} over its {limits.disk_cap:.0%} cap; "
                        f"these limits cannot free more")
        return cut

    def record(self, key, label, summary):
        self.last[key] = {"t": int(time.time()), "what": summary, "dry": self.dry_run}
        log(f"prune {label}{' (dry run): would remove' if self.dry_run else ': removed'} {summary}")

    # -- GOESview's own files

    def cache_units(self):
        """(time, size, path, is_frame_folder): colour-frame folders by scan time, previews by when made."""
        found = []
        for base, dirs, names in os.walk(self.catalog.derived_root):
            when = stamp(os.path.basename(base), "%Y-%m-%d_%H-%M-%S") if SLOT_RE.match(os.path.basename(base)) else None
            if when is not None:
                dirs[:] = []
                found.append([when, None, base, True])
        for base, _, names in os.walk(self.app.previews.folder):
            for name in names:
                if name.endswith(".jpg"):
                    try:
                        st = os.lstat(os.path.join(base, name))
                    except OSError:
                        continue
                    found.append([st.st_mtime, on_disk(st), os.path.join(base, name), False])
        found.sort(key=lambda unit: unit[0])
        return found

    def sweep_cache(self):
        if not self.cache:
            return
        now = time.time()
        over = False
        if self.cache.disk_cap:
            disk = shutil.disk_usage(self.cache_root)
            over = disk.total - disk.free > self.cache.disk_cap * disk.total
        if now - self.cache_swept < 3600 and not over:
            return  # walking the whole cache is slow; hourly is plenty unless the disk is over its cap
        self.cache_swept = now
        units = self.cache_units()
        cut = self.choose(units, self.cache, now, self.cache_root, "GOESview cache") if units else 0
        if not cut:
            return
        doomed = units[:cut]
        frames = [unit for unit in doomed if unit[3]]
        self.record("cache", "GOESview cache", f"{count(len(frames), 'colour frame folder')} and "
                    f"{count(cut - len(frames), 'preview')}, {human(sum(self.size(unit) for unit in doomed))}")
        if self.dry_run:
            return
        dead, stale = set(), []
        for _, _, path, _ in frames:
            rel = os.path.relpath(path, self.catalog.derived_root).replace(os.sep, "/")
            try:
                for entry in os.scandir(path):
                    vpath = f"d/{rel}/{entry.name}"
                    dead.add(vpath)
                    stale.extend(self.app.previews.cached(vpath, entry.stat()))
            except OSError:
                pass
        if self.app.colourist:
            self.app.colourist.failed.update(dead)  # pruned on purpose: do not rebuild them
        self.catalog.forget(set(), dead)
        for _, _, path, is_frames in doomed:
            if not os.path.realpath(path).startswith(self.cache_root + os.sep):
                continue  # never touch anything outside the cache folder
            if is_frames:
                shutil.rmtree(path, ignore_errors=True)
            else:
                stale.append(path)
        for path in stale:
            try:
                os.unlink(path)
            except OSError:
                pass
        if dead:
            self.catalog.publish("pruned", {"cutoff": int(frames[-1][0]), "what": "cache"})

    # -- SatDump's received data: only with an explicit --satdump-* limit

    def survey(self):
        """Work out which folders SatDump retention applies to, and say so when that changes."""
        runs, protected, others = goes_runs(self.catalog.root, self.protect, self.include)
        name = lambda path: os.path.basename(path) or path
        scope = {"runs": [name(r) for r in runs], "protected": [name(r) for r in protected],
                 "others": others, "skipped": (self.scope or {}).get("skipped", 0)}
        if not self.scope or any(scope[k] != self.scope[k] for k in ("runs", "protected", "others")):
            log(f"retention, SatDump data applies to {count(len(runs), 'GOES HRIT run folder')}: "
                + (", ".join(scope["runs"]) or "none found"))
            if protected:
                log("retention, SatDump data: protected by --satdump-protect: " + ", ".join(scope["protected"]))
            if others:
                log(f"retention, SatDump data: never touches the {count(len(others), 'other item')} under the root: "
                    + ", ".join(others[:8]) + (" ..." if len(others) > 8 else ""))
        self.scope = scope
        return runs

    def satdump_units(self):
        """What may be deleted, as [time, size or None, path, is_folder], oldest first.

        Only inside GOES HRIT run folders, only under IMAGES/, L2/ and EMWIN/, and only
        things recognised as SatDump products: a timestamp folder holding nothing but
        SatDump's files, or a flat file with an EMWIN or NWS chart name. Anything else
        found along the way is left where it is.
        """
        found, skipped = [], 0

        def walk(folder, depth):
            nonlocal skipped
            try:
                entries = list(os.scandir(folder))
            except OSError:
                return
            for entry in entries:
                if entry.is_symlink() or entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    when = stamp(entry.name, "%Y-%m-%d_%H-%M-%S") if SLOT_RE.match(entry.name) else None
                    if when is None:
                        if depth < 6:
                            walk(entry.path, depth + 1)
                        continue
                    try:
                        inside = list(os.scandir(entry.path))
                    except OSError:
                        continue
                    if all(e.is_file(follow_symlinks=False) and satdump_product(e.name) for e in inside):
                        found.append([when, None, entry.path, True])
                    else:
                        skipped += 1  # something in there is not SatDump's: leave the whole folder
                    continue
                m = EMWIN_RE.match(entry.name)
                when = stamp(m.group(3), "%Y%m%d%H%M%S") if m else None
                if when is None and (m := NWS_RE.match(entry.name)):
                    when = stamp(m.group(1), "%Y%m%d%H%M%S")
                if when is None:
                    skipped += 1  # not a name SatDump gives its files
                    continue
                found.append([when, None, entry.path, False])

        for run in self.survey():
            for anchor in PRUNE_ANCHORS:
                walk(os.path.join(run, anchor), 0)
            try:  # packed bulletins: one unit per day, as old as that day's end
                for entry in os.scandir(os.path.join(run, ARCHIVE_DIR)):
                    m = DAY_ZIP_RE.match(entry.name)
                    day = stamp(m.group(1), "%Y-%m-%d") if m and entry.is_file(follow_symlinks=False) else None
                    if day is not None:
                        found.append([day + 86399, None, entry.path, False])
            except OSError:
                pass
        if skipped != self.scope["skipped"]:
            log(f"retention, SatDump data: leaving {count(skipped, 'unrecognised item')} inside the run folders alone")
        self.scope["skipped"] = skipped
        found.sort(key=lambda unit: unit[0])
        return found

    def sweep_satdump(self):
        if not self.satdump:
            return  # the default: SatDump's files are never touched
        units = self.satdump_units()
        if not units:
            return
        # Ages count back from the newest data received, capped at the wall clock.
        # A clock set wrong in either direction, a long outage, or one folder with
        # a garbled future date then cannot make the whole archive look old.
        now = min(time.time(), units[-1][0])
        cut = self.choose(units, self.satdump, now, self.catalog.root, "SatDump data")
        if not cut:
            return
        doomed = units[:cut]
        catalog, root = self.catalog, self.catalog.root
        dead_dirs, dead_files, stale, dead_zips = set(), set(), [], []

        def note_previews(vpath, path):
            if vpath.rsplit(".", 1)[-1].lower() in IMAGE_EXT:
                try:
                    stale.extend(self.app.previews.cached(vpath, os.stat(path)))
                except OSError:
                    pass

        for _, _, path, is_folder in doomed:
            if not os.path.realpath(path).startswith(root + os.sep):
                continue  # never touch anything outside the data folder
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if not is_folder and DAY_ZIP_RE.match(os.path.basename(path)):
                packed = catalog.members(f"z/{rel}/")  # a day of bulletins in one archive
                dead_files.update(packed)
                dead_zips.append(path)
                continue
            if not is_folder:
                dead_files.add("s/" + rel)
                note_previews("s/" + rel, path)
                continue
            dead_dirs.add(rel)
            for prefix, base in (("s/", path), ("d/", os.path.join(catalog.derived_root, *rel.split("/")))):
                try:
                    for entry in os.scandir(base):
                        note_previews(f"{prefix}{rel}/{entry.name}", entry.path)
                except OSError:
                    pass

        self.record("satdump", "SatDump data",
                    f"{count(len(dead_dirs), 'folder')} and {count(len(dead_files), 'file')} dated up to "
                    f"{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(doomed[-1][0]))}"
                    + (f", {human(sum(unit[1] for unit in doomed))}" if all(u[1] is not None for u in doomed) else ""))
        if self.dry_run:
            return

        catalog.forget(dead_dirs, dead_files)  # first, so nothing still points at the files
        if self.app.text_index:
            self.app.text_index.forget(dead_files)
        for path in stale:  # previews made from the deleted data
            try:
                os.unlink(path)
            except OSError:
                pass
        for rel in dead_dirs:
            shutil.rmtree(os.path.join(root, *rel.split("/")), ignore_errors=True)
            shutil.rmtree(os.path.join(catalog.derived_root, *rel.split("/")), ignore_errors=True)
        for path in dead_zips + [os.path.join(root, *v[2:].split("/")) for v in dead_files if v.startswith("s/")]:
            try:
                os.unlink(path)
            except OSError:
                pass
        catalog.publish("pruned", {"cutoff": int(doomed[-1][0]), "what": "satdump"})


    # -- SatDump's DCS files: a policy of their own, only with an explicit --satdump-dcs-* limit

    def sweep_dcs(self):
        if not self.dcs:
            return  # the default: SatDump's DCS files are never touched
        root = self.catalog.root
        units = dcs.retention_units(goes_runs(root, self.protect, self.include)[0])
        if not units:
            return
        now = min(time.time(), units[-1][0])  # ages count back from the newest data, as for the rest
        cut = self.choose(units, self.dcs, now, root, "SatDump DCS files")
        if not cut:
            return
        doomed = units[:cut]
        self.record("dcs", "SatDump DCS files",
                    f"{count(len(doomed), 'DCS file')} dated up to "
                    f"{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(doomed[-1][0]))}, "
                    f"{human(sum(self.size(unit) for unit in doomed))}")
        if self.dry_run:
            return
        for _, _, path, _ in doomed:
            if not os.path.realpath(path).startswith(root + os.sep):
                continue  # never touch anything outside the data folder
            try:
                os.unlink(path)
            except OSError:
                pass


# ------------------------------------------------------------ bulletin archive

class Archiver(threading.Thread):
    """Packs each finished day's EMWIN text bulletins into one zip, then deletes the loose files.

    Bulletins are tiny and there are tens of thousands a day. On a drive with large
    clusters each one takes far more room than its length, and folders that size slow
    down everything that writes into them, SatDump included. Today's bulletins are
    left alone as ordinary files; once a UTC day is over (plus an hour's grace) its
    bulletins go into <run>/EMWIN-archive/<day>.zip under their original names.

    A loose file is deleted only after the archive holding it has been written to a
    temporary file, flushed to disk, read back and compared byte for byte, and moved
    into place. If any step fails, the loose files stay and the archive is not touched.
    """
    GRACE = 3600

    def __init__(self, app, protect, include, dry_run):
        super().__init__(daemon=True, name="archiver")
        self.app, self.catalog = app, app.catalog
        self.protect, self.include, self.dry_run = set(protect), set(include), dry_run
        self.last = None
        self.days = 0

    def run(self):
        time.sleep(45)  # let the first scan and its follow-up work settle
        while True:
            try:
                self.sweep()
            except Exception:
                traceback.print_exc()
            time.sleep(1800)

    def sweep(self):
        boundary = int(time.time() - self.GRACE) // 86400 * 86400  # start of the current UTC day
        days = 0
        for run in goes_runs(self.catalog.root, self.protect, self.include)[0]:
            folder, by_day = os.path.join(run, "EMWIN"), {}
            try:
                entries = list(os.scandir(folder))
            except OSError:
                entries = []
            for entry in entries:
                m = EMWIN_RE.match(entry.name) if entry.name.lower().endswith(".txt") else None
                when = stamp(m.group(3), "%Y%m%d%H%M%S") if m else None
                if when is not None and when < boundary and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    by_day.setdefault(time.strftime("%Y-%m-%d", time.gmtime(when)), []).append(entry.name)
            for day in sorted(by_day):
                self.pack(run, day, sorted(by_day[day]))
            try:
                days += sum(1 for e in os.scandir(os.path.join(run, ARCHIVE_DIR)) if DAY_ZIP_RE.match(e.name))
            except OSError:
                pass
        self.days = days

    def pack(self, run, day, names):
        label = f"{day} in {os.path.basename(run)}"
        if self.dry_run:
            log(f"archive (dry run): would pack {count(len(names), 'bulletin')} from {label}")
            return
        folder, out = os.path.join(run, "EMWIN"), os.path.join(run, ARCHIVE_DIR)
        target = os.path.join(out, day + ".zip")
        tmp = target + ".tmp"
        started = time.time()
        try:
            os.makedirs(out, exist_ok=True)
            if os.path.exists(target):
                shutil.copyfile(target, tmp)  # late arrivals for a day already packed: extend a copy
            elif os.path.exists(tmp):
                os.unlink(tmp)
            with zipfile.ZipFile(tmp, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                have = set(zf.namelist())
                for name in names:
                    if name not in have:
                        zf.write(os.path.join(folder, name), name)
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            with zipfile.ZipFile(tmp) as zf:  # read every bulletin back before trusting the archive
                for name in names:
                    with open(os.path.join(folder, name), "rb") as f:
                        if zf.read(name) != f.read():
                            raise ValueError(f"{name} did not read back identically")
            os.replace(tmp, target)
        except Exception as exc:
            log(f"archive: {label} NOT packed, loose files left as they were: {exc}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return

        # The archive is complete, verified and in place. Repoint the catalog, then free the space.
        rel = os.path.relpath(run, self.catalog.root).replace(os.sep, "/")
        prefix = "" if rel == "." else rel + "/"
        moves = [(f"s/{prefix}EMWIN/{name}", f"z/{prefix}{ARCHIVE_DIR}/{day}.zip/{name}") for name in names]
        self.catalog.repath(moves)
        if self.app.text_index:
            self.app.text_index.jobs.put(("move", moves))
        freed = 0
        for name in names:
            try:
                path = os.path.join(folder, name)
                freed += on_disk(os.lstat(path))
                os.unlink(path)
            except OSError:
                pass
        packed = on_disk(os.lstat(target))
        self.last = {"t": int(time.time()), "day": day, "n": len(names), "freed": freed, "size": packed}
        log(f"archive: packed {count(len(names), 'bulletin')} from {label}: {human(freed)} of loose files "
            f"now {human(packed)} in {ARCHIVE_DIR}/{day}.zip ({time.time() - started:.0f}s)")

    def status(self):
        return {"days": self.days, "last": self.last, "dry": self.dry_run}


# ----------------------------------------------------------------------- http

CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def snippet(catalog, vpath, limit=1400):
    raw = catalog.read_bytes(vpath, limit + 1)
    if raw is None:
        return "", False
    text = CONTROL_RE.sub("", raw[:limit].decode("latin-1")).lstrip("\n")
    return text, len(raw) > limit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "goesview"
    app = None  # set in main()

    def log_message(self, *args):
        if self.app.verbose:
            super().log_message(*args)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            params = dict(urllib.parse.parse_qsl(url.query))
            if path.startswith("/api/"):
                self.api(path[5:], params)
            elif path.startswith("/f/z/"):  # a bulletin packed into a day's archive
                self.send_bytes(self.app.catalog.read_bytes(path[3:]), "text/plain; charset=iso-8859-1", "max-age=86400")
            elif path.startswith("/f/"):
                self.send_file(self.app.catalog.resolve(path[3:]), "max-age=86400")
            elif path.startswith("/p/"):
                size, _, vpath = path[3:].partition("/")
                self.send_file(self.app.previews.get(vpath, int(size)) if size.isdigit() else None,
                               "max-age=604800, immutable")
            else:
                name = path.lstrip("/") or "index.html"
                full = os.path.join(WEB, name)
                self.send_file(full if "/" not in name and os.path.isfile(full) else None, "no-cache")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except ValueError:
            self.send_error(400)

    def api(self, name, params):
        catalog = self.app.catalog
        if name == "events":
            return self.events()
        if name == "types":
            body = {"now": int(time.time()), "types": catalog.type_list(), "lut": self.app.lut_name}
        elif name == "items" and params.get("q") and params.get("kind") == "t" and self.app.text_index:
            # the Text page's search box: full text, plus product and office names
            hits, cursor, more = catalog.search_text(self.app.text_index, params["q"], params.get("type"),
                                                     int(params.get("before", 0)) or None,
                                                     min(int(params.get("limit", 30)), 100))
            rows = []
            for item, excerpt in hits:
                row = item.as_dict()
                row["hit"] = excerpt
                row["b"], row["more"] = snippet(catalog, item.path)
                rows.append(row)
            body = {"items": rows, "more": more, "cursor": cursor, "index": self.app.text_index.progress()}
        elif name == "items":
            items, more = catalog.feed(params.get("sec"), params.get("type"),
                                       {"i": "image", "t": "text"}.get(params.get("kind")),
                                       int(params.get("before", 0)), min(int(params.get("limit", 50)), 200),
                                       params.get("q"),
                                       {"0": False, "1": True}.get(params.get("raw")), params.get("group"))
            rows = []
            for item in items:
                row = item.as_dict()
                if item.kind == "text" and params.get("body"):
                    row["b"], row["more"] = snippet(catalog, item.path)
                rows.append(row)
            body = {"items": rows, "more": more}
        elif name == "timeline":
            body = catalog.timeline(params.get("type", ""))
        elif name == "stats":
            body = self.app.stats()
        elif name == "monitor" and self.app.monitor:
            body = self.app.monitor.status()
        elif name == "monitor/history" and self.app.monitor:
            body = self.app.monitor.history(min(max(float(params.get("hours", 24)), 0.25), 24 * monitor.KEEP_DAYS))
        elif name.startswith("dcs/") and self.app.dcs:
            store = self.app.dcs
            if name == "dcs/summary":
                body = store.summary()
            elif name == "dcs/platforms":
                body = store.platforms(params.get("q"), params.get("kind"), params.get("sort"),
                                       min(int(params.get("limit", 100)), 500), max(int(params.get("offset", 0)), 0))
            elif name == "dcs/platform":
                body = store.platform(params.get("a", ""))
            else:
                body = None
        else:
            body = None
        if body is None:
            return self.send_error(404)
        self.send_json(body)

    def send_json(self, body):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if len(data) > 1500 and "gzip" in self.headers.get("Accept-Encoding", ""):
            data = gzip.compress(data, 5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_bytes(self, data, ctype, cache):
        if data is None:
            return self.send_error(404)
        start, end, status = 0, len(data) - 1, 200
        m = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range", ""))
        if m and data:
            start = min(int(m.group(1)), len(data))
            end = min(int(m.group(2)), end) if m.group(2) else end
            status = 206
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{len(data)}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(chunk)

    def send_file(self, path, cache):
        try:
            f = open(path, "rb") if path else None
        except OSError:
            f = None
        if f is None:
            return self.send_error(404)
        with f:
            st = os.fstat(f.fileno())
            since = self.headers.get("If-Modified-Since")
            if since and "Range" not in self.headers:
                try:
                    if int(st.st_mtime) <= parsedate_to_datetime(since).timestamp():
                        self.send_response(304)
                        self.send_header("Cache-Control", cache)
                        self.end_headers()
                        return
                except (TypeError, ValueError):
                    pass
            start, end, status = 0, st.st_size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if m and (m.group(1) or m.group(2)) and st.st_size:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), end) if m.group(2) else end
                else:
                    start = max(0, st.st_size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{st.st_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=" + ("iso-8859-1" if path.lower().endswith(".txt") else "utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{st.st_size}")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.flush()
                self.connection.sendfile(f, start, end - start + 1)

    def events(self):
        """Server-sent events: one long response per open browser tab."""
        catalog = self.app.catalog
        q = catalog.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True

            def send(event, data, event_id=None):
                head = f"id: {event_id}\n" if event_id else ""
                self.wfile.write(f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
                self.wfile.flush()

            self.wfile.write(b"retry: 3000\n\n")
            last = self.headers.get("Last-Event-ID", "")
            if last.isdigit():  # reconnect: replay what the tab missed
                for row in catalog.since(int(last)):
                    send("item", row, row["id"])
            send("stats", self.app.stats())
            while True:
                try:
                    send(*q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            catalog.unsubscribe(q)


class App:
    def __init__(self, catalog, previews, lut_name, verbose):
        self.catalog, self.previews, self.lut_name, self.verbose = catalog, previews, lut_name, verbose
        self.extra = {}
        self.pruner = None
        self.colourist = None
        self.text_index = None
        self.archiver = None
        self.dcs = None
        self.dcs_archiver = None
        self.monitor = None

    def stats(self):
        disk = shutil.disk_usage(self.catalog.root)
        pruner = self.pruner
        storage = {"free": disk.free, "total": disk.total,
                   "cache": pruner.cache.describe() if pruner and pruner.cache else None,
                   "satdump": pruner.satdump.describe() if pruner and pruner.satdump else None,
                   "scope": pruner.scope if pruner else None,
                   "archive": self.archiver.status() if self.archiver else None,
                   # SatDump's DCS files have a policy of their own; None when nothing may delete or pack them
                   "dcs": {"limits": pruner.dcs.describe() if pruner and pruner.dcs else None,
                           "archive": self.dcs_archiver.status() if self.dcs_archiver else None},
                   "dry": bool(pruner and pruner.dry_run),
                   "last": pruner.last if pruner else {}}
        brief = None
        if self.dcs:
            try:
                brief = self.dcs.brief()
            except Exception:
                traceback.print_exc()
        health = None
        if self.monitor:
            try:
                health = self.monitor.brief()
            except Exception:
                traceback.print_exc()
        return {**self.catalog.stats(), "storage": storage, "dcs": brief, "health": health, **self.extra}

    def watch(self, interval):
        ticks = 0
        while True:
            time.sleep(interval)
            try:
                self.catalog.poll()
            except Exception:
                traceback.print_exc()
            ticks += 1
            if ticks % max(1, round(10 / interval)) == 0 and self.catalog.subscribers:
                self.catalog.publish("stats", self.stats())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="SatDump output folder (or a folder of several runs)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/goesview"),
                    help="where previews and false-colour frames are kept (default: %(default)s)")
    ap.add_argument("--lut", default="usradioguy", help="false-colour table: usradioguy, wxstar, a PNG, or none")
    ap.add_argument("--curve", default="curve", help='band 2 contrast curve PNG, or "none"')
    ap.add_argument("--colour-hours", type=float, default=72, help="build false colour for slots this recent (default: %(default)s)")
    ap.add_argument("--warm-hours", type=float, default=24, help="pre-render previews for images this recent (default: %(default)s)")
    ap.add_argument("--workers", type=int, default=2, help="concurrent preview renders (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=3, help="seconds between folder checks (default: %(default)s)")
    ours = ap.add_argument_group(
        "GOESview's own files",
        "The cache of previews and false-colour frames under --cache. Safe to delete: they are rebuilt "
        "from SatDump's data when needed. These options never touch SatDump's files.")
    ours.add_argument("--cache-keep", type=parse_age_or_off, default=30 * 86400, metavar="AGE",
                      help='delete cached files older than this: 36h, 14d, 2w, 3mo; "off" keeps them forever (default: 30d)')
    ours.add_argument("--cache-max-size", type=parse_size, metavar="SIZE", help="keep the cache under this size: 500M, 20G")
    ours.add_argument("--cache-disk-cap", type=parse_cap, metavar="N%",
                      help="when the disk is fuller than this, delete the oldest cached files: 90%%")
    theirs = ap.add_argument_group(
        "SatDump's received data",
        "These PERMANENTLY DELETE files SatDump wrote, oldest first. None has a default: unless you give one, "
        "GOESview never deletes anything SatDump received. They apply to every GOES HRIT run folder under the "
        "root, meaning folders carrying SatDump's own '..._goes_hrit_...' name, old runs included, and to nothing "
        "else: other satellites, other pipelines and your own files and folders are never touched. Inside a run "
        "only recognised SatDump products go: timestamp folders under IMAGES/ and L2/, EMWIN files and NWS "
        "charts. Data newer than 3 hours is never deleted. The log and the Live page list exactly which folders "
        "are covered. Try with --prune-dry-run first.")
    theirs.add_argument("--satdump-keep", type=parse_age, metavar="AGE", help="delete SatDump data older than this: 30d, 3mo, 1y")
    theirs.add_argument("--satdump-max-size", type=parse_size, metavar="SIZE", help="keep the SatDump folder under this size: 200G, 1.5T")
    theirs.add_argument("--satdump-disk-cap", type=parse_cap, metavar="N%",
                        help="when the disk is fuller than this, delete the oldest SatDump data: 90%%")
    theirs.add_argument("--satdump-protect", action="append", default=[], metavar="FOLDER",
                        help="never delete from this run folder (its name under the root); may be repeated")
    theirs.add_argument("--archive-bulletins", action="store_true",
                        help="pack each finished day's EMWIN text bulletins into one zip inside the run folder "
                             f"({ARCHIVE_DIR}/DAY.zip), verify it, then delete the loose files. Nothing is lost and "
                             "they stay viewable and searchable; it saves a great deal of space on drives with large "
                             "clusters. Today's bulletins are left as ordinary files")
    theirs.add_argument("--satdump-include", action="append", default=[], metavar="FOLDER",
                        help="also treat this folder as a GOES HRIT run, if you named it yourself; may be repeated")
    dcs_group = ap.add_argument_group(
        "DCS (Data Collection System)",
        "When SatDump's \"Parse DCS\" is on, GOESview reads the DCS files it writes and shows them on a DCS page; "
        "with no DCS files there is no DCS page. Reading never changes SatDump's files. The --satdump-dcs-* and "
        "--archive-dcs options are separate from everything above: --satdump-keep and friends never touch DCS "
        "files, and these never touch anything else. None has a default.")
    dcs_group.add_argument("--dcs-history", type=parse_age, default=3 * 86400, metavar="AGE",
                           help="how far back GOESview's own DCS database keeps every message, for a platform's "
                                "history and charts; about 300 MB a day (default: 3d). This is GOESview's file, "
                                "in the cache folder")
    dcs_group.add_argument("--satdump-dcs-keep", type=parse_age, metavar="AGE",
                           help="PERMANENTLY DELETE SatDump's DCS files older than this: 15d, 1mo")
    dcs_group.add_argument("--satdump-dcs-max-size", type=parse_size, metavar="SIZE",
                           help="keep SatDump's DCS files under this size, deleting the oldest: 20G")
    dcs_group.add_argument("--archive-dcs", action="store_true",
                           help="pack each finished day's DCS files into one zip inside the run folder "
                                f"({dcs.DCS_ARCHIVE_DIR}/DAY.zip), verify it, then delete the loose files. A day is "
                                "some 24,000 files; packed it takes about a ninth of the space. Today's files are "
                                "left as they are")
    dcs_group.add_argument("--no-dcs", action="store_true", help="do not read DCS files at all: no DCS page")
    ap.add_argument("--prune-every", type=float, default=10, metavar="MIN", help="minutes between retention sweeps (default: %(default)s)")
    ap.add_argument("--prune-dry-run", action="store_true", help="log what retention would delete, delete nothing")
    for old in ("--keep", "--max-size", "--min-free"):  # early names that did not say whose files they deleted
        ap.add_argument(old, dest="renamed", help=argparse.SUPPRESS)
    ap.add_argument("--no-text-index", action="store_true",
                    help="do not build the full-text search index for the Text page (it is small: a few percent of the size of the bulletin text)")
    ap.add_argument("--satdump-log", metavar="PATH", default="auto",
                    help="SatDump's log file, or the folder it keeps its logs in, for the System page: signal, error "
                         "rate, lock, and whether SatDump has stopped or hung. It is only read. Default: "
                         "~/.config/satdump if it exists; \"none\" to leave SatDump out")
    ap.add_argument("--no-monitor", action="store_true", help="no System page and no health checks")
    ap.add_argument("--satdump-api", help="URL of SatDump's HTTP stats (satdump live ... --http_server), e.g. http://127.0.0.1:8081/api")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args()

    if args.renamed:
        ap.error("--keep, --max-size and --min-free were split in two: use --cache-keep / --cache-max-size / "
                 "--cache-disk-cap for GOESview's own files, or --satdump-keep / --satdump-max-size / "
                 "--satdump-disk-cap to delete SatDump's received data")
    if not os.path.isdir(args.root):
        sys.exit(f"Not a folder: {args.root}")
    try:  # claim the port first: if it is taken, stop before any background work starts
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.exit(f"Cannot listen on {args.host}:{args.port}: {exc}")
    server.daemon_threads = True
    catalog = Catalog(args.root, os.path.join(args.cache, "derived"))
    previews = Previews(catalog, os.path.join(args.cache, "previews"), args.workers)
    catalog.listeners.append(previews.enqueue)
    app = App(catalog, previews, None, args.verbose)
    catalog.scan()

    if Image is None:
        log("Pillow not installed: no previews, originals only (apt install python3-pil)")
    else:
        threading.Thread(target=previews.warm_forever, args=(args.warm_hours * 3600,), daemon=True).start()

    if args.lut != "none":
        lut_path = fc and fc.find_lut(args.lut)
        curve_path = fc and args.curve != "none" and fc.find_lut(args.curve) or None
        if fc is None or Image is None:
            log("numpy/Pillow not installed: false colour disabled (apt install python3-numpy python3-pil)")
        elif not lut_path:
            log(f"colour table not found: {args.lut}; false colour disabled")
        else:
            app.lut_name = os.path.splitext(os.path.basename(args.lut))[0]
            app.colourist = Colourist(catalog, app.lut_name, lut_path, curve_path, args.colour_hours * 3600)
            app.colourist.start()
            log(f"false colour: {lut_path}")

    if not args.no_text_index:
        try:
            os.makedirs(args.cache, exist_ok=True)
            app.text_index = TextIndex(catalog, args.cache)
            app.text_index.start()
        except Exception as exc:
            log(f"full-text search unavailable ({exc}); the Text page filters by product and office only")

    cache_limits = Limits(args.cache_keep, args.cache_max_size, args.cache_disk_cap)
    satdump_limits = Limits(args.satdump_keep, args.satdump_max_size, args.satdump_disk_cap)
    dry = " (dry run)" if args.prune_dry_run else ""
    log(f"retention, GOESview cache: {cache_limits.describe() if cache_limits else 'kept forever'}{dry}")
    if satdump_limits:
        log(f"retention, SatDump data: {satdump_limits.describe()}; OLDER DATA IS DELETED{dry}")
    else:
        log("retention, SatDump data: never deleted (no --satdump-* option given)")
    dcs_limits = Limits(args.satdump_dcs_keep, args.satdump_dcs_max_size)
    if dcs_limits:
        log(f"retention, SatDump DCS files: {dcs_limits.describe()}; OLDER DCS FILES ARE DELETED{dry}")
    else:
        log("retention, SatDump DCS files: never deleted (no --satdump-dcs-* option given)")
    if cache_limits or satdump_limits or dcs_limits:
        app.pruner = Pruner(app, cache_limits, satdump_limits, args.prune_every * 60, args.prune_dry_run,
                            args.satdump_protect, args.satdump_include, dcs_limits)
        app.pruner.start()

    if args.archive_bulletins:
        app.archiver = Archiver(app, args.satdump_protect, args.satdump_include, args.prune_dry_run)
        app.archiver.start()
        log(f"bulletin archive: on{dry}; finished days are packed into <run>/{ARCHIVE_DIR}/DAY.zip and the loose files deleted")
    else:
        log("bulletin archive: off (see --archive-bulletins)")

    if not args.no_dcs:
        try:
            os.makedirs(args.cache, exist_ok=True)
            # reading is harmless, so protected runs are read too; packing and deleting honour --satdump-protect
            app.dcs = dcs.DcsStore(args.cache, lambda: goes_runs(catalog.root, (), args.satdump_include)[0],
                                   args.dcs_history, log)
            app.dcs.start()
        except Exception as exc:
            log(f"DCS unavailable ({exc})")
    if args.archive_dcs:
        app.dcs_archiver = dcs.DcsArchiver(
            lambda: goes_runs(catalog.root, args.satdump_protect, args.satdump_include)[0], args.prune_dry_run, log, on_disk)
        app.dcs_archiver.start()
        log(f"DCS archive: on{dry}; finished days are packed into <run>/{dcs.DCS_ARCHIVE_DIR}/DAY.zip and the loose files deleted")
    else:
        log("DCS archive: off (see --archive-dcs)")

    if not args.no_monitor:
        where = None if args.satdump_log == "none" else args.satdump_log
        if where == "auto":
            where = os.path.expanduser("~/.config/satdump")
            where = where if os.path.isdir(where) else None
        try:
            os.makedirs(args.cache, exist_ok=True)
            app.monitor = monitor.Monitor(args.cache, app, log, where)
            app.monitor.start()
            log(f"system monitor: on; SatDump's log: {where or 'not found, so only this machine and GOESview are watched'}")
        except Exception as exc:
            log(f"system monitor unavailable ({exc})")

    if args.satdump_api:
        threading.Thread(target=poll_satdump, args=(args.satdump_api, app.extra), daemon=True).start()
    threading.Thread(target=app.watch, args=(args.interval,), daemon=True).start()

    Handler.app = app
    log(f"serving {catalog.root} on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
