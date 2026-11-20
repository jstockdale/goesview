"""System monitor for GOESview: is everything that feeds the viewer healthy, and how has it been?

Three sources, sampled every ten seconds:

    SatDump's log   signal-to-noise, Viterbi error rate, lock, files written, warnings.
                    SatDump writes a progress line every ten seconds while a pipeline runs,
                    so the log also shows when it has stopped or hung. Read-only.
    the machine     processor, memory, temperature, disk, battery, and the SatDump and
                    GOESview processes themselves (Linux /proc and /sys; absent elsewhere).
    GOESview        how recently each kind of product arrived, and its own queues.

The last hour is kept in memory at ten-second steps. Every minute is also saved to
monitor-1.sqlite3 in the cache folder, for 30 days, and on start the history of the signal
is filled in from whatever SatDump's current log still holds.
"""

import calendar
import collections
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback

STEP = 10
KEEP_DAYS = 30
LINE_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d) - (\d\d)/(\d\d)/(\d{4})\] \((\w)\) (.*)$")
SNR_RE = re.compile(r"SNR : ([-\d.]+)dB, Peak SNR: ([-\d.]+)dB")
VITERBI_RE = re.compile(r"Viterbi : (\w+) BER : ([-\d.]+)")
DEFRAMER_RE = re.compile(r"Deframer : (\w+)")
SET_RE = re.compile(r"^Set (.+?) (samplerate|frequency) to (\d+)")
PARAM_RE = re.compile(r"^\s+- (\w+) : (.*)$")
SUMS = ("lrit", "written", "warn", "err", "imagery", "weather", "text", "dcs")  # per-minute counts
ORDER = {"ok": 0, "off": 0, "warn": 1, "bad": 2}


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def span(seconds):
    seconds = max(0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.0f} days"


class Process:
    """One process's share of a processor and its memory, from /proc."""
    TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def __init__(self, names=None, pid=None):
        self.names, self.pid, self.fixed = names, pid, pid is not None
        self.before = None

    def find(self):
        if self.fixed:
            return self.pid
        if self.pid and (read(f"/proc/{self.pid}/comm") or "").strip() in self.names:
            return self.pid
        self.pid = self.before = None
        try:
            for entry in os.listdir("/proc"):
                if entry.isdigit() and (read(f"/proc/{entry}/comm") or "").strip() in self.names:
                    self.pid = int(entry)
                    break
        except OSError:
            pass
        return self.pid

    def sample(self, uptime):
        """{"pid", "cpu" percent of one core, "rss" bytes, "up" seconds}, or None if it is not running."""
        pid = self.find()
        stat = read(f"/proc/{pid}/stat") if pid else None
        if not stat:
            return None
        fields = stat.rsplit(")", 1)[-1].split()
        ticks, started = int(fields[11]) + int(fields[12]), int(fields[19]) / self.TICK
        now = time.time()
        cpu = None
        if self.before and now > self.before[0]:
            cpu = round(100 * (ticks - self.before[1]) / self.TICK / (now - self.before[0]), 1)
        self.before = (now, ticks)
        statm = (read(f"/proc/{pid}/statm") or "0 0").split()
        return {"pid": pid, "cpu": cpu, "rss": int(statm[1]) * self.PAGE, "up": max(0, uptime - started) if uptime else None}


class Monitor(threading.Thread):
    def __init__(self, folder, app, log, satdump_log=None):
        super().__init__(daemon=True, name="monitor")
        self.app, self.log = app, log
        self.path = os.path.join(folder, "monitor-1.sqlite3")
        self.where = satdump_log  # a log file, or the folder SatDump keeps its logs in
        self.started = time.time()
        self.lock = threading.Lock()
        self.ring = collections.deque(maxlen=3600 // STEP)   # the last hour, one sample per step
        self.minutes = {}                                    # minute -> sums being gathered
        self.messages = collections.deque(maxlen=40)         # SatDump's recent warnings and errors
        self.sd = {"params": {}}                             # what the log says about the receiver
        self.live = {}                                       # newest signal numbers, with their time
        self.file = self.file_path = None
        self.file_checked = 0
        self.skew = 0
        self.cpu_before = None
        self.satdump = Process(("satdump-ui", "satdump"))
        self.me = Process(pid=os.getpid())
        self.host = {}
        self.pruned = 0
        db = self.connect()
        db.execute("CREATE TABLE IF NOT EXISTS minutes(t INTEGER PRIMARY KEY, d TEXT) WITHOUT ROWID")
        db.commit()
        db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        return db

    # -- SatDump's log

    def newest_log(self):
        if not self.where:
            return None
        if os.path.isfile(self.where):
            return self.where
        try:
            return max(glob.glob(os.path.join(self.where, "satdump*.log")), key=os.path.getmtime)
        except (ValueError, OSError):
            return None

    @staticmethod
    def clock_skew(path, size):
        """SatDump stamps its log in UTC here, but a build that used local time would make every
        reading look hours old. Compare the last line's stamp with when the file was last written
        and correct by the nearest quarter hour if they disagree by more than ten minutes."""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", "replace").splitlines()
            for line in reversed(tail):
                if m := LINE_RE.match(line):
                    hh, mm, ss, day, month, year = (int(x) for x in m.groups()[:6])
                    gap = os.path.getmtime(path) - calendar.timegm((year, month, day, hh, mm, ss))
                    return round(gap / 900) * 900 if abs(gap) > 600 else 0
        except (OSError, ValueError):
            pass
        return 0

    def read_log(self, catching_up=False):
        """Take in whatever SatDump has logged since last time."""
        now = time.time()
        if self.file is None or now - self.file_checked > 30:
            self.file_checked = now
            path = self.newest_log()
            if path != self.file_path:  # SatDump starts a new log each time it starts
                if self.file:
                    self.file.close()
                self.file, self.file_path = None, path
                self.sd, self.live = {"params": {}}, {}
                if path:
                    try:
                        self.file = open(path, "r", errors="replace")
                        size = os.path.getsize(path)
                        self.skew = self.clock_skew(path, size)
                        if size > 40 << 20:  # a very long run: the recent part is plenty
                            self.file.seek(size - (40 << 20))
                            self.file.readline()
                        catching_up = True
                    except OSError:
                        self.file = None
        if not self.file:
            return
        while True:
            where = self.file.tell()
            line = self.file.readline()
            if not line.endswith("\n"):  # nothing more, or a line still being written
                self.file.seek(where)
                break
            m = LINE_RE.match(line)
            if not m:
                continue
            hh, mm, ss, day, month, year, level, text = m.groups()
            t = calendar.timegm((int(year), int(month), int(day), int(hh), int(mm), int(ss))) + self.skew
            self.take(t, level, text, catching_up)

    def take(self, t, level, text, catching_up):
        sums = self.minutes.setdefault(t // 60, {})
        add = lambda key, n=1: sums.__setitem__(key, sums.get(key, 0) + n)
        if text.startswith("Progress"):
            if m := SNR_RE.search(text):
                snr, peak = float(m.group(1)), float(m.group(2))
                add("snr_sum", snr), add("snr_n")
                sums["snr_min"] = min(sums.get("snr_min", snr), snr)
                sums["peak"] = max(sums.get("peak", peak), peak)
                self.live.update(snr=round(snr, 2), peak=round(peak, 2), t=t)
            if m := VITERBI_RE.search(text):
                ber, synced = float(m.group(2)), m.group(1) == "SYNCED"
                deframer = DEFRAMER_RE.search(text)
                framed = bool(deframer and deframer.group(1) == "SYNCED")
                add("ber_sum", ber), add("ber_n"), add("sync_n", 1 if synced and framed else 0)
                sums["ber_max"] = max(sums.get("ber_max", ber), ber)
                self.live.update(ber=round(ber, 4), viterbi=synced, deframer=framed, t=t)
            return
        if text.startswith("New LRIT file"):
            return add("lrit")
        if text.startswith("Writing file") or text.startswith("Saving /"):
            return add("written")
        if level in ("W", "E", "C"):
            if "Usage :" in text or "Extra options" in text or text.startswith(" --"):
                return  # SatDump prints its usage as errors every time it starts
            add("err" if level in ("E", "C") else "warn")
            if self.messages and self.messages[-1][2] == text and t - self.messages[-1][0] < 3600:
                self.messages[-1] = (t, level, text, self.messages[-1][3] + 1)  # the same thing again: count it
            else:
                self.messages.append((t, level, text[:300], 1))
            return
        if text.startswith("Starting SatDump"):
            self.sd.update(version=text[len("Starting SatDump"):].strip(), opened=t)
        elif m := SET_RE.match(text):
            self.sd["device"] = m.group(1)
            self.sd[m.group(2)] = int(m.group(3))
        elif text.startswith("Start processing"):
            self.sd["pipeline"] = t
        elif text.startswith("Decoding to ") and not text.rstrip().endswith((".cadu", ".soft", ".frm")):
            self.sd["output"] = os.path.basename(text[len("Decoding to "):].strip().rstrip("/"))
        elif m := PARAM_RE.match(text):
            if m.group(1) in ("parse_dcs", "samplerate", "baseband_format", "write_lrit", "fill_missing", "freq_shift"):
                self.sd["params"][m.group(1)] = m.group(2).strip().strip('"')

    # -- the machine

    def sample_host(self):
        host = {}
        uptime = None
        if text := read("/proc/uptime"):
            uptime = host["up"] = float(text.split()[0])
        if text := read("/proc/stat"):
            fields = [int(x) for x in text.split("\n", 1)[0].split()[1:]]
            idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
            if self.cpu_before and total > self.cpu_before[1]:
                host["cpu"] = round(100 * (1 - (idle - self.cpu_before[0]) / (total - self.cpu_before[1])), 1)
            self.cpu_before = (idle, total)
        if text := read("/proc/loadavg"):
            host["load"] = float(text.split()[0])
        host["cores"] = os.cpu_count() or 1
        if text := read("/proc/meminfo"):
            mem = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in text.splitlines() if ":" in line and len(line.split()) > 1}
            if "MemTotal" in mem and "MemAvailable" in mem:
                host["mem_total"], host["mem_free"] = mem["MemTotal"], mem["MemAvailable"]
                host["mem"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
        temps = []
        for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            if (text := read(zone)) and text.strip().lstrip("-").isdigit():
                temps.append(int(text) / 1000)
        if temps:
            host["temp"] = round(max(temps), 1)
        for supply in glob.glob("/sys/class/power_supply/*"):
            if (level := read(os.path.join(supply, "capacity"))) and level.strip().isdigit():
                host["battery"] = int(level)
                host["power"] = (read(os.path.join(supply, "status")) or "").strip() or None
                break
        try:
            disk = shutil.disk_usage(self.app.catalog.root)
            host["disk_total"], host["disk_free"] = disk.total, disk.free
            host["disk"] = round(100 * (1 - disk.free / disk.total), 1)
        except OSError:
            pass
        host["satdump"] = self.satdump.sample(uptime)
        host["goesview"] = self.me.sample(uptime)
        return host

    # -- the loop

    def run(self):
        try:
            self.read_log(catching_up=True)
            self.flush(time.time(), backfill=True)
        except Exception:
            traceback.print_exc()
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(STEP - time.time() % STEP)

    def tick(self):
        now = time.time()
        host = self.sample_host()
        with self.lock:
            self.read_log()
            self.host = host
            fresh = self.live if self.live and now - self.live.get("t", 0) < 3 * STEP else {}
            synced = bool(fresh.get("viterbi") and fresh.get("deframer")) if fresh else None
            sample = {"t": int(now), "snr": fresh.get("snr"), "peak": fresh.get("peak"), "ber": fresh.get("ber"),
                      "sync": None if synced is None else 100 * int(synced), "cpu": host.get("cpu"), "load": host.get("load"),
                      "mem": host.get("mem"), "temp": host.get("temp"), "battery": host.get("battery"),
                      "sd_cpu": (host.get("satdump") or {}).get("cpu"), "gv_cpu": (host.get("goesview") or {}).get("cpu")}
            self.ring.append(sample)
            sums = self.minutes.setdefault(int(now) // 60, {})
            for key in ("cpu", "load", "mem", "temp", "battery", "sd_cpu", "gv_cpu"):
                if sample[key] is not None:
                    sums[key + "_sum"] = sums.get(key + "_sum", 0) + sample[key]
                    sums[key + "_n"] = sums.get(key + "_n", 0) + 1
            if host.get("disk") is not None:
                sums["disk"] = host["disk"]
            self.flush(now)
        if now - self.pruned > 6 * 3600:
            self.pruned = now
            db = self.connect()
            db.execute("DELETE FROM minutes WHERE t < ?", (int(now) - KEEP_DAYS * 86400,))
            db.commit()
            db.close()

    def flush(self, now, backfill=False):
        """Save every finished minute. A minute seen only in the log (history from before GOESview
        started) never replaces one that was sampled live."""
        current = int(now) // 60
        done = sorted(minute for minute in self.minutes if minute < current)
        if not done:
            return
        rates = {}
        if not backfill:
            try:  # what GOESview itself took in during those minutes
                stats = self.app.catalog.stats()
                for section, counts in stats["rate"].items():
                    for back, n in enumerate(reversed(counts)):
                        rates.setdefault(stats["now"] // 60 - back, {})[section] = n
                if self.app.dcs and self.app.dcs.present:
                    summary = self.app.dcs.summary()
                    for back, n in enumerate(reversed(summary["minutes"])):
                        rates.setdefault(summary["now"] // 60 - 1 - back, {})["dcs"] = n
            except Exception:
                traceback.print_exc()
        db = self.connect()
        try:
            for minute in done:
                sums = self.minutes.pop(minute)
                row = {}
                for key in ("cpu", "load", "mem", "temp", "battery", "sd_cpu", "gv_cpu", "snr", "ber"):
                    if sums.get(key + "_n"):
                        row[key] = round(sums[key + "_sum"] / sums[key + "_n"], 4 if key == "ber" else 2)
                if sums.get("ber_n"):
                    row["sync"] = round(100 * sums.get("sync_n", 0) / sums["ber_n"], 1)
                for key in ("snr_min", "peak", "ber_max", "disk"):
                    if key in sums:
                        row[key] = round(sums[key], 4)
                for key in ("lrit", "written", "warn", "err"):
                    if sums.get(key):
                        row[key] = sums[key]
                row.update(rates.get(minute, {}))
                if row:
                    db.execute(f"INSERT OR {'IGNORE' if backfill else 'REPLACE'} INTO minutes(t, d) VALUES(?, ?)",
                               (minute * 60, json.dumps(row, separators=(",", ":"))))
            db.commit()
        finally:
            db.close()

    # -- answers

    def checks(self):
        """The traffic lights: [{"id", "label", "state" ok|warn|bad|off, "text"}]."""
        now = time.time()
        host, live, sd = self.host, self.live, self.sd
        out = []

        def check(key, label, state, text):
            out.append({"id": key, "label": label, "state": state, "text": text})

        process = host.get("satdump")
        watching = bool(self.file_path)
        if process:
            check("satdump", "SatDump", "ok", f"Running for {span(process['up'] or 0)}" + (f", {sd['version']}" if sd.get("version") else ""))
        elif watching:
            check("satdump", "SatDump", "bad", "Not running on this machine")
        else:
            check("satdump", "SatDump", "off", "Not found on this machine, so its signal cannot be watched from here")
        if watching and process:
            quiet = now - live["t"] if live.get("t") else None
            if quiet is None:
                check("pipeline", "Pipeline", "warn", "SatDump is open but no pipeline has reported progress")
            elif quiet < 45:
                check("pipeline", "Pipeline", "ok", "Reporting every 10 seconds" + (f", started {span(now - sd['pipeline'])} ago" if sd.get("pipeline") else ""))
            else:
                check("pipeline", "Pipeline", "bad" if quiet > 120 else "warn",
                      f"No progress from SatDump for {span(quiet)}: the pipeline is stopped or SatDump has hung")
            if quiet is not None and quiet < 120:
                locked = live.get("viterbi") and live.get("deframer")
                check("lock", "Lock", "ok" if locked else "bad",
                      "Viterbi and deframer in sync" if locked else
                      f"Not locked: Viterbi {'in sync' if live.get('viterbi') else 'NOT in sync'}, deframer {'in sync' if live.get('deframer') else 'NOT in sync'}")
                if live.get("snr") is not None:
                    check("snr", "Signal", "ok" if locked else "warn", f"{live['snr']:.1f} dB (peak {live['peak']:.1f} dB)")
                if live.get("ber") is not None:
                    ber = live["ber"]
                    check("ber", "Error rate", "ok" if ber < 0.05 else "warn" if ber < 0.1 else "bad", f"Viterbi BER {ber:.3f}")
        try:
            stats = self.app.catalog.stats()
            for section, label, fine, late in (("imagery", "Imagery", 1800, 5400), ("weather", "Weather products", 1800, 5400),
                                               ("text", "Text bulletins", 300, 900)):
                last = stats["last"].get(section)
                if last:
                    age = now - last
                    check(section, label, "ok" if age < fine else "warn" if age < late else "bad", f"Last arrived {span(age)} ago")
            if self.app.dcs and self.app.dcs.present:
                brief = self.app.dcs.brief()
                age = now - (brief["last"] or 0)
                check("dcs", "DCS", "ok" if age < 120 else "warn" if age < 600 else "bad",
                      f"{brief['rate']:,} messages a minute, newest {span(age)} ago")
        except Exception:
            traceback.print_exc()
        if host.get("disk") is not None:
            used = host["disk"]
            check("disk", "Disk", "ok" if used < 85 else "warn" if used < 95 else "bad",
                  f"{used:.0f}% used, {host['disk_free'] / 2**30:,.0f} GB free of {host['disk_total'] / 2**30:,.0f} GB")
        if host.get("temp") is not None:
            temp = host["temp"]
            check("temp", "Temperature", "ok" if temp < 70 else "warn" if temp < 80 else "bad", f"{temp:.0f} °C")
        if host.get("cpu") is not None:
            busy = host.get("load", 0) / host["cores"]
            check("cpu", "Processor", "ok" if busy < 1.5 else "warn" if busy < 3 else "bad",
                  f"{host['cpu']:.0f}% busy, load {host.get('load', 0):.2f} on {host['cores']} cores")
        if host.get("mem") is not None:
            free = 100 - host["mem"]
            check("mem", "Memory", "ok" if free > 15 else "warn" if free > 7 else "bad",
                  f"{host['mem']:.0f}% used, {host['mem_free'] / 2**30:.1f} GB available of {host['mem_total'] / 2**30:.1f} GB")
        if host.get("battery") is not None:
            state, text = self.power(host["battery"], host.get("power") or "", now)
            check("power", "Power", state, text)
        return out

    def power(self, level, status, now):
        """Is the battery a worry? Only when it is really running down.

        A full battery on a machine that is plugged in reports "discharging" on and off as the
        charger tops it up, so that word alone means nothing. It counts as running down when the
        level has dropped two points in ten minutes, or has got below 90% while discharging.
        """
        status = status.lower()
        earlier = [s["battery"] for s in self.ring if s.get("battery") is not None and now - s["t"] <= 600]
        falling = bool(earlier) and earlier[0] - level >= 2
        if status != "discharging":
            note = "charging" if status == "charging" else "full" if status == "full" else status or "state unknown"
            if level < 20:
                return "warn", f"Battery low at {level}%, {note}"
            return "ok", f"Mains power, battery {level}% ({note})"
        if level < 20:
            return "bad", f"On battery and nearly empty: {level}% left"
        if falling:
            return "warn", f"On battery and falling: {level}%, down from {earlier[0]}% ten minutes ago"
        if level < 90:
            return "warn", f"On battery, {level}% left"
        return "ok", f"On battery, {level}% and holding"

    def brief(self):
        """For the header: the worst state and what it is about."""
        with self.lock:
            checks = self.checks()
        wrong = sorted((c for c in checks if ORDER[c["state"]] > 0), key=lambda c: -ORDER[c["state"]])
        return {"state": wrong[0]["state"] if wrong else "ok", "n": len(wrong),
                "text": f"{wrong[0]['label']}: {wrong[0]['text']}" if wrong else "Everything is working"}

    def status(self):
        with self.lock:
            now = time.time()
            keys = ("snr", "peak", "ber", "sync", "cpu", "sd_cpu", "gv_cpu", "load", "mem", "temp", "battery")
            ring = list(self.ring)
            me = self.host.get("goesview") or {}
            app = self.app
            goesview = {"up": now - self.started, "cpu": me.get("cpu"), "rss": me.get("rss"),
                        "python": sys.version.split()[0], "items": len(app.catalog.items), "threads": threading.active_count(),
                        "viewers": len(app.catalog.subscribers)}
            if app.dcs and app.dcs.present:
                goesview["dcs_backlog"] = app.dcs.pending
            return {"now": int(now), "checks": self.checks(), "host": self.host, "live": self.live,
                    "satdump": {**self.sd, "log": self.file_path}, "goesview": goesview,
                    "messages": [{"t": t, "level": level, "text": text, "n": n} for t, level, text, n in reversed(self.messages)],
                    "recent": {"step": STEP, "t": [s["t"] for s in ring], "series": {k: [s[k] for s in ring] for k in keys}}}

    def history(self, hours):
        """Minute rows over the last `hours`, thinned to a few hundred points."""
        now = int(time.time())
        db = self.connect()
        try:
            rows = db.execute("SELECT t, d FROM minutes WHERE t >= ? ORDER BY t", (now - int(hours * 3600),)).fetchall()
        finally:
            db.close()
        width = max(1, round(hours * 60 / 480)) * 60  # seconds per point
        buckets = {}
        for t, text in rows:
            try:
                buckets.setdefault(t // width, []).append(json.loads(text))
            except ValueError:
                pass
        first = (now - int(hours * 3600)) // width
        times, series = [], {}
        for index, bucket in enumerate(range(first, now // width + 1)):
            times.append(bucket * width)
            members = buckets.get(bucket, [])
            seen = {key for row in members for key in row}
            for key in seen:
                values = [row[key] for row in members if key in row]
                if key in SUMS:  # counts become a rate per minute
                    value = round(sum(values) / (width / 60), 2)
                elif key in ("snr_min",):
                    value = min(values)
                elif key in ("peak", "ber_max"):
                    value = max(values)
                else:
                    value = round(sum(values) / len(values), 4)
                series.setdefault(key, [None] * len(times))
                column = series[key]
                column.extend([None] * (index + 1 - len(column)))
                column[index] = value
        for column in series.values():
            column.extend([None] * (len(times) - len(column)))
        return {"step": width, "t": times, "series": series}
