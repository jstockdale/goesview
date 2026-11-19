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
import gzip
import hashlib
import json
import mimetypes
import os
import posixpath
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import timedelta
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    # -- scanning

    def scan(self):
        """First pass: read everything, then number items in arrival order."""
        started = time.time()
        self._staging = []
        self._rescan(self.root, 0)
        staged, self._staging = self._staging, None
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
        self.items.append(item)
        self.by_path[vpath] = item
        kind.arrivals.append(item)
        bisect.insort(kind.items, item, key=lambda i: (i.time, i.id))
        if live:
            self.publish("item", item.as_dict(), item.id)
        for listener in self.listeners:
            listener(item, live)
        return item

    def add_derived(self, vpath, spec, when, recv, size, note):
        with self.lock:
            return self._commit(vpath, spec, when, recv, size, note, live=True)

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
            if before:
                hi = bisect.bisect_left(source, before, key=lambda i: i.id)
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

    def since(self, last_id, limit=300):
        with self.lock:
            start = bisect.bisect_right(self.items, last_id, key=lambda i: i.id)
            return [i.as_dict() for i in self.items[max(start, len(self.items) - limit):]]

    def forget(self, dead_dirs, dead_files):
        """Drop everything in the given timestamp folders (paths relative to the
        root) and the given individual files (virtual paths). Returns the count."""
        def gone(vpath):
            return vpath in dead_files or posixpath.dirname(vpath)[2:] in dead_dirs
        with self.lock:
            dead = {i.id for i in self.items if gone(i.path)}
            self.items = [i for i in self.items if i.id not in dead]
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
            self.catalog.add_derived(vpath, spec, when, st.st_mtime, st.st_size, note)
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


def tree_size(folder):
    total = 0
    for base, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


class Pruner(threading.Thread):
    """Deletes the oldest files to stay within the cache limits and, separately and
    only if asked, the SatDump limits. Always oldest first."""

    def __init__(self, app, cache, satdump, every, dry_run):
        super().__init__(daemon=True, name="pruner")
        self.app, self.catalog = app, app.catalog
        self.cache, self.satdump = cache, satdump
        self.every, self.dry_run = every, dry_run
        self.last = {"cache": None, "satdump": None}
        self.cache_swept = 0
        self.cache_root = os.path.dirname(self.catalog.derived_root)
        os.makedirs(self.cache_root, exist_ok=True)

    def run(self):
        while True:
            for sweep in (self.sweep_cache, self.sweep_satdump):
                try:
                    sweep()
                except Exception:
                    traceback.print_exc()
            time.sleep(self.every)

    def choose(self, units, limits, now, disk_path, label):
        """How many of `units` (oldest first) must go to satisfy `limits`."""
        protected = now - PRUNE_FLOOR
        cut = 0  # units[:cut] go
        if limits.keep:
            while cut < len(units) and units[cut][0] < now - limits.keep:
                cut += 1
        if limits.max_size:
            total = sum(unit[1] for unit in units[cut:])
            while total > limits.max_size and cut < len(units) and units[cut][0] < protected:
                total -= units[cut][1]
                cut += 1
            if total > limits.max_size:
                log(f"prune {label}: still {human(total)} after removing everything older than 3 hours")
        if limits.disk_cap:
            disk = shutil.disk_usage(disk_path)
            excess = (disk.total - disk.free) - limits.disk_cap * disk.total - sum(unit[1] for unit in units[:cut])
            while excess > 0 and cut < len(units) and units[cut][0] < protected:
                excess -= units[cut][1]
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
                found.append((when, tree_size(base), base, True))
        for base, _, names in os.walk(self.app.previews.folder):
            for name in names:
                if name.endswith(".jpg"):
                    try:
                        st = os.lstat(os.path.join(base, name))
                    except OSError:
                        continue
                    found.append((st.st_mtime, st.st_size, os.path.join(base, name), False))
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
                    f"{count(cut - len(frames), 'preview')}, {human(sum(unit[1] for unit in doomed))}")
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

    def satdump_units(self, sizes):
        """Everything SatDump wrote that may be deleted, as (time, size, path, is_folder)."""
        found = []

        def walk(folder, depth, anchored):
            try:
                entries = list(os.scandir(folder))
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink() or entry.path == self.catalog.derived_root:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if anchored and SLOT_RE.match(entry.name):
                        when = stamp(entry.name, "%Y-%m-%d_%H-%M-%S")
                        if when is not None:
                            found.append((when, tree_size(entry.path) if sizes else 0, entry.path, True))
                    elif depth < 8 and (anchored or depth == 0 or entry.name in PRUNE_ANCHORS):
                        walk(entry.path, depth + 1, anchored or entry.name in PRUNE_ANCHORS)
                elif anchored:
                    m = EMWIN_RE.match(entry.name)
                    when = stamp(m.group(3), "%Y%m%d%H%M%S") if m else None
                    if when is None and (m := NWS_RE.match(entry.name)):
                        when = stamp(m.group(1), "%Y%m%d%H%M%S")
                    size = 0
                    if when is None or sizes:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        when, size = st.st_mtime if when is None else when, st.st_size
                    found.append((when, size, entry.path, False))

        walk(self.catalog.root, 0, os.path.basename(self.catalog.root) in PRUNE_ANCHORS)
        found.sort(key=lambda unit: unit[0])
        return found

    def sweep_satdump(self):
        if not self.satdump:
            return  # the default: SatDump's files are never touched
        sizes = bool(self.satdump.max_size or self.satdump.disk_cap)
        units = self.satdump_units(sizes)
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
        dead_dirs, dead_files, stale = set(), set(), []

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
                    + (f", {human(sum(unit[1] for unit in doomed))}" if sizes else ""))
        if self.dry_run:
            return

        catalog.forget(dead_dirs, dead_files)  # first, so nothing still points at the files
        for path in stale:  # previews made from the deleted data
            try:
                os.unlink(path)
            except OSError:
                pass
        for rel in dead_dirs:
            shutil.rmtree(os.path.join(root, *rel.split("/")), ignore_errors=True)
            shutil.rmtree(os.path.join(catalog.derived_root, *rel.split("/")), ignore_errors=True)
        for vpath in dead_files:
            try:
                os.unlink(os.path.join(root, *vpath[2:].split("/")))
            except OSError:
                pass
        catalog.publish("pruned", {"cutoff": int(doomed[-1][0]), "what": "satdump"})


# ----------------------------------------------------------------------- http

CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def snippet(path, limit=1400):
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError:
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
                    row["b"], row["more"] = snippet(catalog.resolve(item.path))
                rows.append(row)
            body = {"items": rows, "more": more}
        elif name == "timeline":
            body = catalog.timeline(params.get("type", ""))
        elif name == "stats":
            body = self.app.stats()
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

    def stats(self):
        disk = shutil.disk_usage(self.catalog.root)
        pruner = self.pruner
        storage = {"free": disk.free, "total": disk.total,
                   "cache": pruner.cache.describe() if pruner and pruner.cache else None,
                   "satdump": pruner.satdump.describe() if pruner and pruner.satdump else None,
                   "dry": bool(pruner and pruner.dry_run),
                   "last": pruner.last if pruner else {}}
        return {**self.catalog.stats(), "storage": storage, **self.extra}

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
        "These PERMANENTLY DELETE files SatDump wrote, oldest first: timestamp folders under IMAGES/ and L2/, "
        "and files in EMWIN/ and IMAGES/NWS/. None has a default: unless you give one, GOESview never deletes "
        "anything SatDump received. Data newer than 3 hours is never deleted. Try with --prune-dry-run first.")
    theirs.add_argument("--satdump-keep", type=parse_age, metavar="AGE", help="delete SatDump data older than this: 30d, 3mo, 1y")
    theirs.add_argument("--satdump-max-size", type=parse_size, metavar="SIZE", help="keep the SatDump folder under this size: 200G, 1.5T")
    theirs.add_argument("--satdump-disk-cap", type=parse_cap, metavar="N%",
                        help="when the disk is fuller than this, delete the oldest SatDump data: 90%%")
    ap.add_argument("--prune-every", type=float, default=10, metavar="MIN", help="minutes between retention sweeps (default: %(default)s)")
    ap.add_argument("--prune-dry-run", action="store_true", help="log what retention would delete, delete nothing")
    for old in ("--keep", "--max-size", "--min-free"):  # early names that did not say whose files they deleted
        ap.add_argument(old, dest="renamed", help=argparse.SUPPRESS)
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

    cache_limits = Limits(args.cache_keep, args.cache_max_size, args.cache_disk_cap)
    satdump_limits = Limits(args.satdump_keep, args.satdump_max_size, args.satdump_disk_cap)
    dry = " (dry run)" if args.prune_dry_run else ""
    log(f"retention, GOESview cache: {cache_limits.describe() if cache_limits else 'kept forever'}{dry}")
    if satdump_limits:
        log(f"retention, SatDump data: {satdump_limits.describe()}; OLDER DATA IS DELETED{dry}")
    else:
        log("retention, SatDump data: never deleted (no --satdump-* option given)")
    if cache_limits or satdump_limits:
        app.pruner = Pruner(app, cache_limits, satdump_limits, args.prune_every * 60, args.prune_dry_run)
        app.pruner.start()

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
