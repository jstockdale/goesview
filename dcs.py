"""DCS (Data Collection System) support for GOESview.

With "Parse DCS" switched on, SatDump writes one JSON file every few seconds into
<run>/DCS/, each holding a few dozen messages from the tens of thousands of river
gauges, weather stations and buoys that report through GOES. That is around a
million messages a day: far too many to treat like bulletins, one item per file
held in memory. So they get their own small database in the cache folder:

    platforms   one row per platform ever heard: when, how often, its latest message
    messages    every message, for the last --dcs-history (3 days by default)
    hours       hourly totals, kept longer
    files       which SatDump files have been read, so none is read twice

Three separate jobs live here, and only the first runs unless asked for:

    DcsStore        reads the files. It never changes or deletes anything SatDump wrote.
    DcsArchiver     --archive-dcs: packs each finished day's files into one verified zip.
    retention_units --satdump-dcs-*: what DCS retention may delete, oldest first.
"""

import calendar
import glob
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import urllib.request
import zipfile

DCS_DIR = "DCS"
DCS_ARCHIVE_DIR = "DCS-archive"  # in a run folder: one zip of DCS files per finished day
FILE_RE = re.compile(r"^pH-(\d{2})(\d{3})(\d{2})(\d{2})(\d{2})-\w+\.dcs\.json$", re.I)
DAY_ZIP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.zip$")
HEX_RE = re.compile(r"^[0-9a-fA-F]{1,8}$")

# NOAA's Platform Description Table: every DCS platform, with its owner, a description and where it
# is. Since late 2026 it is served here (the old dcs1.noaa.gov address only redirects to a web page).
PDT_URL = "https://dadds-dcs-api.data.nesdis.noaa.gov/pdts/PDTS_COMPRESSED.txt"

# Header flags, as bits. The first eight mean something went wrong with the message.
FLAGS = ("parity_errors", "no_eot", "timing_error", "unexpected_message", "wrong_channel",
         "invalid_address", "uncorrectable_address", "crc_fail", "address_corrected", "pdt_incomplete")
BAD = 0xFF
SATS = {"GOES-East": 1, "GOES-West": 2}


def file_time(name):
    """When a DCS file was made, from its name: pH-YYDDDHHMMSS-A.dcs.json (UTC)."""
    m = FILE_RE.match(name)
    if not m:
        return None
    yy, doy, hh, mm, ss = map(int, m.groups())
    if not 1 <= doy <= 366 or hh > 23 or mm > 59 or ss > 60:
        return None
    return calendar.timegm((2000 + yy, 1, 1, hh, mm, ss)) + (doy - 1) * 86400


def parse_time(text):
    """'2026-11-20 03:15:05.288' to seconds, read as UTC; None if it is not a time."""
    try:
        whole = calendar.timegm((int(text[0:4]), int(text[5:7]), int(text[8:10]),
                                 int(text[11:13]), int(text[14:16]), int(text[17:19])))
        return whole + (float("0" + text[19:]) if text[19:20] == "." else 0.0)
    except (TypeError, ValueError):
        return None


def number(value):
    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


NUMBER_RE = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)")


def tidy(value):
    """A reading as SatDump prints it ('6.830000', '03.34') without the padding."""
    text = str(value).strip()
    if NUMBER_RE.fullmatch(text):
        try:
            return f"{float(text):.4f}".rstrip("0").rstrip(".") or "0"
        except ValueError:
            pass
    return text[:24]


def platform_name(dcp):
    """'DELAWARE RIVER AT LORDVILLE · NY · USGS01' from what SatDump knows about a platform.
    SatDump takes this from the station tables it downloads (NOAA's PDT, and HADS)."""
    if not isinstance(dcp, dict):
        return None
    parts = [dcp.get(key).strip() for key in ("description", "state", "agency")
             if isinstance(dcp.get(key), str) and dcp.get(key).strip()]
    return " · ".join(parts)[:200] or None


def position(lat, lon):
    """(lat, lon) if that is a real place, else (None, None). 0,0 is how "unknown" is written."""
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not isinstance(lat, bool) \
            and -90 <= lat <= 90 and -180 <= lon <= 180 and (abs(lat) > 0.001 or abs(lon) > 0.001):
        return round(lat, 5), round(lon, 5)
    return None, None


def read_pdt(path):
    """NOAA's platform table as {address: (label, details as JSON, lat, lon)}.

    Fixed-width text, one platform per line: agency [0:6], address [6:14], state [43:45],
    description [47:78], latitude [78:85] and longitude [85:93] in ten-thousandths of a degree,
    transmitter maker [94:108] and model [108:124].
    """
    found = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            address = line[6:14]
            if len(line) < 94 or not HEX_RE.match(address) or len(address) != 8:
                continue
            try:
                lat, lon = position(int(line[78:85]) / 1e4, int(line[85:93]) / 1e4)
            except ValueError:
                lat = lon = None
            keep = {"description": line[47:78].strip(), "agency": line[0:6].strip(), "state": line[43:45].strip(),
                    "manufacturer": line[94:108].strip(), "model": line[108:124].strip()}
            keep = {k: v for k, v in keep.items() if v and v.upper() != "UNKNOWN"}
            if lat is not None:
                keep["lat"], keep["lon"] = lat, lon
            if keep:
                keep["source"] = "NOAA platform table"
                label = " · ".join(keep[k] for k in ("description", "state", "agency") if k in keep)[:200]
                found[int(address, 16)] = (label or None, json.dumps(keep, separators=(",", ":")), lat, lon)
    return found


def platform_details(dcp):
    """The parts of SatDump's platform record worth keeping, as compact JSON."""
    if not isinstance(dcp, dict):
        return None
    keep = {}
    for key in ("description", "agency", "state", "lat", "lon", "manufacturer", "model", "nmc_descriptor", "transmit_interval"):
        value = dcp.get(key)
        if isinstance(value, str) and value.strip():
            keep[key] = value.strip()[:200]
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value not in (-1, 0):
            keep[key] = round(value, 5)
    sensors = [str(pe.get("name", ""))[:80] for pe in dcp.get("pe_info") or [] if isinstance(pe, dict)]
    if sensors:
        keep["sensors"] = ", ".join(dict.fromkeys(sensors))[:1200]
    return json.dumps(keep, separators=(",", ":")) if keep else None


# ---------------------------------------------------------------- fallback decoding
#
# SatDump decodes a message when it knows the station's sensors (from HADS) and the message has
# exactly the shape that list predicts. The rest arrive as text. Three kinds of that rest have a
# shape that can be read without knowing the station, and are decoded here instead. What cannot
# be known is what each reading IS: those are called "Column 3" or "Sensor 2", and the type says
# "(inferred)" so nobody takes them for more than they are.

INFERRED = " (inferred)"
LABEL_RE = re.compile(r":([A-Za-z][\w.\-]{0,23})((?:[ \t]+#?[-+]?(?:\d+\.?\d*|\.\d+))+)")
SPLIT_RE = re.compile(r"[\s,;]+")
BATTERY = "Voltage - battery (volt)"


def sensor_names(dcp):
    """[(name, minutes between readings)] for a station, as SatDump has it from HADS."""
    found = []
    for pe in (dcp or {}).get("pe_info") or []:
        if isinstance(pe, dict) and pe.get("name"):
            interval = pe.get("record_interval")
            found.append((str(pe["name"])[:80], interval if isinstance(interval, int) and interval > 0 else 0))
    return found


def labelled(text):
    """':PRS 1 #1 3048 2993 :BAT 12.1' -> one reading list per label. The label is the name."""
    out, covered = [], 0
    for match in LABEL_RE.finditer(text):
        name, body = match.groups()
        covered += len(match.group(0))
        tokens = body.split()
        age = interval = 0
        if len(tokens) >= 3 and tokens[1].startswith("#") and tokens[0].isdigit() and tokens[1][1:].isdigit():
            age, interval, tokens = int(tokens[0]), int(tokens[1][1:]), tokens[2:]  # age and spacing, in minutes
        values = [tidy(t) for t in tokens if not t.startswith("#")][:96]
        if values:
            out.append([name, interval, age, values])
    if covered < 0.5 * len(text.strip()):
        return None  # a stray label inside something else: that is not a decoding of the message
    return out[:40] or None


def close(values):
    """Readings near enough to each other to be one sensor over time."""
    return max(values) - min(values) <= max(0.3, 0.08 * max(abs(v) for v in values))


def table(text, sensors):
    """A message that is nothing but numbers.

    Lines that share a width of two or more are readings over time, one column per sensor.
    A line on its own is one sensor with its readings. Several single-number lines are one
    sensor each (a weather station's list), unless they sit close together, in which case they
    are one sensor over time.
    """
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    rows = []
    for line in lines:
        tokens = [t for t in SPLIT_RE.split(line) if t]
        if tokens and all(NUMBER_RE.fullmatch(t) for t in tokens):
            rows.append([tidy(t) for t in tokens])
    if not rows or len(rows) < 0.6 * len(lines) or sum(len(r) for r in rows) < 2:
        return None
    widths = {}
    for row in rows:
        widths[len(row)] = widths.get(len(row), 0) + 1
    wide = max((w for w, n in widths.items() if w >= 2 and (n >= 2 or len(rows) == 1)),
               key=lambda w: (widths[w], w), default=None)
    singles = [i for i, row in enumerate(rows) if len(row) == 1]
    series = len(singles) >= 3 and close([float(rows[i][0]) for i in singles])
    # A line one longer than the rest carries the table's values plus one more (often the battery).
    extras = [r[wide] for r in rows if wide and len(r) == wide + 1 and widths.get(wide + 1, 0) < widths[wide]]
    if extras:
        block = [r[:wide] for r in rows if len(r) in (wide, wide + 1)]
    else:
        block = [r for r in rows if len(r) == wide]
    # Which way round? Readings of one sensor over time vary little; readings of different sensors
    # do not. Take the reading of the table in which the sensors come out steadier.
    def spread(groups):
        return sum((max(g) - min(g)) / (max(abs(v) for v in g) + 0.01) for g in groups) / len(groups)
    by_row = False
    if wide and len(block) >= 2:
        numbers = [[float(v) for v in r] for r in block]
        by_row = spread(numbers) < spread([list(c) for c in zip(*numbers)])
    groups, columns_done, series_done = [], False, False
    for i, row in enumerate(rows):
        if wide and (len(row) == wide or (extras and len(row) == wide + 1)):
            if not columns_done:
                columns_done = True
                if by_row:
                    groups += [(f"Line {j + 1}", list(r)) for j, r in enumerate(block)]
                else:
                    groups += [(f"Column {c + 1}", [r[c] for r in block]) for c in range(wide)]
                if extras:
                    groups.append(("Last value", extras))
        elif len(row) == 1 and series:
            if not series_done:
                series_done = True
                run = [rows[j][0] for j in singles]
                groups.append((f"Lines {singles[0] + 1} to {singles[-1] + 1}", run))
        elif len(row) == 2 and series and not wide and close([float(rows[j][0]) for j in singles] + [float(row[0])]):
            # the same single-sensor list, with one more value tacked on the end of this line
            if not series_done:
                series_done = True
                run = [rows[j][0] for j in singles]
                groups.append((f"Lines {singles[0] + 1} to {singles[-1] + 1}", run))
            run.append(row[0])
            groups.append(("Last value", [row[1]]))
        else:
            groups.append((f"Line {i + 1}", row))
    if not groups or len(groups) > 40:
        return None
    named = len(sensors) == len(groups)
    return [[sensors[i][0] if named else name, 0, 0, values[:96]] for i, (name, values) in enumerate(groups)]


def coherent(values):
    """Do these look like one sensor's readings? Close together, allowing for a slow trend."""
    seen = [v for v in values if v is not None]
    return not seen or max(seen) - min(seen) <= max(30, 0.08 * max(abs(v) for v in seen))


def follows(before, after):
    """Could `after` be the same sensor carrying on from `before`? One sensor moves in small steps;
    a different sensor usually starts somewhere else entirely."""
    a = next((v for v in reversed(before) if v is not None), None)
    b = next((v for v in after if v is not None), None)
    return a is None or b is None or abs(a - b) <= max(25, 0.05 * max(abs(a), abs(b)))


def pseudobinary_b(text, sensors):
    """Sutron Pseudobinary B: 'B', a group character, an age, then readings of three characters
    each (18 bits, signed, two implied decimals), and usually one last character for the battery.

    The message does not say how many sensors it carries or how they are arranged. SatDump needs
    HADS to agree with the message exactly and otherwise leaves it. Here the arrangement is worked
    out from the readings themselves: the fewest sensors for which every sensor's readings sit
    close together, tried both as blocks and interleaved.
    """
    body = text.strip().split(None, 1)[0] if text.strip() else ""
    # "B1".."B4" is Sutron's own; "BC." and "BS." are the older kind SatDump also knows of.
    if len(body) < 6 or body[0] != "B" or not ("1" <= body[1] <= "4" or (body[1] in "CS" and len(body) >= 9)):
        return None
    data = body[3:]
    if len(data) % 3 == 2:
        return None
    battery = data[-1] if len(data) % 3 == 1 else None
    if battery:
        data = data[:-1]
        if not "+" <= battery <= "\x7f":
            return None
    if any(not ("?" <= c <= "\x7f" or c == "/") for c in data):
        return None
    readings = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        if chunk == "///":
            readings.append(None)  # the station had nothing for this slot
            continue
        raw = (ord(chunk[0]) & 63) << 12 | (ord(chunk[1]) & 63) << 6 | ord(chunk[2]) & 63
        readings.append((raw ^ 0x20000) - 0x20000)
    count = len(readings)
    if not count or count > 400 or all(r is None for r in readings):
        return None
    # Sensors one after another. Most stations send the same number of readings per sensor, so
    # first look for a block size at which the big jumps fall between blocks and not inside
    # them. That keeps a lively sensor (wind, temperature) in one piece.
    jumps = [abs(a - b) if a is not None and b is not None else 0 for a, b in zip(readings, readings[1:])]
    scores = {}
    for size in range(2, count):
        if count % size or count // size > 24:
            continue
        inside = [j for i, j in enumerate(jumps) if (i + 1) % size]
        edges = [j for i, j in enumerate(jumps) if (i + 1) % size == 0]
        scores[size] = (sum(edges) / len(edges)) / (sum(inside) / len(inside) + 1)
    best = max(scores.values(), default=0)
    if best >= 4:
        size = max(s for s, score in scores.items() if score >= 0.8 * best)
        blocks = [readings[i:i + size] for i in range(0, count, size)]
    else:
        blocks = [[r] for r in readings]
    # Then join neighbours that agree: a sensor with more readings than the others, or, with no
    # block size found, plain runs of readings that sit together.
    groups = [list(blocks[0])]
    for block in blocks[1:]:
        if follows(groups[-1], block):
            groups[-1] += block
        else:
            groups.append(list(block))
    # Sensors with similar values (two river stages, say) pass as one run when they are really
    # taking turns. That shows as a repeating pattern: every k-th reading agrees far better than
    # the readings do as a whole.
    known = [r for r in readings if r is not None]
    whole = max(known) - min(known)
    turning = False
    if whole >= 8:
        for k in range(2, 9):
            if count % k or count // k < 2:
                continue
            turns = [[r for r in readings[j::k] if r is not None] for j in range(k)]
            if sum(max(t) - min(t) for t in turns if t) <= 0.25 * whole:
                groups, turning = [readings[j::k] for j in range(k)], True
                break
    # Or interleaved, one reading from each sensor in turn: taken if it needs fewer sensors.
    for k in range(2, 2 if turning else len(groups)):
        if count % k == 0 and all(coherent(readings[j::k]) for j in range(k)):
            groups = [readings[j::k] for j in range(k)]
            break
    if len(groups) > 12:
        return None  # that many "sensors" means the readings did not fall into runs: not this shape
    standard = "1" <= body[1] <= "4"
    age = ord(body[2]) & 63 if standard else 0
    names = [n for n in sensors if n[0] != BATTERY] if battery and sensors and sensors[-1][0] == BATTERY else sensors
    named = len(names) == len(groups)
    out = [[names[i][0] if named else f"Sensor {i + 1}", names[i][1] if named else 0, age,
            [f"{v / 100:.2f}".rstrip("0").rstrip(".") if v is not None else "" for v in group][:96]]
           for i, group in enumerate(groups)]
    if battery:  # SatDump's formulas: the first is Sutron's own, the second it worked out for the BC kind
        level = 63 if battery == "?" else ord(battery) - 64
        volts = level * 0.234 + 10.6 if standard else level * 0.313 + 0.285
        out.append([BATTERY, 0, age, [f"{volts:.2f}"]])
    return out


def fallback(text, dcp):
    """(type, readings) for a message SatDump left undecoded, or None."""
    if not text or len(text) > 6000:
        return None
    sensors = sensor_names(dcp)
    for kind, decode in (("Labelled", lambda: labelled(text) if ":" in text else None),
                         ("Pseudobinary B", lambda: pseudobinary_b(text, sensors) if text.lstrip()[:1] == "B" else None),
                         ("Table", lambda: table(text, sensors))):
        try:
            readings = decode()
        except (ValueError, IndexError, TypeError):
            readings = None
        if readings:
            return kind + INFERRED, readings
    return None


def parse(raw, ftime):
    """One SatDump DCS file to (messages, missed).

    A message is (t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals, name, dcp, lat, lon).
    The times SatDump prints have been seen a whole hour away from the file's own name, so
    they are shifted by whatever whole number of hours lines the newest of them up with it.
    """
    doc = json.loads(raw)
    blocks = doc.get("blocks") if isinstance(doc, dict) else None
    rows, missed, ends = [], [], []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        head = block.get("header") or {}
        sat = SATS.get(head.get("spacecraft"), 0)
        channel = head.get("channel") if isinstance(head.get("channel"), int) else None
        if block.get("type") == "Missed Message":
            address = head.get("platform_address")
            if isinstance(address, str) and HEX_RE.match(address):
                missed.append((int(address, 16), channel, sat))
            continue
        address = head.get("corrected_address")
        if block.get("type") != "DCS Message" or not isinstance(address, str) or not HEX_RE.match(address):
            continue
        end = parse_time(head.get("message_end"))
        if end is not None:
            ends.append(end)
        flags = sum(1 << bit for bit, key in enumerate(FLAGS) if head.get(key) is True)
        if head.get("crc_pass") is False:
            flags |= 1 << FLAGS.index("crc_fail")
        values = block.get("data_values")
        vals = None
        if isinstance(values, list) and values:
            vals = json.dumps([[str(v.get("name", ""))[:80], v.get("interval") or 0, v.get("reading_age") or 0,
                                [tidy(x) for x in (v.get("values") or [])[:96]]]
                               for v in values[:40] if isinstance(v, dict)], separators=(",", ":"))
        rate = head.get("data_rate")
        dcp = block.get("dcp") if isinstance(block.get("dcp"), dict) else None
        kind = str(block.get("data_type") or "Unknown")[:24]
        if vals is None and (guess := fallback(str(block.get("data_ascii") or ""), dcp)):
            kind, vals = guess[0], json.dumps(guess[1], separators=(",", ":"))
        rows.append([parse_time(head.get("carrier_start")), int(address, 16), channel, sat,
                     int(rate) if isinstance(rate, str) and rate.isdigit() else None,
                     number(head.get("signal_strength")), number(head.get("phase_noise")),
                     number(head.get("freq_offset")), flags, kind,
                     str(block.get("data_ascii") or "")[:4000], vals, platform_name(dcp), platform_details(dcp),
                     *position((dcp or {}).get("lat"), (dcp or {}).get("lon"))])
    shift = round((ftime - max(ends)) / 3600) * 3600 if ends else 0
    for row in rows:
        t = row[0] + shift if row[0] is not None else ftime
        row[0] = int(t if abs(t - ftime) < 6 * 3600 else ftime)
    return rows, missed


class DcsStore(threading.Thread):
    """Reads SatDump's DCS files into the database and answers the DCS page's questions."""

    def __init__(self, folder, runs, history, log, stations=None):
        super().__init__(daemon=True, name="dcs")
        self.folder = folder
        self.stations_from = stations  # None, a file, or an address to fetch NOAA's platform table from
        self.stations, self.stations_stamp, self.stations_checked = {}, None, 0
        self.path = os.path.join(folder, "dcs-3.sqlite3")
        for stale in glob.glob(os.path.join(folder, "dcs-[12].sqlite3*")):  # earlier layouts: rebuilt from the files
            try:
                os.unlink(stale)
            except OSError:
                pass
        self.runs, self.history, self.log = runs, history, log
        self.seen = {}       # run name -> names of its files already read
        self.zips_done = set()
        self.attempts = {}
        self.pending = 0
        self.trimmed = 0
        self.lock = threading.Lock()
        self.cached = (0, None)
        db = self.connect()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS files(run TEXT, name TEXT, t INTEGER, lo INTEGER, hi INTEGER, n INTEGER,
                PRIMARY KEY(run, name)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS files_t ON files(t);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, t INTEGER, addr INTEGER, ch INTEGER,
                sat INTEGER, rate INTEGER, sig REAL, noise REAL, off REAL, flags INTEGER, typ TEXT, txt TEXT, vals TEXT);
            CREATE INDEX IF NOT EXISTS messages_addr ON messages(addr, t);
            CREATE TABLE IF NOT EXISTS platforms(addr INTEGER PRIMARY KEY, first INTEGER, last INTEGER, n INTEGER,
                missed INTEGER DEFAULT 0, missed_at INTEGER, ch INTEGER, sat INTEGER, rate INTEGER, sig REAL,
                noise REAL, off REAL, flags INTEGER, typ TEXT, txt TEXT, vals TEXT, name TEXT, dcp TEXT,
                lat REAL, lon REAL);
            CREATE INDEX IF NOT EXISTS platforms_last ON platforms(last);
            CREATE TABLE IF NOT EXISTS hours(h INTEGER PRIMARY KEY, n INTEGER, east INTEGER, west INTEGER,
                bad INTEGER, missed INTEGER, decoded INTEGER, files INTEGER);
        """)
        self.present = db.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None
        db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")  # readers keep working while it writes
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass  # some filesystems cannot do WAL; the default journal still works
        return db

    # -- reading SatDump's files

    def run(self):
        db = self.connect()
        while True:
            started = time.time()
            busy = False
            try:
                if time.time() - self.stations_checked > 3600:
                    self.load_stations(db)
                busy = self.sweep(db)
                if time.time() - self.trimmed > 600:
                    self.trim(db)
            except Exception:
                traceback.print_exc()
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
            # A folder holding a great many files is slow to list: look less often then.
            time.sleep(0.2 if busy else max(5, min(120, 10 * (time.time() - started))))

    def load_stations(self, db):
        """Take in NOAA's platform table, fetching a fresh copy once a day if asked to, and give
        every platform that SatDump did not describe its name and position from it."""
        self.stations_checked = time.time()
        source = self.stations_from
        if not source:
            return
        path = source
        if source.startswith(("http://", "https://")):
            path = os.path.join(self.folder, "dcs-platforms.txt")
            try:
                fresh = time.time() - os.path.getmtime(path) < 86400
            except OSError:
                fresh = False
            if not fresh:
                try:
                    request = urllib.request.Request(source, headers={"User-Agent": "GOESview"})
                    with urllib.request.urlopen(request, timeout=60) as response:
                        data = response.read(64 << 20)
                    lines = data.split(b"\n")
                    if sum(1 for line in lines if len(line) >= 94 and HEX_RE.match(line[6:14].decode("ascii", "replace"))) < 1000:
                        raise ValueError("that is not a platform table")
                    with open(path + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(path + ".tmp", path)
                    self.log(f"DCS: fetched NOAA's platform table, {len(data) / 2**20:.1f} MB")
                except Exception as exc:
                    self.log(f"DCS: could not fetch the platform table from {source}: {exc}")
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return
        if stamp == self.stations_stamp:
            return
        try:
            self.stations = read_pdt(path)
        except OSError as exc:
            self.log(f"DCS: cannot read the platform table {path}: {exc}")
            return
        self.stations_stamp = stamp
        filled = 0
        for (addr,) in db.execute("SELECT addr FROM platforms WHERE dcp IS NULL").fetchall():
            known = self.stations.get(addr)
            if known:
                db.execute("UPDATE platforms SET name = ?, dcp = ?, lat = ?, lon = ? WHERE addr = ?", (*known, addr))
                filled += 1
        db.commit()
        self.log(f"DCS: platform table loaded, {len(self.stations):,} platforms" + (f"; named {filled:,} already heard" if filled else ""))

    def sweep(self, db):
        """Read what is new. Returns True if there is more waiting than one pass took."""
        now = time.time()
        floor = now - self.history
        todo = []
        for run in self.runs():
            key = os.path.basename(run.rstrip(os.sep)) or run
            seen = self.seen.get(key)
            if seen is None:
                seen = self.seen[key] = {name for (name,) in db.execute("SELECT name FROM files WHERE run = ?", (key,))}
            try:
                for entry in os.scandir(os.path.join(run, DCS_DIR)):
                    t = file_time(entry.name)
                    if t is not None and t >= floor and entry.name not in seen:
                        todo.append((t, key, entry.path, None, entry.name))
            except OSError:
                pass
            try:  # days already packed away: only needed when the database is new or was behind
                archives = list(os.scandir(os.path.join(run, DCS_ARCHIVE_DIR)))
            except OSError:
                archives = []
            for entry in archives:
                m = DAY_ZIP_RE.match(entry.name)
                if not m or calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d")) + 86400 < floor:
                    continue
                try:
                    st = entry.stat()
                    mark = (entry.path, st.st_mtime_ns, st.st_size)
                    if mark in self.zips_done:
                        continue
                    with zipfile.ZipFile(entry.path) as zf:
                        waiting = [(file_time(name), key, entry.path, name, name) for name in zf.namelist()
                                   if name not in seen and (file_time(name) or 0) >= floor]
                except (OSError, zipfile.BadZipFile):
                    continue
                if waiting:
                    todo.extend(waiting)
                else:
                    self.zips_done.add(mark)
        todo.sort()
        self.pending = len(todo)
        if not todo:
            return False
        done, opened, deadline = 0, {}, time.time() + 2
        try:
            for t, key, path, member, name in todo:
                if time.time() > deadline:
                    break
                try:
                    if member is None:
                        if now - os.stat(path).st_mtime < 2:
                            continue  # SatDump may still be writing it
                        with open(path, "rb") as f:
                            raw = f.read()
                    else:
                        if path not in opened:
                            opened[path] = zipfile.ZipFile(path)
                        raw = opened[path].read(member)
                    rows, missed = parse(raw, t)
                except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                    tries = self.attempts[name] = self.attempts.get(name, 0) + 1
                    if tries < 5:
                        continue  # perhaps half written: try again next time
                    self.log(f"DCS: cannot read {name}: {exc}")
                    rows, missed = [], []
                self.store(db, t, key, name, rows, missed)
                self.attempts.pop(name, None)
                done += 1
        finally:
            db.commit()
            for zf in opened.values():
                zf.close()
        if done and not self.present:
            self.present = True
            self.log("DCS: SatDump is writing DCS files; the DCS page is on")
        self.pending -= done
        return self.pending > 0 and done > 0

    def store(self, db, ftime, key, name, rows, missed):
        cur = db.cursor()
        lo = hi = None
        east = west = bad = decoded = 0
        for t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals, label, dcp, lat, lon in rows:
            if dcp is None and (known := self.stations.get(addr)):  # SatDump said nothing: ask NOAA's table
                label, dcp, lat, lon = known
            cur.execute("INSERT INTO messages(t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals))
            hi = cur.lastrowid
            lo = lo or hi
            east += sat == 1
            west += sat == 2
            bad += bool(flags & BAD)
            decoded += vals is not None
            cur.execute("UPDATE platforms SET n = n + 1, first = CASE WHEN first IS NULL OR first > ? THEN ? ELSE first END "
                        "WHERE addr = ?", (t, t, addr))
            if not cur.rowcount:
                cur.execute("INSERT INTO platforms(addr, first, last, n) VALUES(?, ?, 0, 1)", (addr, t))
            cur.execute("UPDATE platforms SET last = ?, ch = ?, sat = ?, rate = ?, sig = ?, noise = ?, off = ?, flags = ?, "
                        "typ = ?, txt = ?, vals = ?, name = COALESCE(?, name), dcp = COALESCE(?, dcp), "
                        "lat = COALESCE(?, lat), lon = COALESCE(?, lon) WHERE addr = ? AND last <= ?",
                        (t, ch, sat, rate, sig, noise, off, flags, typ, txt[:600], vals, label, dcp, lat, lon, addr, t))
        for addr, ch, sat in missed:  # a platform that did not transmit in its slot
            cur.execute("INSERT OR IGNORE INTO platforms(addr, last, n, ch, sat) VALUES(?, 0, 0, ?, ?)", (addr, ch, sat))
            cur.execute("UPDATE platforms SET missed = missed + 1, missed_at = ? WHERE addr = ?", (ftime, addr))
        hour = ftime // 3600
        cur.execute("INSERT OR IGNORE INTO hours(h, n, east, west, bad, missed, decoded, files) VALUES(?,0,0,0,0,0,0,0)", (hour,))
        cur.execute("UPDATE hours SET n = n + ?, east = east + ?, west = west + ?, bad = bad + ?, missed = missed + ?, "
                    "decoded = decoded + ?, files = files + 1 WHERE h = ?",
                    (len(rows), east, west, bad, len(missed), decoded, hour))
        cur.execute("INSERT OR REPLACE INTO files(run, name, t, lo, hi, n) VALUES(?,?,?,?,?,?)", (key, name, ftime, lo, hi, len(rows)))
        self.seen[key].add(name)

    def trim(self, db):
        """Drop messages older than the history window. Platforms and hourly totals stay."""
        self.trimmed = time.time()
        cutoff = int(time.time() - self.history)
        old = db.execute("SELECT run, name, lo, hi FROM files WHERE t < ?", (cutoff,)).fetchall()
        for run, name, lo, hi in old:
            if lo is not None:
                db.execute("DELETE FROM messages WHERE id BETWEEN ? AND ?", (lo, hi))
            self.seen.get(run, set()).discard(name)
        db.execute("DELETE FROM files WHERE t < ?", (cutoff,))
        db.execute("DELETE FROM hours WHERE h < ?", ((cutoff - 30 * 86400) // 3600,))
        db.commit()
        if old:
            self.log(f"DCS: dropped the messages of {len(old)} files older than the {self.history / 86400:g}-day history "
                     f"from GOESview's own database (SatDump's files are not touched by this)")

    # -- answering the page

    def brief(self):
        """The few numbers the header and the Live page want, or None when there is no DCS data."""
        if not self.present:
            return None
        s = self.summary()
        return {"rate": s["rate"], "heard": s["heard"]["hour"], "last": s["last"]}

    def summary(self):
        with self.lock:
            when, value = self.cached
            if value is not None and time.time() - when < 5:
                return value
            db = self.connect()
            try:
                value = self._summary(db)
            finally:
                db.close()
            self.cached = (time.time(), value)
            return value

    def _summary(self, db):
        now = int(time.time())
        minute = now // 60
        lo = db.execute("SELECT min(lo) FROM files WHERE t >= ? AND lo IS NOT NULL", (now - 3700,)).fetchone()[0]
        minutes, channels, types, sats, bad, total, decoded, inferred = {}, {}, {}, {1: 0, 2: 0}, 0, 0, 0, 0
        if lo is not None:
            minutes = dict(db.execute("SELECT t / 60, count(*) FROM messages WHERE id >= ? AND t >= ? GROUP BY 1",
                                      (lo, (minute - 60) * 60)))
            for ch, sat, typ, flagged, has_values, n in db.execute(
                    f"SELECT ch, sat, typ, (flags & {BAD}) != 0, vals IS NOT NULL, count(*) FROM messages "
                    "WHERE id >= ? AND t >= ? GROUP BY 1, 2, 3, 4, 5", (lo, now - 3600)):
                total += n
                decoded += n if has_values else 0
                inferred += n if has_values and typ and typ.endswith(INFERRED) else 0
                channels[ch] = channels.get(ch, 0) + n
                types[typ] = types.get(typ, 0) + n
                sats[sat] = sats.get(sat, 0) + n
                bad += n if flagged else 0
        series = [minutes.get(m, 0) for m in range(minute - 60, minute)]
        recent = [n for n in series[-6:-1]]  # the last few whole minutes; the newest is still filling
        hour, day, heard, known = db.execute(
            "SELECT sum(last >= ?), sum(last >= ?), sum(n > 0), count(*) FROM platforms", (now - 3600, now - 86400)).fetchone()
        hours = [list(row) for row in db.execute(
            "SELECT h * 3600, n, east, west, bad, missed, decoded FROM hours WHERE h >= ? ORDER BY h", (now // 3600 - 72,))]
        missed = db.execute("SELECT sum(missed) FROM hours WHERE h >= ?", (now // 3600 - 1,)).fetchone()[0]
        files, first, last = db.execute("SELECT count(*), min(t), max(t) FROM files").fetchone()
        try:
            size = sum(os.path.getsize(self.path + tail) for tail in ("", "-wal") if os.path.exists(self.path + tail))
        except OSError:
            size = 0
        return {
            "now": now, "rate": round(sum(recent) / len(recent)) if recent else 0, "minutes": series,
            "hour": {"n": total, "east": sats.get(1, 0), "west": sats.get(2, 0), "bad": bad, "missed": missed or 0,
                     "decoded": decoded, "inferred": inferred,
                     "types": sorted(types.items(), key=lambda kv: -kv[1]),
                     "channels": sorted((ch, n) for ch, n in channels.items() if ch is not None)},
            "heard": {"hour": hour or 0, "day": day or 0, "ever": heard or 0, "known": known or 0},
            "hours": hours, "last": last, "first": first,
            "store": {"history": self.history, "size": size, "files": files, "pending": self.pending,
                      "stations": len(self.stations)},
        }

    @staticmethod
    def readings(vals, limit=3):
        """'Stage 3.91 · Battery 12.4' from a message's decoded values."""
        try:
            found = [f"{name} {tidy(values[0])}" for name, _, _, values in json.loads(vals)
                     if values and NUMBER_RE.fullmatch(values[0].strip())]
            return " · ".join(found[:limit]) or None
        except (TypeError, ValueError):
            return None

    def platforms(self, q=None, kind=None, sort=None, limit=100, offset=0):
        where, args = ["n > 0"], []
        if kind == "values":
            where.append("vals IS NOT NULL")
        elif kind == "text":
            where.append("vals IS NULL AND typ = 'ASCII'")
        elif kind == "binary":
            where.append("vals IS NULL AND typ != 'ASCII'")
        elif kind == "inferred":
            where.append(f"typ LIKE '%{INFERRED}'")
        elif kind == "flagged":
            where.append(f"(flags & {BAD}) != 0")
        elif kind == "missed":
            where = ["missed > 0"]
        q = (q or "").strip()
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses, extra = ["txt LIKE ? ESCAPE '\\'", "name LIKE ? ESCAPE '\\'"], [like, like]
            if HEX_RE.match(q):
                clauses.append("printf('%08x', addr) LIKE ?")
                extra.append(q.lower() + "%")
            where.append("(" + " OR ".join(clauses) + ")")
            args += extra
        order = {"count": "n DESC", "strong": "sig DESC", "weak": "sig IS NULL, sig ASC", "address": "addr ASC",
                 "missed": "missed DESC"}.get(sort, "last DESC")
        db = self.connect()
        try:
            rows = db.execute(
                "SELECT addr, last, n, missed, ch, sat, rate, sig, flags, typ, substr(txt, 1, 160), vals, name "
                f"FROM platforms WHERE {' AND '.join(where)} ORDER BY {order}, addr LIMIT ? OFFSET ?",
                args + [limit + 1, offset]).fetchall()
            count = db.execute(f"SELECT count(*) FROM platforms WHERE {' AND '.join(where)}", args).fetchone()[0]
        finally:
            db.close()
        out = [{"a": f"{addr:08x}", "t": last, "n": n, "m": missed, "ch": ch, "sat": sat, "rate": rate, "sig": sig,
                "f": flags or 0, "ty": typ, "x": txt, "v": self.readings(vals), "nm": name}
               for addr, last, n, missed, ch, sat, rate, sig, flags, typ, txt, vals, name in rows[:limit]]
        return {"items": out, "more": len(rows) > limit, "count": count}

    def map(self, hours=None):
        """Every platform heard that has a position: [address, lat, lon, last heard, kind], where
        kind is 3 decoded by SatDump, 2 inferred, 1 plain text, 0 undecoded."""
        with self.lock:
            when, key, value = getattr(self, "map_cache", (0, None, None))
            if value is not None and key == hours and time.time() - when < 20:
                return value
            now = int(time.time())
            db = self.connect()
            try:
                rows = db.execute("SELECT addr, lat, lon, last, typ, vals IS NOT NULL FROM platforms "
                                  "WHERE n > 0 AND lat IS NOT NULL AND lon IS NOT NULL AND last >= ? ORDER BY last",
                                  (now - int(hours * 3600) if hours else 0,)).fetchall()
                placed, heard = db.execute("SELECT sum(lat IS NOT NULL), count(*) FROM platforms WHERE n > 0 AND last >= ?",
                                           (now - int(hours * 3600) if hours else 0,)).fetchone()
            finally:
                db.close()
            points = [[f"{addr:08x}", lat, lon, last,
                       (2 if (typ or "").endswith(INFERRED) else 3) if decoded else (1 if typ == "ASCII" else 0)]
                      for addr, lat, lon, last, typ, decoded in rows]
            value = {"now": now, "points": points, "placed": placed or 0, "heard": heard or 0}
            self.map_cache = (time.time(), hours, value)
            return value

    def platform(self, address, limit=600):
        if not HEX_RE.match(address or ""):
            return None
        addr = int(address, 16)
        db = self.connect()
        try:
            row = db.execute("SELECT first, last, n, missed, missed_at, ch, sat, rate, sig, noise, off, flags, typ, name, dcp "
                             "FROM platforms WHERE addr = ?", (addr,)).fetchone()
            if not row:
                return None
            messages = db.execute("SELECT t, ch, sat, sig, noise, off, flags, typ, txt, vals FROM messages "
                                  "WHERE addr = ? ORDER BY t DESC LIMIT ?", (addr, limit)).fetchall()
        finally:
            db.close()
        first, last, n, missed, missed_at, ch, sat, rate, sig, noise, off, flags, typ, name, dcp = row
        try:
            details = json.loads(dcp) if dcp else None
        except ValueError:
            details = None
        return {"a": f"{addr:08x}", "first": first, "t": last, "n": n, "m": missed, "mt": missed_at, "ch": ch, "sat": sat,
                "rate": rate, "sig": sig, "noise": noise, "off": off, "f": flags or 0, "ty": typ, "nm": name, "dcp": details,
                "history": self.history, "flags": list(FLAGS),
                "messages": [{"t": t, "ch": c, "sat": s, "sig": g, "noise": z, "off": o, "f": f or 0, "ty": y, "x": x,
                              "v": [[name, interval, age, [tidy(x) for x in values]]
                                    for name, interval, age, values in json.loads(v)] if v else None}
                             for t, c, s, g, z, o, f, y, x, v in messages]}


# ------------------------------------------------------------------ archive

class DcsArchiver(threading.Thread):
    """Packs each finished day's DCS files into one zip, then deletes the loose files.

    A day of DCS is some 24,000 files of about 100 kB. They compress about ninefold, and
    one zip takes far less room and far fewer directory entries than the loose files.
    Today's files are left alone; once a UTC day is over (plus an hour's grace) its files
    go into <run>/DCS-archive/<day>.zip under their original names.

    A loose file is deleted only after the archive holding it has been written to a
    temporary file, flushed to disk, read back and compared byte for byte, and moved
    into place. If any step fails, the loose files stay and the archive is not touched.
    """
    GRACE = 3600

    def __init__(self, runs, dry_run, log, on_disk):
        super().__init__(daemon=True, name="dcs-archiver")
        self.runs, self.dry_run, self.log, self.on_disk = runs, dry_run, log, on_disk
        self.last = None
        self.days = 0

    def run(self):
        time.sleep(75)  # let the first scan and its follow-up work settle
        while True:
            try:
                self.sweep()
            except Exception:
                traceback.print_exc()
            time.sleep(1800)

    def sweep(self):
        boundary = int(time.time() - self.GRACE) // 86400 * 86400  # start of the current UTC day
        days = 0
        for run in self.runs():
            by_day = {}
            try:
                # A link leads out of the run: nothing behind one is packed or removed.
                linked = os.path.islink(os.path.join(run, DCS_DIR)) or os.path.islink(os.path.join(run, DCS_ARCHIVE_DIR))
                entries = [] if linked else list(os.scandir(os.path.join(run, DCS_DIR)))
            except OSError:
                entries = []
            for entry in entries:
                when = file_time(entry.name)
                if when is not None and when < boundary and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    by_day.setdefault(time.strftime("%Y-%m-%d", time.gmtime(when)), []).append(entry.name)
            for day in sorted(by_day):
                self.pack(run, day, sorted(by_day[day]))
            try:
                days += sum(1 for e in os.scandir(os.path.join(run, DCS_ARCHIVE_DIR)) if DAY_ZIP_RE.match(e.name))
            except OSError:
                pass
        self.days = days

    def pack(self, run, day, names):
        label = f"{day} in {os.path.basename(run)}"
        if self.dry_run:
            self.log(f"DCS archive (dry run): would pack {len(names)} DCS files from {label}")
            return
        folder, out = os.path.join(run, DCS_DIR), os.path.join(run, DCS_ARCHIVE_DIR)
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
            with zipfile.ZipFile(tmp) as zf:  # read every file back before trusting the archive
                for name in names:
                    with open(os.path.join(folder, name), "rb") as f:
                        if zf.read(name) != f.read():
                            raise ValueError(f"{name} did not read back identically")
            os.replace(tmp, target)
        except Exception as exc:
            self.log(f"DCS archive: {label} NOT packed, loose files left as they were: {exc}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
        freed = 0
        for name in names:  # the archive is complete, verified and in place: free the space
            try:
                path = os.path.join(folder, name)
                freed += self.on_disk(os.lstat(path))
                os.unlink(path)
            except OSError:
                pass
        packed = self.on_disk(os.lstat(target))
        self.last = {"t": int(time.time()), "day": day, "n": len(names), "freed": freed, "size": packed}
        self.log(f"DCS archive: packed {len(names)} DCS files from {label}: {freed / 2**20:.0f} MB of loose files "
                 f"now {packed / 2**20:.0f} MB in {DCS_ARCHIVE_DIR}/{day}.zip ({time.time() - started:.0f}s)")

    def status(self):
        return {"days": self.days, "last": self.last, "dry": self.dry_run}


# ---------------------------------------------------------------- retention

def retention_units(runs):
    """What DCS retention may delete, as [time, size or None, path, is_folder], oldest first.

    Only SatDump's own DCS files, by name, in <run>/DCS/, and the day archives GOESview
    made from them in <run>/DCS-archive/. Anything else in those folders is left alone.
    """
    found = []
    for run in runs:
        try:
            if os.path.islink(os.path.join(run, DCS_DIR)):
                raise OSError("a link, not this run's own folder")
            for entry in os.scandir(os.path.join(run, DCS_DIR)):
                when = file_time(entry.name)
                if when is not None and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    found.append([when, None, entry.path, False])
        except OSError:
            pass
        try:
            if os.path.islink(os.path.join(run, DCS_ARCHIVE_DIR)):
                raise OSError("a link, not this run's own folder")
            for entry in os.scandir(os.path.join(run, DCS_ARCHIVE_DIR)):
                m = DAY_ZIP_RE.match(entry.name)
                if m and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    found.append([calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d")) + 86399, None, entry.path, False])
        except (OSError, ValueError):
            pass
    found.sort(key=lambda unit: unit[0])
    return found
