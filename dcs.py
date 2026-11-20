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
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import zipfile

DCS_DIR = "DCS"
DCS_ARCHIVE_DIR = "DCS-archive"  # in a run folder: one zip of DCS files per finished day
FILE_RE = re.compile(r"^pH-(\d{2})(\d{3})(\d{2})(\d{2})(\d{2})-\w+\.dcs\.json$", re.I)
DAY_ZIP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.zip$")
HEX_RE = re.compile(r"^[0-9a-fA-F]{1,8}$")

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


def platform_name(dcp):
    """A label for a platform from whatever SatDump knows about it. SatDump fills this in from
    NOAA's platform table when it has one; the field names are its own, so look for likely ones."""
    if not isinstance(dcp, dict):
        return None
    lowered = {str(k).lower(): v for k, v in dcp.items()}
    parts = []
    for keys in (("description", "location", "name", "site", "station"), ("agency", "owner", "organization", "state")):
        for key in keys:
            value = lowered.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
    return " · ".join(parts)[:200] or None


def parse(raw, ftime):
    """One SatDump DCS file to (messages, missed).

    A message is (t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals, name, dcp).
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
                                [str(x)[:24] for x in (v.get("values") or [])[:96]]]
                               for v in values[:40] if isinstance(v, dict)], separators=(",", ":"))
        rate = head.get("data_rate")
        dcp = block.get("dcp") if isinstance(block.get("dcp"), dict) else None
        rows.append([parse_time(head.get("carrier_start")), int(address, 16), channel, sat,
                     int(rate) if isinstance(rate, str) and rate.isdigit() else None,
                     number(head.get("signal_strength")), number(head.get("phase_noise")),
                     number(head.get("freq_offset")), flags, str(block.get("data_type") or "Unknown")[:24],
                     str(block.get("data_ascii") or "")[:4000], vals, platform_name(dcp),
                     json.dumps(dcp, separators=(",", ":"))[:4000] if dcp else None])
    shift = round((ftime - max(ends)) / 3600) * 3600 if ends else 0
    for row in rows:
        t = row[0] + shift if row[0] is not None else ftime
        row[0] = int(t if abs(t - ftime) < 6 * 3600 else ftime)
    return rows, missed


class DcsStore(threading.Thread):
    """Reads SatDump's DCS files into the database and answers the DCS page's questions."""

    def __init__(self, folder, runs, history, log):
        super().__init__(daemon=True, name="dcs")
        self.path = os.path.join(folder, "dcs-1.sqlite3")
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
                noise REAL, off REAL, flags INTEGER, typ TEXT, txt TEXT, vals TEXT, name TEXT, dcp TEXT);
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
        for t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals, label, dcp in rows:
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
                        "typ = ?, txt = ?, vals = ?, name = COALESCE(?, name), dcp = COALESCE(?, dcp) "
                        "WHERE addr = ? AND last <= ?",
                        (t, ch, sat, rate, sig, noise, off, flags, typ, txt[:600], vals, label, dcp, addr, t))
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
        minutes, channels, types, sats, bad, total = {}, {}, {}, {1: 0, 2: 0}, 0, 0
        if lo is not None:
            minutes = dict(db.execute("SELECT t / 60, count(*) FROM messages WHERE id >= ? AND t >= ? GROUP BY 1",
                                      (lo, (minute - 60) * 60)))
            for ch, sat, typ, flagged, n in db.execute(
                    f"SELECT ch, sat, typ, (flags & {BAD}) != 0, count(*) FROM messages WHERE id >= ? AND t >= ? "
                    "GROUP BY 1, 2, 3, 4", (lo, now - 3600)):
                total += n
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
                     "types": sorted(types.items(), key=lambda kv: -kv[1]),
                     "channels": sorted((ch, n) for ch, n in channels.items() if ch is not None)},
            "heard": {"hour": hour or 0, "day": day or 0, "ever": heard or 0, "known": known or 0},
            "hours": hours, "last": last, "first": first,
            "store": {"history": self.history, "size": size, "files": files, "pending": self.pending},
        }

    @staticmethod
    def readings(vals, limit=3):
        """'Stage 3.91 · Battery 12.4' from a message's decoded values."""
        try:
            found = [f"{name} {values[0]}" for name, _, _, values in json.loads(vals)
                     if values and re.fullmatch(r"[-+]?\d*\.?\d+", values[0])]
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
                              "v": json.loads(v) if v else None} for t, c, s, g, z, o, f, y, x, v in messages]}


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
                entries = list(os.scandir(os.path.join(run, DCS_DIR)))
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
            for entry in os.scandir(os.path.join(run, DCS_DIR)):
                when = file_time(entry.name)
                if when is not None and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    found.append([when, None, entry.path, False])
        except OSError:
            pass
        try:
            for entry in os.scandir(os.path.join(run, DCS_ARCHIVE_DIR)):
                m = DAY_ZIP_RE.match(entry.name)
                if m and not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                    found.append([calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d")) + 86399, None, entry.path, False])
        except (OSError, ValueError):
            pass
    found.sort(key=lambda unit: unit[0])
    return found
