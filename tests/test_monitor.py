"""Tests for monitor.py: the System page's readings, traffic lights and history.

SatDump log fixtures are written in code in SatDump's real format, one event per line:

    [04:58:00 - 20/11/2026] (I) Progress inf%, SNR : 4.997120dB, Peak SNR: 5.908409dB

Stamps are UTC and generated from time.time(), so anything judged against "now" is deterministic.
Nothing here starts the monitor's thread: its methods are called directly, and /proc and /sys are
replaced by fakes wherever a result depends on them.
"""

import collections
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402

GiB, MiB = 2 ** 30, 2 ** 20
SERIES = ("snr", "peak", "ber", "sync", "cpu", "sd_cpu", "gv_cpu", "load", "mem", "temp", "battery")
VERSION = "v1.2.3-3618db33"
RUN = "2026-11-20_04-57_goes_hrit_1.6941 GHz"
PDT = "Error loading PDT line for DCP processing: stoul"


# ------------------------------------------------------------------ SatDump's log

def stamp(t):
    return time.strftime("[%H:%M:%S - %d/%m/%Y]", time.gmtime(t))


def logline(t, level, text):
    return f"{stamp(t)} ({level}) {text}\n"


def snr_text(snr, peak):
    return f"Progress inf%, SNR : {snr:f}dB, Peak SNR: {peak:f}dB"


def viterbi_text(ber, viterbi="SYNCED", deframer="SYNCED"):
    return f"Progress inf%, Viterbi : {viterbi} BER : {ber:f}, Deframer : {deframer}"


def startup(t0):
    """What SatDump logs from starting to its first progress report, as (t, level, text)."""
    return [
        (t0, "I", f"Starting SatDump {VERSION}"),
        (t0, "E", "Usage : /usr/bin/satdump-ui [downlink] [input_level] [input_file] [output_file_or_directory] "
                  "[additional options as required]"),
        (t0, "E", "Extra options (examples. Any parameter used in modules can be used here) :"),
        (t0, "E", " --samplerate [baseband_samplerate] --baseband_format [f32/s16/s8/u8] --dc_block --iq_swap"),
        (t0 + 6, "D", "Set RTL-SDR samplerate to 1024000"),
        (t0 + 6, "D", "Set RTL-SDR frequency to 1694100000"),
        (t0 + 37, "I", f"Decoding to /some/path/{RUN}"),
        (t0 + 37, "D", "   - parse_dcs : true"),
        (t0 + 37, "D", "   - samplerate : 1024000"),
        (t0 + 37, "D", '   - baseband_format : "cs16"'),
        (t0 + 37, "D", "   - buffer_size : 8192"),
        (t0 + 37, "I", "Start processing..."),
        (t0 + 60, "I", snr_text(4.997120, 5.908409)),
        (t0 + 60, "I", viterbi_text(0.021973)),
        (t0 + 62, "I", "New LRIT file : pH-26324041703-A.lrit"),
        (t0 + 62, "I", "Writing file /some/path/DCS/pH-26324041703-A.dcs.json..."),
        (t0 + 63, "W", PDT),
    ]


def write_log(path, entries, mtime=None, mode="w"):
    """Write (t, level, text) entries; the file's modification time matches its last line unless given."""
    with open(path, mode) as f:
        for t, level, text in entries:
            f.write(logline(t, level, text))
    if mtime is None and entries:
        mtime = max(t for t, _, _ in entries)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def frozen(t):
    """Make the clock read t. monitor.py reads it as time.time(), through the time module."""
    return mock.patch.object(monitor.time, "time", return_value=t)


def quiet_tracebacks():
    return mock.patch.object(monitor.traceback, "print_exc")


# ------------------------------------------------------------------ the parts of GOESview the monitor touches

class FakeCatalog:
    def __init__(self, root):
        self.root = root
        self.items = []
        self.subscribers = set()
        self.last = {}      # section -> newest arrival time
        self.rate = {}      # section -> 60 per-minute counts, oldest first
        self.now = None     # what stats() reports as "now"; the clock when None
        self.fail = False
        self.calls = 0

    def stats(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("catalogue unavailable")
        return {"now": int(self.now if self.now is not None else time.time()), "rate": self.rate, "last": self.last}


class FakeDcs:
    def __init__(self, present=True, rate=0, last=None, minutes=None, pending=0):
        self.present, self.rate, self.last, self.pending = present, rate, last, pending
        self.minutes = minutes if minutes is not None else [0] * 60  # the 60 whole minutes before now
        self.now = None
        self.brief_calls = self.summary_calls = 0

    def brief(self):
        self.brief_calls += 1
        return {"rate": self.rate, "heard": 3, "last": self.last} if self.present else None

    def summary(self):
        self.summary_calls += 1
        return {"now": int(self.now if self.now is not None else time.time()), "rate": self.rate,
                "minutes": list(self.minutes), "last": self.last}


class FakeApp:
    def __init__(self, root, dcs=None):
        self.catalog = FakeCatalog(root)
        self.dcs = dcs


def ring_sample(t, **values):
    """One ten-second sample in the shape tick() records."""
    sample = {"t": int(t), **{key: None for key in SERIES}}
    sample.update(values)
    return sample


def host_reading(**changes):
    """A healthy machine, as sample_host() describes it."""
    host = {"up": 86400.0, "cpu": 20.0, "load": 0.8, "cores": 4,
            "mem_total": 8 * GiB, "mem_free": 4 * GiB, "mem": 50.0, "temp": 50.0,
            "disk_total": 200 * GiB, "disk_free": 100 * GiB, "disk": 50.0,
            "satdump": {"pid": 4242, "cpu": 60.0, "rss": 300 * MiB, "up": 7200.0},
            "goesview": {"pid": os.getpid(), "cpu": 2.0, "rss": 80 * MiB, "up": 600.0}}
    host.update(changes)
    return host


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="test_monitor_")
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "cache")
        self.logs = os.path.join(tmp.name, "satdump")
        self.root = os.path.join(tmp.name, "live_output")
        for folder in (self.cache, self.logs, self.root):
            os.mkdir(folder)

    def make(self, satdump_log=None, dcs=None):
        m = monitor.Monitor(self.cache, FakeApp(self.root, dcs), lambda *args, **kwargs: None, satdump_log)
        self.addCleanup(lambda: m.file and m.file.close())
        return m

    def watched(self, now, dcs=None, **host):
        """A monitor watching a SatDump that runs here and reported five seconds ago, on a healthy machine."""
        m = self.make(dcs=dcs)
        m.file_path = os.path.join(self.logs, "satdump.log")  # a log is being watched
        m.host = host_reading(**host)
        m.sd = {"params": {}, "version": VERSION, "pipeline": now - 600}
        m.live = {"snr": 5.0, "peak": 5.91, "ber": 0.022, "viterbi": True, "deframer": True, "t": now - 5}
        m.app.catalog.last = {"imagery": now - 300, "weather": now - 600, "text": now - 60}
        return m

    @staticmethod
    def lights(m, now):
        with frozen(now):
            return m.checks()

    def light(self, m, now, key):
        found = [c for c in self.lights(m, now) if c["id"] == key]
        self.assertLessEqual(len(found), 1)
        return found[0] if found else None

    @staticmethod
    def saved(m):
        db = sqlite3.connect(m.path)
        try:
            return {t: json.loads(d) for t, d in db.execute("SELECT t, d FROM minutes")}
        finally:
            db.close()

    @staticmethod
    def store(m, rows):
        db = sqlite3.connect(m.path)
        try:
            db.executemany("INSERT OR REPLACE INTO minutes(t, d) VALUES(?, ?)",
                           [(t, row if isinstance(row, str) else json.dumps(row)) for t, row in rows.items()])
            db.commit()
        finally:
            db.close()


# ------------------------------------------------------------------ small pieces

class SpanTest(unittest.TestCase):
    def test_wording_of_durations(self):
        """Durations in the lights read as seconds, minutes, hours or days at the documented cut-offs."""
        for seconds, text in ((-5, "0 s"), (0, "0 s"), (89, "89 s"), (240, "4 min"), (5399, "90 min"),
                              (5400, "1.5 h"), (7200, "2.0 h"), (172799, "48.0 h"), (172800, "2 days"),
                              (30 * 86400, "30 days")):
            with self.subTest(seconds=seconds):
                self.assertEqual(monitor.span(seconds), text)


class ReadHelperTest(Case):
    def test_read_returns_text_or_none(self):
        """read() gives a file's text, and None when it cannot be read instead of raising."""
        path = os.path.join(self.cache, "x")
        with open(path, "w") as f:
            f.write("42\n")
        self.assertEqual(monitor.read(path), "42\n")
        self.assertIsNone(monitor.read(os.path.join(self.cache, "missing")))
        self.assertIsNone(monitor.read(self.cache))  # a folder


class LineFormatTest(unittest.TestCase):
    def test_parses_satdump_line(self):
        """LINE_RE splits a SatDump line into its UTC stamp, level and text."""
        m = monitor.LINE_RE.match("[04:58:00 - 20/11/2026] (I) Progress inf%, SNR : 4.997120dB, Peak SNR: 5.908409dB\n")
        self.assertIsNotNone(m)
        self.assertEqual(m.groups()[:7], ("04", "58", "00", "20", "11", "2026", "I"))
        self.assertEqual(m.group(8), "Progress inf%, SNR : 4.997120dB, Peak SNR: 5.908409dB")

    def test_keeps_leading_spaces_of_the_text(self):
        """The indented parameter lines keep their indent, which is how they are recognised."""
        m = monitor.LINE_RE.match("[04:57:37 - 20/11/2026] (D)    - parse_dcs : true")
        self.assertEqual(m.group(7), "D")
        self.assertEqual(m.group(8), "   - parse_dcs : true")
        self.assertTrue(monitor.PARAM_RE.match(m.group(8)))

    def test_rejects_other_lines(self):
        """Lines without SatDump's stamp and level are not log events."""
        for text in ("", "Progress inf%, SNR : 4.9dB, Peak SNR: 5.9dB", "[04:58 - 20/11/2026] (I) short stamp",
                     "[04:58:00 - 20/11/26] (I) two-digit year", "[04:58:00 - 20/11/2026] I no brackets",
                     "  [04:58:00 - 20/11/2026] (I) indented"):
            with self.subTest(text=text):
                self.assertIsNone(monitor.LINE_RE.match(text))


# ------------------------------------------------------------------ what the log says

class TakeTest(Case):
    def setUp(self):
        super().setUp()
        self.m = self.make()
        self.t = (int(time.time()) // 60 - 5) * 60  # the start of a finished minute

    def take(self, *entries):
        for t, level, text in entries:
            self.m.take(t, level, text, False)

    def row(self, minute_start=None):
        self.m.flush(time.time(), backfill=True)
        return self.saved(self.m).get(self.t if minute_start is None else minute_start)

    def test_signal_and_error_rate_become_live(self):
        """A progress pair gives the live signal, peak, error rate and lock, with the line's time."""
        self.take((self.t + 1, "I", snr_text(4.997120, 5.908409)), (self.t + 1, "I", viterbi_text(0.021973)))
        self.assertEqual(self.m.live, {"snr": 5.0, "peak": 5.91, "ber": 0.022, "viterbi": True, "deframer": True,
                                       "t": self.t + 1})

    def test_negative_signal(self):
        """A signal below the noise (negative dB) is read with its sign."""
        self.take((self.t, "I", snr_text(-1.25, 0.5)))
        self.assertEqual((self.m.live["snr"], self.m.live["peak"]), (-1.25, 0.5))

    def test_lock_needs_viterbi_and_deframer(self):
        """Viterbi and deframer are reported separately; the minute counts as locked only when both are."""
        self.take((self.t, "I", viterbi_text(0.01, "SYNCED", "NOSYNC")))
        self.assertEqual((self.m.live["viterbi"], self.m.live["deframer"]), (True, False))
        self.take((self.t + 10, "I", viterbi_text(0.01, "NOSYNC", "SYNCED")))
        self.assertEqual((self.m.live["viterbi"], self.m.live["deframer"]), (False, True))
        self.take((self.t + 20, "I", viterbi_text(0.01)))
        self.assertEqual(self.row()["sync"], 33.3)

    def test_minute_signal_summary(self):
        """A minute's row has average and lowest signal, best peak, average and worst error rate, and share in lock."""
        self.take((self.t, "I", snr_text(4.0, 6.0)), (self.t, "I", viterbi_text(0.01)),
                  (self.t + 10, "I", snr_text(5.0, 7.0)), (self.t + 10, "I", viterbi_text(0.02)),
                  (self.t + 20, "I", snr_text(6.5, 6.5)), (self.t + 20, "I", viterbi_text(0.09, "NOSYNC", "NOSYNC")))
        row = self.row()
        self.assertEqual(row["snr"], 5.17)
        self.assertEqual(row["snr_min"], 4.0)
        self.assertEqual(row["peak"], 7.0)
        self.assertEqual(row["ber"], 0.04)
        self.assertEqual(row["ber_max"], 0.09)
        self.assertEqual(row["sync"], 66.7)

    def test_receiver_details(self):
        """Version, radio, frequency, sample rate, pipeline start, output folder and chosen options are kept."""
        self.take(*startup(self.t))
        sd = self.m.sd
        self.assertEqual(sd["version"], VERSION)
        self.assertEqual(sd["opened"], self.t)
        self.assertEqual(sd["device"], "RTL-SDR")
        self.assertEqual(sd["frequency"], 1694100000)
        self.assertEqual(sd["samplerate"], 1024000)
        self.assertEqual(sd["pipeline"], self.t + 37)
        self.assertEqual(sd["output"], RUN)
        self.assertEqual(sd["params"], {"parse_dcs": "true", "samplerate": "1024000", "baseband_format": "cs16"})

    def test_all_selected_parameters(self):
        """Each of the six options worth showing is kept, quotes stripped; others are not."""
        wanted = {"parse_dcs": "true", "samplerate": "2400000", "baseband_format": "cs16", "write_lrit": "false",
                  "fill_missing": "true", "freq_shift": "-1500"}
        for name, value in wanted.items():
            self.take((self.t, "D", f'   - {name} : "{value}"'))
        self.take((self.t, "D", "   - viterbi_outsync_after : 5"), (self.t, "D", "   - cpu_threads : 4"))
        self.assertEqual(self.m.sd["params"], wanted)

    def test_output_folder_is_the_run_not_a_raw_file(self):
        """The output shown is the run folder; decoding to a raw .cadu/.soft/.frm file does not replace it."""
        self.take((self.t, "I", f"Decoding to /some/path/{RUN}/"))
        self.assertEqual(self.m.sd["output"], RUN)
        for raw in ("/some/path/x.cadu", "/some/path/x.soft", "/some/path/x.frm"):
            self.take((self.t, "I", f"Decoding to {raw}"))
        self.assertEqual(self.m.sd["output"], RUN)

    def test_other_radio(self):
        """The radio's name is whatever SatDump names in its Set ... lines, spaces included."""
        self.take((self.t, "D", "Set Airspy Mini frequency to 1694100000"), (self.t, "D", "Set Airspy Mini samplerate to 3000000"))
        self.assertEqual((self.m.sd["device"], self.m.sd["frequency"], self.m.sd["samplerate"]),
                         ("Airspy Mini", 1694100000, 3000000))

    def test_counts_files_warnings_and_errors(self):
        """LRIT files received, files written (both wordings), warnings and errors (critical counts as one) are counted."""
        self.take((self.t, "I", "New LRIT file : pH-26324041703-A.lrit"),
                  (self.t + 1, "I", "New LRIT file : IMG_FD_047_IR_20261120.lrit"),
                  (self.t + 2, "I", "Writing file /some/path/DCS/pH-26324041703-A.dcs.json..."),
                  (self.t + 3, "I", "Saving /some/path/IMAGES/GOES-19/Full Disk/G19_13.png"),
                  (self.t + 4, "I", "Writing file /some/path/EMWIN/A_x.TXT..."),
                  (self.t + 5, "W", PDT),
                  (self.t + 6, "E", "Could not open the device"),
                  (self.t + 7, "C", "Pipeline crashed"),
                  (self.t + 8, "I", "Some other information"))
        row = self.row()
        self.assertEqual((row["lrit"], row["written"], row["warn"], row["err"]), (2, 3, 1, 2))

    def test_usage_printed_as_errors_is_ignored(self):
        """SatDump's usage text, printed as errors at every start, is neither counted nor listed."""
        self.take(*startup(self.t))
        self.m.flush(time.time(), backfill=True)
        self.assertFalse(any("err" in row for row in self.saved(self.m).values()))
        self.assertEqual([text for _, _, text, _ in self.m.messages], [PDT])

    def test_repeated_warning_collapses_with_a_count(self):
        """The same warning again and again is one entry with a count and the newest time; every one is still counted."""
        self.take(*[(self.t + i, "W", PDT) for i in range(5)])
        self.assertEqual(list(self.m.messages), [(self.t + 4, "W", PDT, 5)])
        self.assertEqual(self.row()["warn"], 5)

    def test_different_warning_in_between_starts_a_new_entry(self):
        """Only consecutive repeats collapse; a different message in between keeps them apart."""
        self.take((self.t, "W", PDT), (self.t + 1, "E", "Could not open the device"), (self.t + 2, "W", PDT))
        self.assertEqual([(text, n) for _, _, text, n in self.m.messages],
                         [(PDT, 1), ("Could not open the device", 1), (PDT, 1)])

    def test_same_warning_an_hour_later_is_a_new_entry(self):
        """A repeat more than an hour after the last one is listed again rather than folded in."""
        self.take((self.t - 3600, "W", PDT), (self.t - 1, "W", PDT), (self.t + 3599, "W", PDT))
        self.assertEqual([(t, n) for t, _, _, n in self.m.messages], [(self.t - 1, 2), (self.t + 3599, 1)])

    def test_keeps_the_newest_forty_messages(self):
        """Only the 40 newest warnings and errors are kept."""
        self.take(*[(self.t + i, "W", f"warning number {i}") for i in range(45)])
        texts = [text for _, _, text, _ in self.m.messages]
        self.assertEqual(len(texts), 40)
        self.assertEqual((texts[0], texts[-1]), ("warning number 5", "warning number 44"))

    def test_long_message_is_cut_to_300_characters(self):
        """A very long warning is kept as its first 300 characters."""
        text = "W" + "x" * 499
        self.take((self.t, "W", text))
        self.assertEqual(self.m.messages[-1][2], text[:300])

    # Regression: the repeat check once compared the full incoming text with the stored 300 characters,
    # so a long warning never matched itself and was listed again each time.
    def test_long_repeated_warning_also_collapses(self):
        """A repeated warning longer than 300 characters collapses like a short one."""
        text = "Could not write file /some/path/" + "very/deep/folder/" * 20 + "G19_13.png: No space left on device"
        self.assertGreater(len(text), 300)
        self.take((self.t, "W", text), (self.t + 1, "W", text), (self.t + 2, "W", text))
        self.assertEqual(len(self.m.messages), 1)
        self.assertEqual(self.m.messages[-1][3], 3)

    def test_warning_is_not_mistaken_for_receiver_details(self):
        """A warning that happens to mention a setting is a warning, not a receiver detail."""
        self.take((self.t, "W", "Set RTL-SDR frequency to 1694100000 failed"))
        self.assertNotIn("device", self.m.sd)
        self.assertEqual(self.m.messages[-1][1], "W")

    def test_progress_without_numbers_changes_nothing(self):
        """A progress line with neither signal nor Viterbi numbers leaves the live readings alone."""
        self.take((self.t, "I", "Progress 12.5%"))
        self.assertEqual(self.m.live, {})


class ReadLogTest(Case):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.t0 = (int(self.now) // 60 - 5) * 60  # five minutes ago, on a minute
        self.path = os.path.join(self.logs, "satdump.log")

    def test_reads_a_whole_log(self):
        """Pointed at a log file, the monitor takes in every event in it."""
        write_log(self.path, startup(self.t0))
        m = self.make(self.path)
        m.read_log()
        self.assertEqual(m.file_path, self.path)
        self.assertEqual(m.sd["version"], VERSION)
        self.assertEqual(m.sd["output"], RUN)
        self.assertEqual(m.live["t"], self.t0 + 60)
        self.assertEqual(m.live["snr"], 5.0)
        self.assertEqual([text for _, _, text, _ in m.messages], [PDT])
        m.flush(self.now, backfill=True)
        row = self.saved(m)[self.t0 + 60]
        self.assertEqual(row, {"snr": 5.0, "ber": 0.022, "sync": 100.0, "snr_min": 4.9971, "peak": 5.9084,
                               "ber_max": 0.022, "lrit": 1, "written": 1, "warn": 1})

    def test_folder_uses_the_newest_satdump_log(self):
        """Pointed at a folder, the monitor reads the newest satdump*.log and ignores other files."""
        older, newer = os.path.join(self.logs, "satdump_old.log"), os.path.join(self.logs, "satdump_new.log")
        write_log(older, [(self.t0, "I", "Starting SatDump v1.0.0")], mtime=self.now - 3600)
        write_log(newer, [(self.t0, "I", f"Starting SatDump {VERSION}")], mtime=self.now - 120)
        write_log(os.path.join(self.logs, "other.log"), [(self.t0, "I", "Starting SatDump v9")], mtime=self.now)
        write_log(os.path.join(self.logs, "satdump.ini"), [(self.t0, "I", "Starting SatDump v8")], mtime=self.now)
        m = self.make(self.logs)
        self.assertEqual(m.newest_log(), newer)
        m.read_log()
        self.assertEqual((m.file_path, m.sd["version"]), (newer, VERSION))

    def test_no_log_to_read(self):
        """With no log (none configured, an empty folder, a missing path) nothing is read and nothing breaks."""
        for where in (None, self.logs, os.path.join(self.logs, "does-not-exist")):
            with self.subTest(where=where):
                m = self.make(where)
                self.assertIsNone(m.newest_log())
                m.read_log()
                self.assertIsNone(m.file_path)
                self.assertEqual((m.sd, m.live), ({"params": {}}, {}))

    def test_log_that_appears_later_is_found_at_once(self):
        """When SatDump starts after GOESview, its first log is picked up on the next read, not 30 s later."""
        m = self.make(self.logs)
        m.read_log()
        self.assertIsNone(m.file_path)
        path = os.path.join(self.logs, "satdump.log")
        write_log(path, [(self.t0, "I", f"Starting SatDump {VERSION}")])
        m.read_log()
        self.assertEqual((m.file_path, m.sd.get("version")), (path, VERSION))

    def test_reads_only_what_is_new(self):
        """Each read takes in only the lines written since the last one."""
        write_log(self.path, [(self.t0, "I", "New LRIT file : a.lrit")])
        m = self.make(self.path)
        m.read_log()
        m.read_log()
        with open(self.path, "a") as f:
            f.write(logline(self.t0 + 1, "I", "New LRIT file : b.lrit"))
        m.read_log()
        m.read_log()
        m.flush(self.now, backfill=True)
        self.assertEqual(self.saved(m)[self.t0]["lrit"], 2)

    def test_line_still_being_written_waits(self):
        """A last line without its newline is not taken until it is complete, and then exactly once."""
        whole = logline(self.t0, "W", PDT)
        with open(self.path, "w") as f:
            f.write(logline(self.t0, "I", f"Starting SatDump {VERSION}") + whole[:40])
        m = self.make(self.path)
        m.read_log()
        self.assertEqual(m.sd["version"], VERSION)
        self.assertEqual(list(m.messages), [])
        m.read_log()
        self.assertEqual(list(m.messages), [])
        with open(self.path, "a") as f:
            f.write(whole[40:])
        m.read_log()
        m.read_log()
        self.assertEqual(list(m.messages), [(self.t0, "W", PDT, 1)])

    def test_skips_lines_that_are_not_events(self):
        """Blank lines and lines without a stamp are skipped and reading carries on."""
        with open(self.path, "w") as f:
            f.write("\n   continuation of something\n" + logline(self.t0, "I", f"Starting SatDump {VERSION}")
                    + "garbage [04:58:00 - 20/11/2026] (W) not at line start\n"
                    + logline(self.t0 + 1, "W", PDT))
        m = self.make(self.path)
        m.read_log()
        self.assertEqual(m.sd["version"], VERSION)
        self.assertEqual([text for _, _, text, _ in m.messages], [PDT])

    def test_stray_bytes_do_not_stop_reading(self):
        """Bytes that are not UTF-8 are replaced, and the lines after them are still read."""
        with open(self.path, "wb") as f:
            f.write(logline(self.t0, "W", "bad name ").encode()[:-1] + b"\xff\xfe\n")
            f.write(logline(self.t0 + 1, "I", f"Starting SatDump {VERSION}").encode())
        m = self.make(self.path)
        m.read_log()
        self.assertEqual(m.sd["version"], VERSION)
        self.assertIn("�", m.messages[0][2])
        with open(self.path, "a") as f:
            f.write(logline(self.t0 + 2, "W", PDT))
        m.read_log()
        self.assertEqual(m.messages[-1][2], PDT)

    def test_newer_log_is_picked_up_and_resets_the_receiver(self):
        """A newer log is taken up within 30 s, forgetting the old run's receiver details and signal."""
        first, second = os.path.join(self.logs, "satdump_1.log"), os.path.join(self.logs, "satdump_2.log")
        write_log(first, startup(self.t0), mtime=self.now - 100)
        m = self.make(self.logs)
        m.read_log()
        self.assertEqual((m.sd["version"], m.sd["device"], m.live["snr"]), (VERSION, "RTL-SDR", 5.0))
        write_log(second, [(self.t0 + 200, "I", "Starting SatDump v1.2.4-aaaaaaaa")], mtime=self.now - 10)
        m.read_log()  # looked for a newer log moments ago: still on the first
        self.assertEqual((m.file_path, m.sd["version"]), (first, VERSION))
        with frozen(time.time() + 31):
            m.read_log()
        self.assertEqual(m.file_path, second)
        self.assertEqual(m.sd, {"params": {}, "version": "v1.2.4-aaaaaaaa", "opened": self.t0 + 200})
        self.assertEqual(m.live, {})
        self.assertEqual(m.status()["satdump"]["log"], second)

    def test_very_large_log_is_read_from_its_last_40_mb(self):
        """Opening a log over 40 MB reads only its last 40 MB, starting at the first whole line there."""
        size_limit = 40 << 20
        tail = "".join(logline(t, level, text) for t, level, text in (
            (self.t0 + 60, "I", snr_text(7.5, 8.25)), (self.t0 + 60, "I", viterbi_text(0.0125)),
            (self.t0 + 61, "I", "New LRIT file : late.lrit")))
        head = "".join(logline(t, level, text) for t, level, text in (
            (self.t0, "I", "Starting SatDump v0.0.1-old"), (self.t0, "W", "from long ago"),
            (self.t0, "I", "New LRIT file : early.lrit")))
        cut = len(head) + MiB  # where the last 40 MB begin, inside a long line
        before_cut = logline(self.t0, "I", "a long line that starts before the cut ")[:-1]
        after_cut = logline(self.t0, "E", "only the end of a line")[:-1]  # looks like an event, but is not one
        with open(self.path, "wb") as f:  # sparse: the long line is mostly a hole of zero bytes
            f.write(head.encode())
            f.seek(cut - len(before_cut))
            f.write(before_cut.encode() + after_cut.encode())
            f.seek(cut + size_limit - len(tail) - 1)
            f.write(b"\n" + tail.encode())
        self.assertEqual(os.path.getsize(self.path) - size_limit, cut)
        os.utime(self.path, (self.t0 + 61, self.t0 + 61))
        m = self.make(self.path)
        m.read_log()
        self.assertNotIn("version", m.sd)
        self.assertEqual(list(m.messages), [])
        self.assertEqual((m.live["snr"], m.live["ber"]), (7.5, 0.0125))
        m.flush(self.now, backfill=True)
        rows = self.saved(m)
        self.assertNotIn(self.t0, rows)
        self.assertEqual(rows[self.t0 + 60]["lrit"], 1)

    def test_log_in_local_time_is_corrected(self):
        """A log stamped five hours behind UTC is read at the right time, so the pipeline is not called stopped."""
        real = int(self.now) - 5
        write_log(self.path, [(real - 18000 - 600, "I", "Start processing..."),
                              (real - 18000, "I", snr_text(5.0, 6.0)), (real - 18000, "I", viterbi_text(0.02))],
                  mtime=real)
        m = self.make(self.path)
        m.read_log()
        self.assertEqual(m.skew, 18000)
        self.assertEqual((m.live["t"], m.sd["pipeline"]), (real, real - 600))
        m.host = host_reading()
        pipeline = self.light(m, real + 5, "pipeline")
        self.assertEqual((pipeline["state"], pipeline["text"]), ("ok", "Reporting every 10 seconds, started 10 min ago"))


class ClockSkewTest(Case):
    def skew(self, last_stamp, mtime, extra=""):
        path = os.path.join(self.logs, "satdump.log")
        write_log(path, [(last_stamp - 30, "I", "Start processing..."), (last_stamp, "I", snr_text(5.0, 6.0))], mtime=mtime)
        if extra:
            with open(path, "a") as f:
                f.write(extra)
            os.utime(path, (mtime, mtime))
        return monitor.Monitor.clock_skew(path, os.path.getsize(path))

    def test_hours_behind(self):
        """A log stamped five hours behind its file time is corrected by five hours."""
        now = int(time.time())
        self.assertEqual(self.skew(now - 18000, now), 18000)

    def test_hours_ahead_by_a_half_hour_zone(self):
        """A log stamped 5 h 30 min ahead (a half-hour zone) is corrected back by exactly that."""
        now = int(time.time())
        self.assertEqual(self.skew(now + 19800, now), -19800)

    def test_rounds_to_the_nearest_quarter_hour(self):
        """The correction is the nearest quarter hour, not the raw difference."""
        now = int(time.time())
        self.assertEqual(self.skew(now - 18000 - 190, now), 18000)    # 5 h 3 min: written a little after the line
        self.assertEqual(self.skew(now - 18000 + 400, now), 18000)    # 4 h 53 min
        self.assertEqual(self.skew(now - 2700 - 500, now), 3600)      # 53 min: nearer an hour than 45 min

    def test_small_disagreement_is_left_alone(self):
        """Up to ten minutes between the last line and the file time is not treated as a clock difference."""
        now = int(time.time())
        for gap in (0, 5, 300, 600, -600):
            with self.subTest(gap=gap):
                self.assertEqual(self.skew(now - gap, now), 0)
        self.assertEqual(self.skew(now - 660, now), 900)  # eleven minutes is more than ten

    def test_uses_the_last_stamped_line(self):
        """The newest stamped line is compared, even when other text or a partial line follows it."""
        now = int(time.time())
        self.assertEqual(self.skew(now - 7200, now, extra="trailing text\n[12:00:0"), 7200)

    def test_nothing_to_compare(self):
        """A missing file or one without stamped lines gives no correction."""
        missing = os.path.join(self.logs, "missing.log")
        self.assertEqual(monitor.Monitor.clock_skew(missing, 0), 0)
        path = os.path.join(self.logs, "plain.log")
        with open(path, "w") as f:
            f.write("no stamps here\n")
        os.utime(path, (time.time() + 99999, time.time() + 99999))
        self.assertEqual(monitor.Monitor.clock_skew(path, os.path.getsize(path)), 0)


# ------------------------------------------------------------------ the traffic lights

class SatDumpLightTest(Case):
    def test_running(self):
        """SatDump running here is green, with how long it has run and its version."""
        now = time.time()
        m = self.watched(now)
        c = self.light(m, now, "satdump")
        self.assertEqual((c["label"], c["state"], c["text"]), ("SatDump", "ok", f"Running for 2.0 h, {VERSION}"))
        m.sd.pop("version")
        self.assertEqual(self.light(m, now, "satdump")["text"], "Running for 2.0 h")

    def test_not_running_while_its_log_is_watched(self):
        """SatDump's log is here but the program is not running: red, and the pipeline is not judged."""
        now = time.time()
        m = self.watched(now, satdump=None)
        c = self.light(m, now, "satdump")
        self.assertEqual((c["state"], c["text"]), ("bad", "Not running on this machine"))
        ids = [c["id"] for c in self.lights(m, now)]
        for key in ("pipeline", "lock", "snr", "ber"):
            self.assertNotIn(key, ids)

    def test_not_on_this_machine(self):
        """No SatDump and no log here: the light is off, not a fault, and nothing about the signal is shown."""
        now = time.time()
        m = self.make()
        m.host = host_reading(satdump=None)
        c = self.light(m, now, "satdump")
        self.assertEqual((c["state"], c["text"]),
                         ("off", "Not found on this machine, so its signal cannot be watched from here"))
        ids = [c["id"] for c in self.lights(m, now)]
        for key in ("pipeline", "lock", "snr", "ber"):
            self.assertNotIn(key, ids)

    def test_running_without_a_log(self):
        """SatDump running with no log to read is green, but the pipeline cannot be judged."""
        now = time.time()
        m = self.watched(now)
        m.file_path = None
        ids = [c["id"] for c in self.lights(m, now)]
        self.assertIn("satdump", ids)
        self.assertNotIn("pipeline", ids)
        self.assertEqual(self.light(m, now, "satdump")["state"], "ok")


class PipelineLightTest(Case):
    def test_open_without_a_pipeline(self):
        """SatDump open but no progress report yet: amber, and no lock light."""
        now = time.time()
        m = self.watched(now)
        m.live = {}
        c = self.light(m, now, "pipeline")
        self.assertEqual((c["label"], c["state"], c["text"]),
                         ("Pipeline", "warn", "SatDump is open but no pipeline has reported progress"))
        self.assertIsNone(self.light(m, now, "lock"))

    def test_reporting(self):
        """Progress within the last 45 s is green, with when the pipeline started if known."""
        now = time.time()
        m = self.watched(now)
        c = self.light(m, now, "pipeline")
        self.assertEqual((c["state"], c["text"]), ("ok", "Reporting every 10 seconds, started 10 min ago"))
        m.sd.pop("pipeline")
        self.assertEqual(self.light(m, now, "pipeline")["text"], "Reporting every 10 seconds")

    def test_hung(self):
        """Four minutes without progress is red with the README's words: stopped or hung."""
        now = time.time()
        m = self.watched(now)
        m.live["t"] = now - 240
        c = self.light(m, now, "pipeline")
        self.assertEqual(c["state"], "bad")
        self.assertEqual(c["text"], "No progress from SatDump for 4 min: the pipeline is stopped or SatDump has hung")

    def test_quiet_thresholds(self):
        """Quiet under 45 s is green, from 45 s to 2 min amber, over 2 min red."""
        now = 1_800_000_000.0
        m = self.watched(now)
        for quiet, state in ((0, "ok"), (44.9, "ok"), (45, "warn"), (60, "warn"), (120, "warn"), (120.1, "bad"),
                             (86400, "bad")):
            with self.subTest(quiet=quiet):
                m.live["t"] = now - quiet
                self.assertEqual(self.light(m, now, "pipeline")["state"], state)

    def test_quiet_wording(self):
        """The amber text says how long SatDump has been quiet."""
        now = 1_800_000_000.0
        m = self.watched(now)
        m.live["t"] = now - 60
        c = self.light(m, now, "pipeline")
        self.assertEqual((c["state"], c["text"]),
                         ("warn", "No progress from SatDump for 60 s: the pipeline is stopped or SatDump has hung"))

    def test_stale_lock_and_signal_are_hidden(self):
        """Lock, signal and error rate are shown while the last report is under two minutes old, then hidden."""
        now = 1_800_000_000.0
        m = self.watched(now)
        for quiet, shown in ((5, True), (119.9, True), (120, False), (600, False)):
            with self.subTest(quiet=quiet):
                m.live["t"] = now - quiet
                ids = [c["id"] for c in self.lights(m, now)]
                for key in ("lock", "snr", "ber"):
                    self.assertEqual(key in ids, shown, key)


class SignalLightTest(Case):
    def test_locked(self):
        """In lock: lock, signal and error rate are green, with their numbers."""
        now = time.time()
        m = self.watched(now)
        lights = {c["id"]: c for c in self.lights(m, now)}
        self.assertEqual((lights["lock"]["label"], lights["lock"]["state"], lights["lock"]["text"]),
                         ("Lock", "ok", "Viterbi and deframer in sync"))
        self.assertEqual((lights["snr"]["label"], lights["snr"]["state"], lights["snr"]["text"]),
                         ("Signal", "ok", "5.0 dB (peak 5.9 dB)"))
        self.assertEqual((lights["ber"]["label"], lights["ber"]["state"], lights["ber"]["text"]),
                         ("Error rate", "ok", "Viterbi BER 0.022"))

    def test_lost_lock(self):
        """Out of lock is red and says which half is out; the signal turns amber."""
        now = time.time()
        m = self.watched(now)
        for viterbi, deframer, text in (
                (True, False, "Not locked: Viterbi in sync, deframer NOT in sync"),
                (False, True, "Not locked: Viterbi NOT in sync, deframer in sync"),
                (False, False, "Not locked: Viterbi NOT in sync, deframer NOT in sync")):
            with self.subTest(viterbi=viterbi, deframer=deframer):
                m.live.update(viterbi=viterbi, deframer=deframer)
                lock = self.light(m, now, "lock")
                self.assertEqual((lock["state"], lock["text"]), ("bad", text))
                self.assertEqual(self.light(m, now, "snr")["state"], "warn")

    def test_signal_known_before_lock_is(self):
        """Signal reported but no Viterbi line yet: not locked, and no error-rate light."""
        now = time.time()
        m = self.watched(now)
        m.live = {"snr": 1.2, "peak": 2.0, "t": now - 5}
        self.assertEqual(self.light(m, now, "lock")["text"], "Not locked: Viterbi NOT in sync, deframer NOT in sync")
        self.assertEqual(self.light(m, now, "snr")["text"], "1.2 dB (peak 2.0 dB)")
        self.assertIsNone(self.light(m, now, "ber"))

    def test_error_rate_thresholds(self):
        """Error rate under 0.05 is green, under 0.1 amber, from 0.1 red."""
        now = time.time()
        m = self.watched(now)
        for ber, state in ((0.0, "ok"), (0.0499, "ok"), (0.05, "warn"), (0.0999, "warn"), (0.1, "bad"), (0.5, "bad")):
            with self.subTest(ber=ber):
                m.live["ber"] = ber
                self.assertEqual(self.light(m, now, "ber")["state"], state)
        m.live["ber"] = 0.1234
        self.assertEqual(self.light(m, now, "ber")["text"], "Viterbi BER 0.123")


class ArrivalLightTest(Case):
    def test_thresholds_per_kind(self):
        """Imagery and weather products are green under 30 min and red from 90 min; text bulletins at 5 and 15 min."""
        now = 1_800_000_000.0
        m = self.watched(now)
        for section, label, fine, late in (("imagery", "Imagery", 1800, 5400), ("weather", "Weather products", 1800, 5400),
                                           ("text", "Text bulletins", 300, 900)):
            for age, state in ((0, "ok"), (fine - 1, "ok"), (fine, "warn"), (late - 1, "warn"), (late, "bad")):
                with self.subTest(section=section, age=age):
                    m.app.catalog.last = {section: int(now - age)}
                    c = self.light(m, now, section)
                    self.assertEqual((c["label"], c["state"]), (label, state))

    def test_wording(self):
        """The text says how long ago the newest one arrived."""
        now = 1_800_000_000.0
        m = self.watched(now)
        m.app.catalog.last = {"imagery": int(now - 600), "text": int(now - 30), "weather": int(now - 7200)}
        lights = {c["id"]: c["text"] for c in self.lights(m, now)}
        self.assertEqual(lights["imagery"], "Last arrived 10 min ago")
        self.assertEqual(lights["text"], "Last arrived 30 s ago")
        self.assertEqual(lights["weather"], "Last arrived 2.0 h ago")

    def test_kinds_never_seen_have_no_light(self):
        """A kind of product that has never arrived gets no light at all."""
        now = time.time()
        m = self.watched(now)
        m.app.catalog.last = {"text": int(now - 30)}
        ids = [c["id"] for c in self.lights(m, now)]
        self.assertIn("text", ids)
        self.assertNotIn("imagery", ids)
        self.assertNotIn("weather", ids)

    def test_catalogue_failure_leaves_the_other_lights(self):
        """If the catalogue cannot answer, its lights are missing but the machine's are still shown."""
        now = time.time()
        m = self.watched(now, dcs=FakeDcs(rate=10, last=now - 5))
        m.app.catalog.fail = True
        with quiet_tracebacks() as printed:
            ids = [c["id"] for c in self.lights(m, now)]
        self.assertTrue(printed.called)
        for key in ("imagery", "weather", "text", "dcs"):
            self.assertNotIn(key, ids)
        for key in ("satdump", "pipeline", "disk", "temp", "cpu", "mem"):
            self.assertIn(key, ids)


class DcsLightTest(Case):
    def test_present(self):
        """With DCS on, its light gives the rate and how long ago the newest message came."""
        now = 1_800_000_000.0
        m = self.watched(now, dcs=FakeDcs(rate=1234, last=now - 30))
        c = self.light(m, now, "dcs")
        self.assertEqual((c["label"], c["state"], c["text"]), ("DCS", "ok", "1,234 messages a minute, newest 30 s ago"))

    def test_thresholds(self):
        """Newest DCS message under 2 min is green, under 10 min amber, older red."""
        now = 1_800_000_000.0
        dcs = FakeDcs(rate=5)
        m = self.watched(now, dcs=dcs)
        for age, state in ((0, "ok"), (119, "ok"), (120, "warn"), (599, "warn"), (600, "bad"), (86400, "bad")):
            with self.subTest(age=age):
                dcs.last = now - age
                self.assertEqual(self.light(m, now, "dcs")["state"], state)

    def test_off(self):
        """With DCS off, or on but with nothing yet, there is no DCS light."""
        now = time.time()
        self.assertIsNone(self.light(self.watched(now), now, "dcs"))
        dcs = FakeDcs(present=False, rate=5, last=now)
        self.assertIsNone(self.light(self.watched(now, dcs=dcs), now, "dcs"))
        self.assertEqual(dcs.brief_calls, 0)


class MachineLightTest(Case):
    def state(self, key, **host):
        now = time.time()
        m = self.watched(now, **host)
        return self.light(m, now, key)

    def test_disk(self):
        """Disk under 85% used is green, under 95% amber, from 95% red; text gives used and free space."""
        for used, state in ((0, "ok"), (84.9, "ok"), (85, "warn"), (94.9, "warn"), (95, "bad"), (100, "bad")):
            with self.subTest(used=used):
                self.assertEqual(self.state("disk", disk=used)["state"], state)
        c = self.state("disk", disk=62.4, disk_free=1500 * GiB, disk_total=4000 * GiB)
        self.assertEqual((c["label"], c["text"]), ("Disk", "62% used, 1,500 GB free of 4,000 GB"))

    def test_temperature(self):
        """Under 70 degrees is green, under 80 amber, from 80 red."""
        for temp, state in ((-5.0, "ok"), (69.9, "ok"), (70, "warn"), (79.9, "warn"), (80, "bad")):
            with self.subTest(temp=temp):
                self.assertEqual(self.state("temp", temp=temp)["state"], state)
        c = self.state("temp", temp=55.4)
        self.assertEqual((c["label"], c["text"]), ("Temperature", "55 °C"))

    def test_processor_judged_by_load_per_core(self):
        """The processor light follows load per core: under 1.5 green, under 3 amber, from 3 red, whatever the percentage."""
        for load, state in ((0, "ok"), (5.9, "ok"), (6.0, "warn"), (11.9, "warn"), (12.0, "bad")):
            with self.subTest(load=load):
                self.assertEqual(self.state("cpu", cpu=100.0, load=load, cores=4)["state"], state)
        self.assertEqual(self.state("cpu", cpu=99.0, load=1.0, cores=1)["state"], "ok")
        c = self.state("cpu", cpu=37.2, load=1.5, cores=4)
        self.assertEqual((c["label"], c["text"]), ("Processor", "37% busy, load 1.50 on 4 cores"))

    def test_processor_needs_a_first_reading(self):
        """Until a processor percentage is known (it takes two samples) there is no processor light."""
        now = time.time()
        m = self.watched(now)
        del m.host["cpu"]
        self.assertIsNone(self.light(m, now, "cpu"))

    def test_memory(self):
        """More than 15% free is green, more than 7% amber, otherwise red; text gives what is available."""
        for used, state in ((0, "ok"), (84.9, "ok"), (85, "warn"), (92.9, "warn"), (93, "bad"), (100, "bad")):
            with self.subTest(used=used):
                self.assertEqual(self.state("mem", mem=used)["state"], state)
        c = self.state("mem", mem=75.0, mem_free=2 * GiB, mem_total=8 * GiB)
        self.assertEqual((c["label"], c["text"]), ("Memory", "75% used, 2.0 GB available of 8.0 GB"))

    def test_readings_missing_means_no_light(self):
        """A machine without a disk, temperature, memory or battery reading shows no light for it."""
        now = time.time()
        m = self.make()
        m.host = {"cores": 2}
        self.assertEqual([c["id"] for c in self.lights(m, now)], ["satdump"])

    def test_power_light_follows_the_battery_rule(self):
        """The power light appears only with a battery and uses the battery rule."""
        self.assertIsNone(self.state("power"))
        c = self.state("power", battery=50, power="Discharging")
        self.assertEqual((c["label"], c["state"], c["text"]), ("Power", "warn", "On battery, 50% left"))
        c = self.state("power", battery=95, power=None)
        self.assertEqual((c["state"], c["text"]), ("ok", "Mains power, battery 95% (state unknown)"))

    def test_order_on_the_page(self):
        """The lights come in a fixed order: SatDump, its signal, the products, then the machine."""
        now = time.time()
        m = self.watched(now, dcs=FakeDcs(rate=3, last=now - 10), battery=100, power="Full")
        m.app.catalog.last = {"text": int(now - 30), "weather": int(now - 30), "imagery": int(now - 30)}
        self.assertEqual([c["id"] for c in self.lights(m, now)],
                         ["satdump", "pipeline", "lock", "snr", "ber", "imagery", "weather", "text", "dcs",
                          "disk", "temp", "cpu", "mem", "power"])
        for c in self.lights(m, now):
            self.assertEqual(set(c), {"id", "label", "state", "text"})
            self.assertEqual(c["state"], "ok", c)


class PowerTest(Case):
    def setUp(self):
        super().setUp()
        self.m = self.make()
        self.now = 1_800_000_000.0

    def history(self, *points):
        """Battery levels (seconds ago, level) into the last hour of samples, oldest first."""
        for ago, level in points:
            self.m.ring.append(ring_sample(self.now - ago, battery=level))

    def power(self, level, status):
        return self.m.power(level, status, self.now)

    def test_on_mains(self):
        """Charging, full, not charging or unknown is fine, and says which."""
        self.assertEqual(self.power(50, "Charging"), ("ok", "Mains power, battery 50% (charging)"))
        self.assertEqual(self.power(100, "Full"), ("ok", "Mains power, battery 100% (full)"))
        self.assertEqual(self.power(80, "Not charging"), ("ok", "Mains power, battery 80% (not charging)"))
        self.assertEqual(self.power(80, "Unknown"), ("ok", "Mains power, battery 80% (unknown)"))
        self.assertEqual(self.power(80, ""), ("ok", "Mains power, battery 80% (state unknown)"))

    def test_low_on_mains(self):
        """Under 20% is a warning even when charging, but not a fault."""
        self.assertEqual(self.power(19, "Charging"), ("warn", "Battery low at 19%, charging"))
        self.assertEqual(self.power(5, ""), ("warn", "Battery low at 5%, state unknown"))
        self.assertEqual(self.power(20, "Charging")[0], "ok")

    def test_charging_while_falling_is_fine(self):
        """A dropping level is not a worry while the battery says it is charging."""
        self.history((300, 60))
        self.assertEqual(self.power(55, "Charging")[0], "ok")

    def test_full_battery_topping_off(self):
        """A full battery that says "Discharging" while the charger tops it up is not a warning."""
        self.assertEqual(self.power(100, "Discharging"), ("ok", "On battery, 100% and holding"))
        self.history((590, 100), (400, 99), (200, 100), (10, 99))
        self.assertEqual(self.power(99, "Discharging"), ("ok", "On battery, 99% and holding"))
        self.assertEqual(self.power(99, "discharging")[0], "ok")

    def test_falling_two_points_in_ten_minutes(self):
        """Two points down within ten minutes while discharging is a warning, even when still high."""
        self.history((600, 97), (300, 96))
        self.assertEqual(self.power(95, "Discharging"), ("warn", "On battery and falling: 95%, down from 97% ten minutes ago"))

    def test_one_point_is_not_falling(self):
        """One point down in ten minutes is not called falling."""
        self.history((600, 97))
        self.assertEqual(self.power(96, "Discharging"), ("ok", "On battery, 96% and holding"))

    def test_only_the_last_ten_minutes_count(self):
        """A higher level from more than ten minutes ago is not compared."""
        self.history((601, 99), (300, 97))
        self.assertEqual(self.power(97, "Discharging"), ("ok", "On battery, 97% and holding"))

    def test_samples_without_a_battery_reading_are_skipped(self):
        """Samples that have no battery reading do not count as the earlier level."""
        self.m.ring.append(ring_sample(self.now - 500))
        self.history((400, 98))
        self.assertEqual(self.power(96, "Discharging")[0], "warn")
        self.assertIn("down from 98%", self.power(96, "Discharging")[1])

    def test_below_90_while_discharging(self):
        """Below 90% while discharging is a warning, holding steady or not."""
        self.assertEqual(self.power(89, "Discharging"), ("warn", "On battery, 89% left"))
        self.assertEqual(self.power(90, "Discharging"), ("ok", "On battery, 90% and holding"))

    def test_falling_is_reported_before_the_level(self):
        """When it is both under 90% and falling, the text says it is falling."""
        self.history((300, 88))
        self.assertEqual(self.power(85, "Discharging"), ("warn", "On battery and falling: 85%, down from 88% ten minutes ago"))

    def test_nearly_empty(self):
        """Discharging below 20% is a fault, whatever else."""
        self.history((300, 30))
        self.assertEqual(self.power(19, "Discharging"), ("bad", "On battery and nearly empty: 19% left"))
        self.assertEqual(self.power(0, "Discharging")[0], "bad")
        self.assertEqual(self.make().power(20, "Discharging", self.now), ("warn", "On battery, 20% left"))


# ------------------------------------------------------------------ summaries

class BriefTest(Case):
    def brief(self, m, now):
        with frozen(now):
            return m.brief()

    def test_everything_working(self):
        """All green gives the all-clear."""
        now = time.time()
        self.assertEqual(self.brief(self.watched(now), now), {"state": "ok", "n": 0, "text": "Everything is working"})

    def test_off_is_not_a_problem(self):
        """SatDump not being on this machine does not turn the header dot amber."""
        now = time.time()
        m = self.make()
        m.host = host_reading(satdump=None)
        self.assertEqual(self.brief(m, now), {"state": "ok", "n": 0, "text": "Everything is working"})

    def test_one_warning(self):
        """One amber light makes the header amber, with that light's label and text."""
        now = time.time()
        m = self.watched(now, disk=90.0)
        self.assertEqual(self.brief(m, now), {"state": "warn", "n": 1,
                                              "text": "Disk: 90% used, 100 GB free of 200 GB"})

    def test_worst_wins(self):
        """A red light wins over an amber one listed before it, and every light needing a look is counted."""
        now = time.time()
        m = self.watched(now, disk=90.0, temp=85.0, mem=88.0)
        self.assertEqual(self.brief(m, now), {"state": "bad", "n": 3, "text": "Temperature: 85 °C"})

    def test_first_of_the_worst(self):
        """Among equally bad lights, the first on the page is the one named."""
        now = time.time()
        m = self.watched(now, satdump=None, temp=85.0)
        self.assertEqual(self.brief(m, now), {"state": "bad", "n": 2, "text": "SatDump: Not running on this machine"})

    def test_hung_satdump_reaches_the_header(self):
        """A hung SatDump is what the header says."""
        now = time.time()
        m = self.watched(now)
        m.live["t"] = now - 240
        brief = self.brief(m, now)
        self.assertEqual(brief["state"], "bad")
        self.assertEqual(brief["text"],
                         "Pipeline: No progress from SatDump for 4 min: the pipeline is stopped or SatDump has hung")


class StatusTest(Case):
    def build(self, dcs=None):
        now = time.time()
        path = os.path.join(self.logs, "satdump.log")
        t0 = (int(now) // 60 - 3) * 60
        write_log(path, startup(t0) + [(t0 + 70, "E", "Could not open the device")])
        m = self.make(path, dcs=dcs)
        m.app.catalog.items = ["a", "b", "c"]
        m.app.catalog.subscribers = {"viewer-1", "viewer-2"}
        host = host_reading()
        m.sample_host = lambda: host
        for i in range(3):
            with frozen(now - 30 + 10 * i):
                m.tick()
        return m, now

    def test_shape(self):
        """status() has everything the System page reads, and it is plain JSON."""
        m, now = self.build()
        with frozen(now):
            status = m.status()
            checks = m.checks()
        self.assertTrue({"now", "checks", "host", "live", "satdump", "goesview", "messages", "recent"} <= set(status))
        self.assertEqual(status["now"], int(now))
        self.assertEqual(status["checks"], checks)
        self.assertEqual(status["host"], host_reading())
        self.assertEqual(status["live"], m.live)
        json.dumps(status)

    def test_receiver_and_log(self):
        """The receiver section has what the log said, plus which log it is."""
        m, _ = self.build()
        sd = m.status()["satdump"]
        self.assertEqual(sd["log"], os.path.join(self.logs, "satdump.log"))
        self.assertEqual((sd["version"], sd["device"], sd["frequency"], sd["samplerate"], sd["output"]),
                         (VERSION, "RTL-SDR", 1694100000, 1024000, RUN))
        self.assertEqual(sd["params"]["parse_dcs"], "true")

    def test_messages_newest_first(self):
        """Warnings and errors are listed newest first, each with its time, level, text and count."""
        m, _ = self.build()
        messages = m.status()["messages"]
        self.assertEqual([(x["level"], x["text"], x["n"]) for x in messages],
                         [("E", "Could not open the device", 1), ("W", PDT, 1)])
        self.assertEqual(set(messages[0]), {"t", "level", "text", "n"})

    def test_recent_series(self):
        """The last hour comes at ten-second steps: one time axis and every series the same length."""
        m, now = self.build()
        recent = m.status()["recent"]
        self.assertEqual(recent["step"], 10)
        self.assertEqual(recent["t"], [int(now - 30), int(now - 20), int(now - 10)])
        self.assertEqual(set(recent["series"]), set(SERIES))
        for key, values in recent["series"].items():
            self.assertEqual(len(values), 3, key)
        self.assertEqual(recent["series"]["cpu"], [20.0, 20.0, 20.0])
        self.assertEqual(recent["series"]["sd_cpu"], [60.0, 60.0, 60.0])

    def test_goesview_itself(self):
        """GOESview's own numbers: uptime, its process, Python, catalogue size, threads and viewers."""
        m, _ = self.build()
        gv = m.status()["goesview"]
        self.assertGreaterEqual(gv["up"], 0)
        self.assertEqual((gv["cpu"], gv["rss"]), (2.0, 80 * MiB))
        self.assertEqual(gv["python"], sys.version.split()[0])
        self.assertEqual((gv["items"], gv["viewers"]), (3, 2))
        self.assertGreaterEqual(gv["threads"], 1)
        self.assertNotIn("dcs_backlog", gv)

    def test_dcs_backlog(self):
        """With DCS on, the number of DCS files still to read is included."""
        m, _ = self.build(dcs=FakeDcs(rate=1, last=time.time(), pending=17))
        self.assertEqual(m.status()["goesview"]["dcs_backlog"], 17)

    def test_before_the_first_sample(self):
        """Straight after start, before any sample, status() still answers with empty series."""
        m = self.make()
        status = m.status()
        self.assertEqual(status["recent"], {"step": 10, "t": [], "series": {key: [] for key in SERIES}})
        self.assertEqual((status["host"], status["live"], status["messages"]), ({}, {}, []))
        self.assertIsNone(status["satdump"]["log"])
        self.assertEqual(status["goesview"]["cpu"], None)
        json.dumps(status)


# ------------------------------------------------------------------ the loop and history

class TickTest(Case):
    def setUp(self):
        super().setUp()
        self.m = self.make()
        self.host = host_reading(load=0.5, mem=40.0, temp=50.0, battery=99, cpu=12.5)
        self.m.sample_host = lambda: self.host
        self.T = (int(time.time()) // 60 - 10) * 60 + 5.5

    def tick(self, t):
        with frozen(t):
            self.m.tick()

    def test_records_a_sample(self):
        """Each tick records the machine and the fresh signal as one ten-second sample."""
        self.m.live = {"snr": 5.0, "peak": 6.0, "ber": 0.02, "viterbi": True, "deframer": True, "t": self.T - 5}
        self.tick(self.T)
        self.assertEqual(self.m.ring[-1], {"t": int(self.T), "snr": 5.0, "peak": 6.0, "ber": 0.02, "sync": 100,
                                           "cpu": 12.5, "load": 0.5, "mem": 40.0, "temp": 50.0, "battery": 99,
                                           "sd_cpu": 60.0, "gv_cpu": 2.0})
        self.assertIs(self.m.host, self.host)

    def test_out_of_lock_is_zero(self):
        """A fresh report out of lock is recorded as 0% in lock."""
        self.m.live = {"snr": 1.0, "peak": 2.0, "ber": 0.3, "viterbi": True, "deframer": False, "t": self.T - 5}
        self.tick(self.T)
        self.assertEqual(self.m.ring[-1]["sync"], 0)

    def test_stale_signal_is_not_recorded(self):
        """A signal report older than three steps is not carried forward as if it were current."""
        self.m.live = {"snr": 5.0, "peak": 6.0, "ber": 0.02, "viterbi": True, "deframer": True, "t": self.T - 29}
        self.tick(self.T)
        self.assertEqual(self.m.ring[-1]["snr"], 5.0)
        self.m.live["t"] = self.T - 31 + 10
        self.tick(self.T + 10)
        sample = self.m.ring[-1]
        self.assertEqual((sample["snr"], sample["peak"], sample["ber"], sample["sync"]), (None, None, None, None))
        self.assertEqual(sample["cpu"], 12.5)

    def test_machine_without_readings(self):
        """Missing machine readings are recorded as gaps, not zeros."""
        self.host = {"cores": 1, "satdump": None, "goesview": None}
        self.tick(self.T)
        self.assertEqual(self.m.ring[-1], ring_sample(self.T))

    def test_finished_minute_is_saved(self):
        """Once a minute is over, its averages are saved; the minute under way is not."""
        minute = int(self.T) // 60 * 60
        self.tick(self.T)
        self.host = dict(self.host, cpu=37.5, temp=52.0, disk=61.0)
        self.tick(self.T + 10)
        self.assertEqual(self.saved(self.m), {})
        self.tick(minute + 60)
        rows = self.saved(self.m)
        self.assertEqual(list(rows), [minute])
        self.assertEqual(rows[minute], {"cpu": 25.0, "load": 0.5, "mem": 40.0, "temp": 51.0, "battery": 99.0,
                                        "sd_cpu": 60.0, "gv_cpu": 2.0, "disk": 61.0})

    def test_keeps_one_hour_at_ten_seconds(self):
        """The in-memory history holds the last hour: 360 samples."""
        for i in range(365):
            self.tick(self.T + 10 * i)
        times = self.m.status()["recent"]["t"]
        self.assertEqual(len(times), 360)
        self.assertEqual((times[0], times[-1]), (int(self.T) + 50, int(self.T) + 3640))

    def test_prunes_rows_older_than_30_days(self):
        """Saved minutes older than 30 days are removed, at most every six hours."""
        old, kept = int(self.T) - 31 * 86400, int(self.T) - 29 * 86400
        self.store(self.m, {old: {"cpu": 1}, kept: {"cpu": 2}})
        self.tick(self.T)
        self.assertEqual(set(self.saved(self.m)), {kept})
        self.store(self.m, {old: {"cpu": 1}})
        self.tick(self.T + 3600)
        self.assertIn(old, self.saved(self.m))
        self.tick(self.T + 6 * 3600 + 10)
        self.assertNotIn(old, self.saved(self.m))

    def test_reads_the_log_each_tick(self):
        """Each tick takes in what SatDump has logged since the last one."""
        path = os.path.join(self.logs, "satdump.log")
        write_log(path, [(int(self.T) - 3, "I", snr_text(4.5, 5.5))], mtime=self.T)
        self.m.where = path
        self.tick(self.T)
        self.assertEqual(self.m.ring[-1]["snr"], 4.5)
        with open(path, "a") as f:
            f.write(logline(int(self.T) + 7, "I", snr_text(6.25, 7.0)))
        self.tick(self.T + 10)
        self.assertEqual(self.m.ring[-1]["snr"], 6.25)

    # Regression: a log line stamped hh:mm:59 that reached the file after the tick at hh:mm+1:00 was filed
    # under its own, already saved minute, and the next flush replaced that whole row with one holding
    # only the late line's numbers. A straggler now adds to the saved minute instead.
    def test_late_log_line_does_not_wipe_a_saved_minute(self):
        """A log line that is read after its minute was saved does not erase that minute's other readings."""
        path = os.path.join(self.logs, "satdump.log")
        minute = int(self.T) // 60 * 60
        write_log(path, [(minute + 10, "I", snr_text(5.0, 6.0)), (minute + 10, "I", viterbi_text(0.02)),
                         (minute + 20, "I", "New LRIT file : a.lrit")], mtime=minute + 20)
        self.m.where = path
        self.tick(minute + 30)
        self.tick(minute + 60)  # minute over: saved
        saved = self.saved(self.m)[minute]
        self.assertEqual((saved["cpu"], saved["sync"], saved["lrit"]), (12.5, 100.0, 1))
        with open(path, "a") as f:  # stamped in the saved minute, but written just after the tick
            f.write(logline(minute + 59, "I", snr_text(4.0, 4.5)))
        self.tick(minute + 70)
        row = self.saved(self.m)[minute]
        self.assertEqual(row.get("cpu"), 12.5)
        self.assertEqual(row.get("sync"), 100.0)
        self.assertEqual(row.get("lrit"), 1)


class FlushTest(Case):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.minute = int(self.now) // 60  # the minute under way

    def test_finished_minutes_saved_current_kept(self):
        """Every finished minute is saved and forgotten; the minute under way stays in memory."""
        m = self.make()
        m.minutes = {self.minute - 2: {"lrit": 1}, self.minute - 1: {"written": 2}, self.minute: {"warn": 1}}
        m.flush(self.now)
        self.assertEqual(self.saved(m), {(self.minute - 2) * 60: {"lrit": 1}, (self.minute - 1) * 60: {"written": 2}})
        self.assertEqual(list(m.minutes), [self.minute])
        m.flush(self.now)
        self.assertEqual(len(self.saved(m)), 2)

    def test_row_contents_and_rounding(self):
        """Averages are rounded (error rate 4 places, others 2); extremes and disk kept; zero counts left out."""
        m = self.make()
        m.minutes = {self.minute - 1: {"cpu_sum": 100.0, "cpu_n": 3, "load_sum": 1.0, "load_n": 3, "snr_sum": 15.5,
                                       "snr_n": 3, "snr_min": 4.123456, "peak": 7.000049, "ber_sum": 0.1, "ber_n": 3,
                                       "sync_n": 2, "ber_max": 0.0912345, "disk": 61.234567,
                                       "lrit": 4, "written": 0, "warn": 0, "err": 2}}
        m.flush(self.now)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60],
                         {"cpu": 33.33, "load": 0.33, "snr": 5.17, "ber": 0.0333, "sync": 66.7, "snr_min": 4.1235,
                          "peak": 7.0, "ber_max": 0.0912, "disk": 61.2346, "lrit": 4, "err": 2})

    def test_empty_minute_is_not_saved(self):
        """A minute with nothing in it leaves no row (and so shows as a gap)."""
        m = self.make()
        m.minutes = {self.minute - 1: {}, self.minute - 2: {"lrit": 0, "warn": 0}}
        m.flush(self.now)
        self.assertEqual(self.saved(m), {})
        self.assertEqual(m.minutes, {})

    def test_backfill_never_replaces_a_live_minute(self):
        """A minute filled in from the log at start does not replace one that was sampled live."""
        m = self.make()
        live, gap = (self.minute - 3) * 60, (self.minute - 2) * 60
        m.minutes = {self.minute - 3: {"cpu_sum": 50.0, "cpu_n": 1, "snr_sum": 5.0, "snr_n": 1}}
        m.flush(self.now)
        later = self.make()  # GOESview restarted: it fills in the signal from SatDump's log
        later.minutes = {self.minute - 3: {"snr_sum": 1.0, "snr_n": 1}, self.minute - 2: {"snr_sum": 2.0, "snr_n": 1}}
        later.flush(self.now, backfill=True)
        rows = self.saved(later)
        self.assertEqual(rows[live], {"cpu": 50.0, "snr": 5.0})
        self.assertEqual(rows[gap], {"snr": 2.0})

    def test_backfill_does_not_ask_for_arrivals(self):
        """Filling in from the log does not take GOESview's arrival counts, which belong to the live minutes."""
        dcs = FakeDcs(minutes=[9] * 60)
        m = self.make(dcs=dcs)
        m.app.catalog.rate = {"imagery": [5] * 60}
        m.minutes = {self.minute - 1: {"lrit": 1}}
        m.flush(self.now, backfill=True)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60], {"lrit": 1})
        self.assertEqual((m.app.catalog.calls, dcs.summary_calls), (0, 0))

    def test_arrivals_are_saved_with_the_minute(self):
        """Arrivals in GOESview per kind are saved in the minute rows, zero included."""
        m = self.make()
        m.app.catalog.rate = {"imagery": [2] * 60, "text": [0] * 60}
        m.minutes = {self.minute - 2: {"lrit": 1}, self.minute - 1: {"lrit": 1}}
        m.flush(self.now)
        rows = self.saved(m)
        for minute in (self.minute - 2, self.minute - 1):
            self.assertEqual(rows[minute * 60], {"lrit": 1, "imagery": 2, "text": 0})

    # Regression: Catalog.stats() counts arrivals by age, so the last of its 60 counts is the 60 seconds
    # before stats()["now"]. That count was once filed under the minute still under way, putting every
    # arrival one minute late. It belongs to the minute just finished, as the DCS series already assumed.
    def test_arrival_is_saved_under_the_minute_it_arrived(self):
        """An image that arrived halfway through a minute is counted in that minute's row."""
        m = self.make()
        done = self.minute - 3
        now = (done + 1) * 60 + 5             # the first tick after that minute ended
        recv = done * 60 + 30                 # arrived halfway through it
        counts = [0] * 60
        counts[59 - int((now - recv) // 60)] += 1  # Catalog.stats(): index 59 - age in whole minutes
        m.app.catalog.rate, m.app.catalog.now = {"imagery": counts}, now
        m.minutes = {done: {"lrit": 1}, done + 1: {}}
        m.flush(now)
        self.assertEqual(self.saved(m)[done * 60].get("imagery"), 1)

    def test_dcs_messages_saved_with_their_minute(self):
        """DCS messages per minute are saved in the minute they were heard."""
        dcs = FakeDcs(minutes=[0] * 57 + [3, 4, 7])
        m = self.make(dcs=dcs)
        dcs.now = int(self.now)
        m.minutes = {self.minute - 3: {"lrit": 1}, self.minute - 2: {"lrit": 1}, self.minute - 1: {"lrit": 1}}
        m.flush(self.now)
        rows = self.saved(m)
        self.assertEqual([rows[(self.minute - k) * 60]["dcs"] for k in (3, 2, 1)], [3, 4, 7])

    def test_dcs_count_is_completed_at_the_next_save(self):
        """DCS files arrive some seconds late, so a minute saved the moment it ends is short; it is topped up."""
        dcs = FakeDcs(minutes=[0] * 59 + [500])          # the minute just ended: 500 read in so far
        m = self.make(dcs=dcs)
        dcs.now = int(self.now)
        m.minutes = {self.minute - 1: {"lrit": 1}}
        m.flush(self.now)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60], {"lrit": 1, "dcs": 500})
        dcs.minutes, dcs.now = [0] * 58 + [760, 510], int(self.now) + 60   # a minute on: it has all come in
        m.minutes = {self.minute: {"lrit": 2}}
        m.flush(self.now + 60)
        rows = self.saved(m)
        self.assertEqual(rows[(self.minute - 1) * 60], {"lrit": 1, "dcs": 760})
        self.assertEqual(rows[self.minute * 60], {"lrit": 2, "dcs": 510})

    def test_a_stale_dcs_summary_is_made_good_at_the_next_save(self):
        """A summary taken just before the minute ended has nothing for that minute yet: filled in a minute later."""
        dcs = FakeDcs(minutes=[7] * 60)
        m = self.make(dcs=dcs)
        dcs.now = int(self.now) - int(self.now) % 60 - 2    # two seconds before the minute turned
        m.minutes = {self.minute - 1: {"lrit": 1}}
        m.flush(self.now)
        self.assertNotIn("dcs", self.saved(m)[(self.minute - 1) * 60])
        dcs.minutes, dcs.now = [7] * 58 + [740, 300], int(self.now) + 60
        m.minutes = {self.minute: {"lrit": 1}}
        m.flush(self.now + 60)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60]["dcs"], 740)

    def test_dcs_off_adds_nothing(self):
        """With DCS on but empty, nothing about DCS is saved."""
        dcs = FakeDcs(present=False, minutes=[5] * 60)
        m = self.make(dcs=dcs)
        m.minutes = {self.minute - 1: {"lrit": 1}}
        m.flush(self.now)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60], {"lrit": 1})
        self.assertEqual(dcs.summary_calls, 0)

    def test_catalogue_failure_still_saves(self):
        """If GOESview's own counts cannot be had, the minute is still saved without them."""
        m = self.make()
        m.app.catalog.fail = True
        m.minutes = {self.minute - 1: {"cpu_sum": 10.0, "cpu_n": 1}}
        with quiet_tracebacks():
            m.flush(self.now)
        self.assertEqual(self.saved(m)[(self.minute - 1) * 60], {"cpu": 10.0})

    def test_history_survives_a_restart(self):
        """Saved minutes are in the cache folder's database, so a new monitor sees them."""
        m = self.make()
        m.minutes = {self.minute - 1: {"cpu_sum": 10.0, "cpu_n": 1}}
        m.flush(self.now)
        self.assertTrue(os.path.isfile(os.path.join(self.cache, "monitor-1.sqlite3")))
        again = self.make()
        index = again.history(1)["t"].index((self.minute - 1) * 60)
        self.assertEqual(again.history(1)["series"]["cpu"][index], 10.0)


class HistoryTest(Case):
    def setUp(self):
        super().setUp()
        self.m = self.make()

    def test_time_axis_covers_the_span(self):
        """For each range the page offers, the axis spans start to now in even steps, at most 481 points."""
        for hours, step in ((0.25, 60), (1, 60), (3, 60), (6, 60), (24, 180), (168, 1260), (720, 5400)):
            with self.subTest(hours=hours):
                now = time.time()
                h = self.m.history(hours)
                later = time.time()
                self.assertEqual(h["step"], step)
                times = h["t"]
                self.assertLessEqual(len(times), 481)
                self.assertTrue(all(b - a == step for a, b in zip(times, times[1:])))
                self.assertLessEqual(times[0], later - hours * 3600)        # starts at or before the span
                self.assertGreater(times[0] + step, now - hours * 3600 - 1)  # but not a whole step earlier
                self.assertLessEqual(times[-1], later)                       # ends with the bucket holding now
                self.assertGreater(times[-1] + step, now - 1)
                self.assertEqual(h["series"], {})

    def test_one_minute_steps(self):
        """Over an hour, each minute is its own point and counts stay as they were."""
        now = int(time.time())
        at = (now // 60 - 10) * 60
        self.store(self.m, {at: {"snr": 5.25, "lrit": 4, "cpu": 12.0}})
        h = self.m.history(1)
        self.assertEqual(len(h["t"]), 61)
        i = h["t"].index(at)
        self.assertEqual((h["series"]["snr"][i], h["series"]["lrit"][i], h["series"]["cpu"][i]), (5.25, 4.0, 12.0))

    def test_minutes_combined_into_wider_buckets(self):
        """In wider buckets, readings are averaged, lowest and highest keep their extremes, and counts become a rate per minute."""
        width = 180  # 24 hours
        bucket = int(time.time()) // width - 10
        start = bucket * width
        self.store(self.m, {
            start: {"snr": 4.0, "snr_min": 3.0, "peak": 6.0, "ber": 0.01, "ber_max": 0.05, "sync": 100.0, "cpu": 10.0,
                    "lrit": 3, "written": 2, "disk": 50.0},
            start + 60: {"snr": 5.0, "snr_min": 2.0, "peak": 7.5, "ber": 0.03, "ber_max": 0.2, "sync": 50.0, "cpu": 20.0,
                         "lrit": 6, "warn": 3, "dcs": 30, "disk": 50.2},
            start + 120: {"snr": 6.0, "snr_min": 4.0, "peak": 5.0, "ber": 0.02, "ber_max": 0.1, "cpu": 30.0,
                          "imagery": 9, "text": 1, "weather": 2, "err": 1}})
        h = self.m.history(24)
        self.assertEqual(h["step"], width)
        i = h["t"].index(start)
        got = {key: values[i] for key, values in h["series"].items()}
        self.assertEqual(got, {"snr": 5.0, "snr_min": 2.0, "peak": 7.5, "ber": 0.02, "ber_max": 0.2,
                               "sync": 75.0, "cpu": 20.0, "disk": 50.1,
                               "lrit": 3.0, "written": 0.67, "warn": 1.0, "dcs": 10.0, "imagery": 3.0,
                               "text": 0.33, "weather": 0.67, "err": 0.33})

    def test_gaps_are_none(self):
        """Buckets without data, and readings missing from a bucket, come back as None, with every series as long as the axis."""
        width = 180
        b = int(time.time()) // width - 20
        self.store(self.m, {b * width: {"snr": 4.0, "cpu": 10.0}, (b + 3) * width: {"snr": 6.0},
                            (b + 5) * width + 60: {"temp": 40.0}})
        h = self.m.history(24)
        times = h["t"]
        i = times.index(b * width)
        for key, values in h["series"].items():
            self.assertEqual(len(values), len(times), key)
        snr, cpu, temp = h["series"]["snr"], h["series"]["cpu"], h["series"]["temp"]
        self.assertEqual(snr[i:i + 7], [4.0, None, None, 6.0, None, None, None])
        self.assertEqual(cpu[i:i + 7], [10.0, None, None, None, None, None, None])
        self.assertEqual(temp[i:i + 7], [None, None, None, None, None, 40.0, None])
        self.assertEqual(sum(v is not None for v in snr), 2)
        self.assertEqual(sum(v is not None for v in cpu), 1)
        self.assertEqual(sum(v is not None for v in temp), 1)

    def test_only_the_requested_span(self):
        """Rows older than the span asked for are left out."""
        now = int(time.time())
        self.store(self.m, {(now // 60 - 25 * 60) * 60: {"temp": 99.0}, (now // 60 - 30) * 60: {"cpu": 5.0}})
        h = self.m.history(1)
        self.assertEqual(set(h["series"]), {"cpu"})
        self.assertIn("temp", self.m.history(26)["series"])

    def test_unreadable_row_is_skipped(self):
        """A damaged row is skipped and the rest still comes back."""
        now = int(time.time())
        self.store(self.m, {(now // 60 - 5) * 60: "{not json", (now // 60 - 4) * 60: {"cpu": 7.0}})
        h = self.m.history(1)
        self.assertEqual(set(h["series"]), {"cpu"})
        self.assertEqual(h["series"]["cpu"][h["t"].index((now // 60 - 4) * 60)], 7.0)

    def test_from_the_log_to_the_chart(self):
        """Signal read from SatDump's log ends up in the history at its minute."""
        now = time.time()
        t0 = (int(now) // 60 - 5) * 60
        path = os.path.join(self.logs, "satdump.log")
        write_log(path, startup(t0))
        m = self.make(path)
        m.read_log(catching_up=True)
        m.flush(now, backfill=True)
        h = m.history(1)
        i = h["t"].index(t0 + 60)
        series = h["series"]
        self.assertEqual((series["snr"][i], series["peak"][i], series["ber"][i], series["sync"][i],
                          series["lrit"][i], series["written"][i], series["warn"][i]),
                         (5.0, 5.9084, 0.022, 100.0, 1.0, 1.0, 1.0))
        self.assertNotIn("err", series)


# ------------------------------------------------------------------ the machine

class HostTest(Case):
    FILES = {
        "/proc/uptime": "3600.50 7000.00\n",
        "/proc/stat": "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\nintr 12345\n",
        "/proc/loadavg": "1.50 1.20 0.90 2/345 6789\n",
        "/proc/meminfo": "MemTotal:        8000000 kB\nMemFree:          500000 kB\nMemAvailable:    2000000 kB\n"
                         "HugePages_Total:       0\n",
        "/sys/class/thermal/thermal_zone0/temp": "45000\n",
        "/sys/class/thermal/thermal_zone1/temp": "52500\n",
        "/sys/class/thermal/thermal_zone2/temp": "N/A\n",
        "/sys/class/thermal/thermal_zone3/temp": "-5000\n",
        "/sys/class/power_supply/AC/online": "1\n",
        "/sys/class/power_supply/BAT0/capacity": "87\n",
        "/sys/class/power_supply/BAT0/status": "Discharging\n",
    }
    GLOBS = {
        "/sys/class/thermal/thermal_zone*/temp": [f"/sys/class/thermal/thermal_zone{i}/temp" for i in range(4)],
        "/sys/class/power_supply/*": ["/sys/class/power_supply/AC", "/sys/class/power_supply/BAT0"],
    }
    Usage = collections.namedtuple("Usage", "total used free")

    def sample(self, m, files=None, globs=None, disk=None):
        files = dict(self.FILES if files is None else files)
        globs = self.GLOBS if globs is None else globs
        disk = disk if disk is not None else mock.DEFAULT
        with mock.patch.object(monitor, "read", side_effect=files.get), \
                mock.patch.object(monitor.glob, "glob", side_effect=lambda pattern: list(globs.get(pattern, []))), \
                mock.patch.object(monitor.shutil, "disk_usage", return_value=self.Usage(1000, 750, 250)) as usage:
            if disk is not mock.DEFAULT:
                usage.side_effect = disk
            host = m.sample_host()
        return host, usage

    def monitor_with_processes(self):
        m = self.make()
        self.uptimes = []
        m.satdump.sample = lambda uptime: self.uptimes.append(uptime) or {"pid": 4242, "cpu": 50.0, "rss": 1, "up": 60.0}
        m.me.sample = lambda uptime: {"pid": os.getpid(), "cpu": 1.0, "rss": 2, "up": 30.0}
        return m

    def test_reads_the_machine(self):
        """Uptime, load, memory, hottest sensor, battery, disk and the two processes are read from /proc and /sys."""
        m = self.monitor_with_processes()
        host, usage = self.sample(m)
        usage.assert_called_once_with(self.root)
        self.assertEqual(host, {"up": 3600.5, "load": 1.5, "cores": os.cpu_count() or 1,
                                "mem_total": 8000000 * 1024, "mem_free": 2000000 * 1024, "mem": 75.0,
                                "temp": 52.5, "battery": 87, "power": "Discharging",
                                "disk_total": 1000, "disk_free": 250, "disk": 75.0,
                                "satdump": {"pid": 4242, "cpu": 50.0, "rss": 1, "up": 60.0},
                                "goesview": {"pid": os.getpid(), "cpu": 1.0, "rss": 2, "up": 30.0}})
        self.assertEqual(self.uptimes, [3600.5])

    def test_processor_busy_from_two_samples(self):
        """The processor percentage comes from the change between two samples, idle and I/O wait counting as idle."""
        m = self.monitor_with_processes()
        first, _ = self.sample(m)
        self.assertNotIn("cpu", first)
        # 800 ticks later: 200 busy, 500 idle and 100 waiting on I/O, which counts as idle too
        files = dict(self.FILES, **{"/proc/stat": "cpu  200 0 200 1200 200 0 0 0 0 0\n"})
        second, _ = self.sample(m, files)
        self.assertEqual(second["cpu"], 25.0)
        third, _ = self.sample(m, files)  # nothing moved: no new figure rather than a division by zero
        self.assertNotIn("cpu", third)

    def test_bare_machine(self):
        """Without thermal zones, a battery or readable /proc files, those readings are simply absent."""
        m = self.monitor_with_processes()
        host, _ = self.sample(m, files={}, globs={})
        self.assertEqual(set(host), {"cores", "disk_total", "disk_free", "disk", "satdump", "goesview"})
        self.assertEqual(self.uptimes, [None])

    def test_battery_without_status(self):
        """A battery that reports its level but not its state gives no power state."""
        m = self.monitor_with_processes()
        files = {k: v for k, v in self.FILES.items() if not k.endswith("/status")}
        host, _ = self.sample(m, files)
        self.assertEqual((host["battery"], host["power"]), (87, None))

    def test_meminfo_without_available(self):
        """Old kernels without MemAvailable give no memory reading rather than a wrong one."""
        m = self.monitor_with_processes()
        files = dict(self.FILES, **{"/proc/meminfo": "MemTotal:  8000000 kB\nMemFree:  500000 kB\n"})
        host, _ = self.sample(m, files)
        for key in ("mem", "mem_total", "mem_free"):
            self.assertNotIn(key, host)

    def test_disk_unreadable(self):
        """If the output folder's disk cannot be measured, the disk reading is left out."""
        m = self.monitor_with_processes()
        host, _ = self.sample(m, disk=OSError("gone"))
        for key in ("disk", "disk_total", "disk_free"):
            self.assertNotIn(key, host)
        self.assertEqual(host["battery"], 87)

    def test_this_machine(self):
        """On the real machine, sampling works and gives sane numbers (whatever this machine has)."""
        m = self.make()
        host = m.sample_host()
        self.assertGreaterEqual(host["cores"], 1)
        self.assertTrue(0 <= host["disk"] <= 100)
        for key in ("mem", "cpu"):
            if key in host:
                self.assertTrue(0 <= host[key] <= 100, key)
        if host["goesview"] is not None:
            self.assertEqual(host["goesview"]["pid"], os.getpid())
            self.assertGreater(host["goesview"]["rss"], 0)
        json.dumps(host)


class ProcessTest(unittest.TestCase):
    @staticmethod
    def stat(pid, comm, utime, stime, start):
        return (f"{pid} ({comm}) S 1 {pid} {pid} 0 -1 4194560 1200 0 3 0 {utime} {stime} 0 0 20 0 9 0 {start} "
                "912345088 4567 18446744073709551615 1 1 0 0 0 0 0 4096 0 0 0 0 17 2 0 0 0 0 0 0 0 0 0 0 0 0 0\n")

    def setUp(self):
        self.files = {}
        self.clock = [1000.0]
        for patch in (mock.patch.object(monitor, "read", side_effect=lambda path: self.files.get(path)),
                      mock.patch.object(monitor.time, "time", side_effect=lambda: self.clock[0]),
                      mock.patch.object(monitor.Process, "TICK", 100),
                      mock.patch.object(monitor.Process, "PAGE", 4096)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_command_name_with_spaces_and_parentheses(self):
        """A command name with spaces and parentheses does not throw off the fields after it."""
        self.files["/proc/4242/stat"] = self.stat(4242, "sat dump (ui) )x(", 250, 50, 1000)
        self.files["/proc/4242/statm"] = "50000 1200 300 10 0 2000 0\n"
        sample = monitor.Process(pid=4242).sample(3600.0)
        self.assertEqual(sample, {"pid": 4242, "cpu": None, "rss": 1200 * 4096, "up": 3590.0})

    def test_processor_share_from_two_samples(self):
        """The share of a core is the processor time used between two samples over the time between them."""
        p = monitor.Process(pid=4242)
        self.files["/proc/4242/stat"] = self.stat(4242, "goesview", 250, 50, 1000)
        self.assertIsNone(p.sample(3600.0)["cpu"])
        self.clock[0] += 10
        self.files["/proc/4242/stat"] = self.stat(4242, "goesview", 600, 200, 1000)
        self.assertEqual(p.sample(3610.0)["cpu"], 50.0)
        self.files["/proc/4242/stat"] = self.stat(4242, "goesview", 600, 200, 1000)
        self.assertIsNone(p.sample(3610.0)["cpu"])  # no time has passed: no figure

    def test_missing_process(self):
        """A process that is not there gives None."""
        self.assertIsNone(monitor.Process(pid=777).sample(100.0))
        with mock.patch.object(monitor.os, "listdir", return_value=["1", "self", "cpuinfo"]):
            self.files["/proc/1/comm"] = "systemd\n"
            self.assertIsNone(monitor.Process(("satdump-ui", "satdump")).sample(100.0))

    def test_unknown_uptime(self):
        """Without the machine's uptime, the process's own uptime is unknown rather than wrong."""
        self.files["/proc/4242/stat"] = self.stat(4242, "goesview", 1, 1, 1000)
        self.assertIsNone(monitor.Process(pid=4242).sample(None)["up"])
        self.assertEqual(monitor.Process(pid=4242).sample(5.0)["up"], 0)

    def test_found_by_name_and_followed_across_a_restart(self):
        """SatDump is found by name, and refound after a restart with its processor share started afresh."""
        self.files.update({"/proc/1/comm": "systemd\n", "/proc/4242/comm": "satdump-ui\n",
                           "/proc/4242/stat": self.stat(4242, "satdump-ui", 9000, 1000, 500),
                           "/proc/4242/statm": "1 2\n"})
        p = monitor.Process(("satdump-ui", "satdump"))
        with mock.patch.object(monitor.os, "listdir", return_value=["1", "self", "4242"]) as listing:
            self.assertEqual(p.sample(3600.0)["pid"], 4242)
            self.clock[0] += 10
            self.assertEqual(p.sample(3610.0)["pid"], 4242)
            self.assertEqual(listing.call_count, 1)  # the known pid is checked, not searched for again
        for key in ("/proc/4242/comm", "/proc/4242/stat", "/proc/4242/statm"):
            del self.files[key]
        self.files.update({"/proc/5151/comm": "satdump\n", "/proc/5151/stat": self.stat(5151, "satdump", 10, 5, 360000)})
        self.clock[0] += 10
        with mock.patch.object(monitor.os, "listdir", return_value=["1", "5151"]):
            sample = p.sample(3620.0)
        self.assertEqual((sample["pid"], sample["cpu"], sample["up"]), (5151, None, 20.0))


if __name__ == "__main__":
    unittest.main()
