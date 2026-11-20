"""Tests for dcs.py: reading SatDump's DCS files, the fallback decoders, the database behind the
DCS page, the day archive and DCS retention.

Fixtures are built in code in the shape SatDump writes: <run>/DCS/pH-YYDDDHHMMSS-A.dcs.json.
"""

import calendar
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dcs  # noqa: E402

RUN = "2026-11-20_04-57_goes_hrit_1.6941 GHz"


def name_for(t):
    """SatDump's name for a DCS file made at time t (UTC)."""
    g = time.gmtime(t)
    return "pH-%02d%03d%02d%02d%02d-A.dcs.json" % (g.tm_year % 100, g.tm_yday, g.tm_hour, g.tm_min, g.tm_sec)


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".250"


def message(address, text, t, values=None, kind="Unknown", dcp=None, **header):
    """One "DCS Message" block as SatDump writes it."""
    head = {"corrected_address": address, "carrier_start": stamp(t - 4), "message_end": stamp(t), "channel": 77,
            "spacecraft": "GOES-East", "data_rate": "300", "signal_strength": 39.900001525878906,
            "phase_noise": 2.119999885559082, "freq_offset": -0.10000000149011612, "crc_pass": True,
            "parity_errors": False, "no_eot": False}
    head.update(header)
    return {"type": "DCS Message", "data_ascii": text, "data_raw": "", "data_type": kind, "data_values": values or [],
            "dcp": dcp, "header": head}


def missed(address):
    return {"type": "Missed Message", "dcp": None,
            "header": {"platform_address": address, "channel": 12, "spacecraft": "GOES-West", "crc_pass": True}}


def document(blocks):
    return json.dumps({"name": "x.dcs", "source": "NCCF", "type": "DCSH", "blocks": blocks}).encode()


def pseudo(value):
    """One 18-bit reading as three pseudobinary characters."""
    value &= 0x3FFFF
    return "".join("?" if (value >> s) & 63 == 63 else chr(64 + ((value >> s) & 63)) for s in (12, 6, 0))


def touch(path, text=""):
    with open(path, "w") as f:
        f.write(text)
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


def readings(result):
    return [(name, values) for name, _, _, values in result]


class Names(unittest.TestCase):
    def test_file_time_reads_the_utc_stamp_in_the_name(self):
        """pH-26324041523-A.dcs.json is day 324 of 2026 at 04:15:23 UTC."""
        self.assertEqual(dcs.file_time("pH-26324041523-A.dcs.json"), calendar.timegm((2026, 11, 20, 4, 15, 23)))

    def test_file_time_round_trips(self):
        t = calendar.timegm((2027, 1, 1, 0, 0, 5))
        self.assertEqual(dcs.file_time(name_for(t)), t)

    def test_file_time_rejects_other_names(self):
        """Only SatDump's DCS names count: this is what keeps retention and the archive off other files."""
        for name in ("notes.txt", "pH-not-a-dcs-file.json", "pH-26324041523-A.dcs", "pH-26999041523-A.dcs.json",
                     "pH-26324251523-A.dcs.json", "x" + name_for(0)):
            self.assertIsNone(dcs.file_time(name), name)

    def test_parse_time(self):
        self.assertAlmostEqual(dcs.parse_time("2026-11-20 03:15:05.288"), calendar.timegm((2026, 11, 20, 3, 15, 5)) + 0.288)
        self.assertIsNone(dcs.parse_time("soon"))
        self.assertIsNone(dcs.parse_time(None))

    def test_tidy_strips_satdumps_padding(self):
        self.assertEqual(dcs.tidy("6.830000"), "6.83")
        self.assertEqual(dcs.tidy("03.34"), "3.34")
        self.assertEqual(dcs.tidy("0991"), "991")
        self.assertEqual(dcs.tidy("-0.0"), "-0")
        self.assertEqual(dcs.tidy("N36o20'41"), "N36o20'41")

    def test_position_treats_zero_zero_as_unknown(self):
        self.assertEqual(dcs.position(32.5614, -93.6461), (32.5614, -93.6461))
        self.assertEqual(dcs.position(0, 0), (None, None))
        self.assertEqual(dcs.position(None, 5), (None, None))
        self.assertEqual(dcs.position(95, 10), (None, None))

    def test_platform_name_and_details(self):
        dcp = {"description": "DELAWARE RIVER AT LORDVILLE", "state": "NY", "agency": "USGS01", "lat": 41.867221, "lon": -75.214164,
               "manufacturer": "", "transmit_interval": 60, "transmit_window": -1,
               "pe_info": [{"name": "Height, river stage (FT, M)", "record_interval": 15}] * 2}
        self.assertEqual(dcs.platform_name(dcp), "DELAWARE RIVER AT LORDVILLE · NY · USGS01")
        kept = json.loads(dcs.platform_details(dcp))
        self.assertEqual(kept["sensors"], "Height, river stage (FT, M)")
        self.assertNotIn("manufacturer", kept)
        self.assertNotIn("transmit_window", kept)
        self.assertIsNone(dcs.platform_name(None))
        self.assertIsNone(dcs.platform_details({}))


class Parse(unittest.TestCase):
    def test_times_are_aligned_to_the_file_by_whole_hours(self):
        """SatDump has been seen printing times exactly an hour off the file's own name."""
        made = calendar.timegm((2026, 11, 20, 4, 15, 23))
        rows, _ = dcs.parse(document([message("16876222", "x", made - 3600 - 12)]), made)
        self.assertEqual(rows[0][0], made - 12 - 4)

    def test_times_already_right_are_left_alone(self):
        made = calendar.timegm((2026, 11, 20, 4, 15, 23))
        rows, _ = dcs.parse(document([message("16876222", "x", made - 9)]), made)
        self.assertEqual(rows[0][0], made - 9 - 4)

    def test_fields_flags_and_missed(self):
        made = calendar.timegm((2026, 11, 20, 4, 15, 23))
        rows, gone = dcs.parse(document([
            message("16876222", ":HG 15 #15 3.91 3.90", made, kind="SHEF", parity_errors=True, spacecraft="GOES-West",
                    values=[{"name": "Stage", "interval": 15, "reading_age": 15, "values": ["3.910000", "3.900000"]}]),
            missed("cb0302f6"), {"type": "Something else"}, message("not hex!", "x", made)]), made)
        self.assertEqual(len(rows), 1)
        t, addr, ch, sat, rate, sig, noise, off, flags, typ, txt, vals, name, details, lat, lon = rows[0]
        self.assertEqual((addr, ch, sat, rate, typ), (0x16876222, 77, 2, 300, "SHEF"))
        self.assertEqual((sig, noise, off), (39.9, 2.12, -0.1))
        self.assertTrue(flags & 1 << dcs.FLAGS.index("parity_errors"))
        self.assertTrue(flags & dcs.BAD)
        self.assertEqual(json.loads(vals), [["Stage", 15, 15, ["3.91", "3.9"]]])
        self.assertEqual(gone, [(0xcb0302f6, 12, 2)])

    def test_satdumps_decoding_is_never_replaced(self):
        """The fallback decoders only run when SatDump gave no values."""
        made = 1795150000
        rows, _ = dcs.parse(document([message("16876222", "1.0 2.0\n1.1 2.1", made, kind="ASCII Columns",
                                              values=[{"name": "Stage", "interval": 0, "reading_age": 0, "values": ["1.0"]}])]), made)
        self.assertEqual(rows[0][9], "ASCII Columns")

    def test_undecoded_tables_are_read_and_marked_inferred(self):
        made = 1795150000
        rows, _ = dcs.parse(document([message("56a5fe3c", "\r\n12.96 66.24\r\n12.94 66.24\r\n", made, kind="ASCII")]), made)
        self.assertEqual(rows[0][9], "Table (inferred)")
        self.assertEqual(json.loads(rows[0][11]), [["Column 1", 0, 0, ["12.96", "12.94"]], ["Column 2", 0, 0, ["66.24", "66.24"]]])

    def test_position_comes_from_satdumps_platform_record(self):
        made = 1795150000
        rows, _ = dcs.parse(document([message("de94d8f0", "x", made, dcp={"description": "OWYHEE RIVER", "lat": 41.6889, "lon": -115.8439})]), made)
        self.assertEqual(rows[0][12], "OWYHEE RIVER")
        self.assertEqual(rows[0][14:16], [41.6889, -115.8439])

    def test_a_file_that_is_not_json_raises(self):
        with self.assertRaises(ValueError):
            dcs.parse(b'{"blocks": [', 0)


class Labelled(unittest.TestCase):
    def test_labels_with_age_and_interval(self):
        got = dcs.labelled(":PRS 1 #2 3048 2993 :RAD 2 #1 -2410 -2400 :BAT 12.1")
        self.assertEqual(got, [["PRS", 2, 1, ["3048", "2993"]], ["RAD", 1, 2, ["-2410", "-2400"]], ["BAT", 0, 0, ["12.1"]]])

    def test_a_stray_label_inside_binary_is_not_a_decoding(self):
        self.assertIsNone(dcs.labelled("P90870691@@@@@@@v0oCE3@SDf@U3@MDi@V4AC6bz<BH i :PWR 2.4/0.0"))

    def test_text_labels_are_skipped(self):
        got = dcs.labelled(":BAT 12.1 :NAME GAMBIER :PRS 5942 5944")
        self.assertEqual([g[0] for g in got], ["BAT", "PRS"])


class Table(unittest.TestCase):
    def test_equal_lines_are_columns_over_time(self):
        got = dcs.table("\n7.57 22.86 67.06\n7.57 22.86 66.98\n7.58 22.86 67.02\n", [])
        self.assertEqual(readings(got), [("Column 1", ["7.57", "7.57", "7.58"]), ("Column 2", ["22.86", "22.86", "22.86"]),
                                         ("Column 3", ["67.06", "66.98", "67.02"])])

    def test_lines_that_are_each_one_sensor(self):
        """Two lines, each steady along its length: the lines are the sensors."""
        got = dcs.table("\n3.17 3.17 3.17 3.17\n3.26 3.26 3.26 3.26\n", [])
        self.assertEqual(readings(got), [("Line 1", ["3.17"] * 4), ("Line 2", ["3.26"] * 4)])

    def test_a_weather_stations_list_is_one_sensor_per_line(self):
        got = dcs.table("\r\n15.82 \r\n003 \r\n206 \r\n039 \r\n", [])
        self.assertEqual(readings(got), [("Line 1", ["15.82"]), ("Line 2", ["3"]), ("Line 3", ["206"]), ("Line 4", ["39"])])

    def test_single_numbers_close_together_are_one_sensor(self):
        got = dcs.table("\n2.80\n2.80\n2.81\n2.80 11.9\n", [])
        self.assertEqual(readings(got), [("Lines 1 to 3", ["2.8", "2.8", "2.81", "2.8"]), ("Last value", ["11.9"])])

    def test_a_value_tacked_on_one_line(self):
        got = dcs.table("\n11.19 19.09\n11.19 19.09\n11.20 19.09\n11.21 19.09 12.3\n", [])
        self.assertEqual(readings(got), [("Column 1", ["11.19", "11.19", "11.2", "11.21"]), ("Column 2", ["19.09"] * 4),
                                         ("Last value", ["12.3"])])

    def test_one_line_is_one_reading_per_sensor(self):
        got = dcs.table("12.73 10.27 77.60", [])
        self.assertEqual(readings(got), [("Column 1", ["12.73"]), ("Column 2", ["10.27"]), ("Column 3", ["77.6"])])

    def test_station_names_are_used_when_the_count_fits(self):
        got = dcs.table("1.5 12.9\n1.6 12.9", [("River stage", 15), ("Battery", 60)])
        self.assertEqual([g[0] for g in got], ["River stage", "Battery"])
        got = dcs.table("1.5 12.9\n1.6 12.9", [("River stage", 15)])
        self.assertEqual([g[0] for g in got], ["Column 1", "Column 2"])

    def test_text_is_not_a_table(self):
        self.assertIsNone(dcs.table("BATTERY LOW 12.1", []))
        self.assertIsNone(dcs.table("+49.9/////", []))
        self.assertIsNone(dcs.table("7", []))
        self.assertIsNone(dcs.table("", []))


class PseudobinaryB(unittest.TestCase):
    def decode(self, head, values, tail="", sensors=()):
        return dcs.pseudobinary_b(head + "".join("///" if v is None else pseudo(v) for v in values) + tail, list(sensors))

    def test_values_have_two_decimals_and_a_sign(self):
        got = self.decode("B1O", [597, -357, 0])
        self.assertEqual([g[3] for g in got], [["5.97"], ["-3.57"], ["0"]])

    def test_one_sensor_stays_whole_through_a_trend(self):
        got = self.decode("B1D", [289, 288, 287, 285, 283, 279, 273, 269, 268, 270, 275, 285, 302], "H")
        self.assertEqual([g[0] for g in got], ["Sensor 1", dcs.BATTERY])
        self.assertEqual(len(got[0][3]), 13)

    def test_blocks_of_equal_size(self):
        got = self.decode("B1O", [160, 160, 161, 161, 740, 741, 741, 742], "G")
        self.assertEqual(readings(got)[:2], [("Sensor 1", ["1.6", "1.6", "1.61", "1.61"]), ("Sensor 2", ["7.4", "7.41", "7.41", "7.42"])])

    def test_a_lively_sensor_is_not_cut_up(self):
        """Wind or temperature jumps about; the block size keeps it together."""
        got = self.decode("B1G", [541, 541, 541, 541, 923, 931, 938, 946, 2020, 2845, 2785, 2592], "I")
        self.assertEqual([len(g[3]) for g in got], [4, 4, 4, 1])

    def test_blocks_of_unequal_size(self):
        got = self.decode("B1H", [-2850] * 12 + [1325] * 2 + [0] * 6)
        self.assertEqual([(g[3][0], len(g[3])) for g in got], [("-28.5", 12), ("13.25", 2), ("0", 6)])

    def test_sensors_taking_turns(self):
        values = [v for t in range(8) for v in (1118 + t % 2, 1038, 1112 + t // 3)]
        got = self.decode("BST", values, "f")
        self.assertEqual([len(g[3]) for g in got], [8, 8, 8, 1])
        self.assertEqual(set(got[1][3]), {"10.38"})

    def test_station_names_when_they_fit(self):
        """The station lists a battery sensor; the message carries it as the last character."""
        sensors = [("Height, river stage (FT, M)", 60), (dcs.BATTERY, 60)]
        got = self.decode("B1O", [652, 648, 644, 640], "I", sensors)
        self.assertEqual([g[0] for g in got], ["Height, river stage (FT, M)", dcs.BATTERY])
        self.assertEqual(got[0][1:3], [60, 15])

    def test_battery_formulas_match_satdump(self):
        self.assertEqual(self.decode("B1O", [597], "I")[-1][3], ["12.71"])    # 9 * 0.234 + 10.6
        self.assertEqual(self.decode("B1O", [597], "<")[-1][3], ["9.66"])     # below '@': a low battery
        self.assertEqual(self.decode("BCT", [165, 165, 166], "g")[-1][3], ["12.49"])  # 39 * 0.313 + 0.285

    def test_missing_readings_stay_missing(self):
        got = self.decode("B1O", [597, None, 598, 597], "I")
        self.assertEqual(got[0][3], ["5.97", "", "5.98", "5.97"])

    def test_text_after_the_data_is_ignored(self):
        got = dcs.pseudobinary_b("B1O" + pseudo(160) + pseudo(160) + "G 2103543 ", [])
        self.assertEqual(got[0][3], ["1.6", "1.6"])

    def test_things_that_are_not_this_format(self):
        for text in ("BATTERY LOW 12.1", "BSTATION OK", "", "B1", "B1O@@", "C1+AEDC?@O@OL", "B1O" + "12" * 6, "B1O" + "@@" * 4 + "!",
                     "B5O" + pseudo(5) * 3,
                     "B1O" + "".join(pseudo(v) for v in (5, 90000, -70000, 1234, 99999, -5, 40000, -123456, 7777, 31000, -90000, 222, 65000, -1))):
            self.assertIsNone(dcs.pseudobinary_b(text, []), text)


class Fallback(unittest.TestCase):
    def test_each_kind_is_named(self):
        self.assertEqual(dcs.fallback(":PRS 1 #1 3048 2993", None)[0], "Labelled (inferred)")
        self.assertEqual(dcs.fallback("B1O" + pseudo(597) * 4 + "I", None)[0], "Pseudobinary B (inferred)")
        self.assertEqual(dcs.fallback("1.5 12.9\n1.6 12.9", None)[0], "Table (inferred)")

    def test_what_cannot_be_read_is_left(self):
        for text in ("", "P90440201kRm@@@@F0oCD!@qB@@@.@qB<BF!@qC@@@.@qC<BG g", "OTA@ZThdrD@@J@PkGX}AN_PpGD|AQ_@xGP}A", "x" * 7000):
            self.assertIsNone(dcs.fallback(text, None), text[:20])

    def test_sensor_names_come_from_the_platform_record(self):
        dcp = {"pe_info": [{"name": "River stage", "record_interval": 15}, {"name": "Battery", "record_interval": 60}]}
        self.assertEqual([g[0] for g in dcs.fallback("1.5 12.9\n1.6 12.9", dcp)[1]], ["River stage", "Battery"])


PDT_LINE = ("USGS01" "16876222" "S" "077" "R" "121" "005150" "010000" "0010" "0300" "A" "LA" " " " " "FLAT RIVER AT SHED RD. NEAR BOS"
            " 325614" "-0936461" "L" "SUTRON        " "SATLINK3 SL3-1  ").ljust(239)


class PlatformTable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "pdt.txt")

    def test_fixed_width_fields(self):
        self.assertEqual(len(PDT_LINE), 239)
        nowhere = "NWSARH" + "15C396A6" + PDT_LINE[14:47] + " " * 31 + " 000000" + " 0000000" + "L" + "UNKNOWN".ljust(14) + "UNKNOWN".ljust(16)
        with open(self.path, "w") as f:
            f.write(PDT_LINE + "\n" + nowhere.ljust(239) + "\n<html>Document Moved</html>\n\n")
        table = dcs.read_pdt(self.path)
        self.assertEqual(sorted(table), [0x15c396a6, 0x16876222])
        label, details, lat, lon = table[0x16876222]
        self.assertEqual(label, "FLAT RIVER AT SHED RD. NEAR BOS · LA · USGS01")
        self.assertEqual((lat, lon), (32.5614, -93.6461))
        self.assertEqual(json.loads(details)["model"], "SATLINK3 SL3-1")
        self.assertEqual(table[0x15c396a6][2:], (None, None))
        self.assertNotIn("manufacturer", json.loads(table[0x15c396a6][1]))


class StoreCase(unittest.TestCase):
    """A DcsStore over a temporary SatDump tree, driven by hand (its thread is never started)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.run_folder = os.path.join(self.dir.name, "out", RUN)
        os.makedirs(os.path.join(self.run_folder, "DCS"))
        self.cache = os.path.join(self.dir.name, "cache")
        os.makedirs(self.cache)
        self.logged = []
        self.now = int(time.time())

    def write(self, t, blocks, settle=True):
        path = os.path.join(self.run_folder, "DCS", name_for(t))
        with open(path, "wb") as f:
            f.write(document(blocks))
        if settle:  # SatDump finished writing it a while ago
            os.utime(path, (time.time() - 30, time.time() - 30))
        return path

    def store(self, history=3 * 86400, stations=None):
        s = dcs.DcsStore(self.cache, lambda: [self.run_folder], history, self.logged.append, stations)
        self.db = s.connect()
        self.addCleanup(self.db.close)
        return s

    def drain(self, s):
        while s.sweep(self.db):
            pass


class Store(StoreCase):
    def test_files_are_read_once(self):
        t = self.now - 600
        self.write(t, [message("16876222", "x", t), message("16876223", "y", t)])
        s = self.store()
        self.assertFalse(s.present)
        self.drain(s)
        self.drain(s)
        self.assertTrue(s.present)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        again = self.store()   # a restart remembers what was read
        self.drain(again)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_a_file_still_being_written_waits(self):
        t = self.now - 5
        self.write(t, [message("16876222", "x", t)], settle=False)
        s = self.store()
        self.drain(s)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_a_broken_file_is_retried_then_recorded(self):
        path = os.path.join(self.run_folder, "DCS", name_for(self.now - 600))
        with open(path, "w") as f:
            f.write('{"blocks": [')
        os.utime(path, (time.time() - 30,) * 2)
        s = self.store()
        for _ in range(6):
            s.sweep(self.db)
        self.assertEqual(self.db.execute("SELECT n FROM files").fetchall(), [(0,)])
        self.assertTrue(any("cannot read" in line for line in self.logged))

    def test_other_files_in_the_folder_are_ignored(self):
        touch(os.path.join(self.run_folder, "DCS", "notes.txt"), "mine")
        touch(os.path.join(self.run_folder, "DCS", "pH-not-a-dcs-file.json"), "{}")
        s = self.store()
        self.drain(s)
        self.assertFalse(s.present)

    def test_the_platform_row_keeps_the_latest_message(self):
        t = self.now - 3000
        self.write(t + 600, [message("16876222", "newer", t + 600)])
        s = self.store()
        self.drain(s)
        self.write(t, [message("16876222", "older", t), missed("16876222"), missed("aaaa0001")])   # arrives out of order
        self.drain(s)
        row = self.db.execute("SELECT n, txt, first, last, missed FROM platforms WHERE addr = ?", (0x16876222,)).fetchone()
        self.assertEqual(row[:2], (2, "newer"))
        self.assertEqual(row[2:], (t - 4, t + 596, 1))
        never = self.db.execute("SELECT n, missed FROM platforms WHERE addr = ?", (0xaaaa0001,)).fetchone()
        self.assertEqual(never, (0, 1))

    def test_summary(self):
        t = self.now - 300
        self.write(t, [message("16876222", ":HG 1 #15 1.5 1.6", t, kind="SHEF",
                               values=[{"name": "Stage", "interval": 15, "reading_age": 1, "values": ["1.5", "1.6"]}]),
                       message("16876223", "1.5 12.9\n1.6 12.9", t, spacecraft="GOES-West"),
                       message("16876224", "OTA@ZThdr", t, parity_errors=True), missed("cb0302f6")])
        s = self.store()
        self.drain(s)
        summary = s.summary()
        hour = summary["hour"]
        self.assertEqual((hour["n"], hour["east"], hour["west"], hour["bad"], hour["missed"]), (3, 2, 1, 1, 1))
        self.assertEqual((hour["decoded"], hour["inferred"]), (2, 1))
        self.assertEqual(dict(hour["types"]), {"SHEF": 1, "Table (inferred)": 1, "Unknown": 1})
        self.assertEqual(hour["channels"], [(77, 3)])
        self.assertEqual(len(summary["minutes"]), 60)
        self.assertEqual(sum(summary["minutes"]), 3)
        self.assertEqual(summary["heard"]["hour"], 3)
        self.assertEqual(summary["store"]["files"], 1)
        self.assertEqual(s.brief()["heard"], 3)

    def test_brief_is_none_without_dcs(self):
        """No DCS files: no DCS page."""
        self.assertIsNone(self.store().brief())

    def test_platform_list_filters_search_and_order(self):
        t = self.now - 300
        dcp = {"description": "SAPPA CREEK NEAR STAMFORD", "state": "NE", "agency": "USGS01", "lat": 40.1186, "lon": -99.5172}
        self.write(t, [message("16876222", "x", t, kind="SHEF", dcp=dcp, signal_strength=41.0,
                               values=[{"name": "Stage", "interval": 15, "reading_age": 1, "values": ["5.970000"]}]),
                       message("16876223", "1.5 12.9\n1.6 12.9", t, signal_strength=35.0),
                       message("26876224", "plain words here", t, kind="ASCII", signal_strength=38.0),
                       message("36876225", "OTA@ZThdr", t, crc_pass=False, signal_strength=30.0), missed("cb0302f6")])
        s = self.store()
        self.drain(s)
        count = lambda **kw: s.platforms(**kw)["count"]
        self.assertEqual(count(), 4)
        self.assertEqual((count(kind="values"), count(kind="inferred"), count(kind="text"), count(kind="binary")), (2, 1, 1, 1))
        self.assertEqual((count(kind="flagged"), count(kind="missed")), (1, 1))
        self.assertEqual([p["a"] for p in s.platforms(q="1687")["items"]], ["16876222", "16876223"])
        self.assertEqual([p["a"] for p in s.platforms(q="sappa")["items"]], ["16876222"])
        self.assertEqual(count(q="100%"), 0)   # LIKE wildcards in a search are taken literally
        self.assertEqual([p["a"] for p in s.platforms(sort="strong")["items"]][0], "16876222")
        self.assertEqual([p["a"] for p in s.platforms(sort="weak")["items"]][0], "36876225")
        first = s.platforms(q="16876222")["items"][0]
        self.assertEqual((first["nm"], first["v"]), ("SAPPA CREEK NEAR STAMFORD · NE · USGS01", "Stage 5.97"))
        page = s.platforms(limit=2)
        self.assertTrue(page["more"])
        self.assertEqual(len(s.platforms(limit=2, offset=2)["items"]), 2)

    def test_one_platform(self):
        t = self.now - 7200
        for i in range(3):
            self.write(t + i * 3600 - 1, [message("16876222", f"msg {i}", t + i * 3600 - 1,
                                                   values=[{"name": "Stage", "interval": 15, "reading_age": 1, "values": ["1.500000"]}])])
        s = self.store()
        self.drain(s)
        got = s.platform("16876222")
        self.assertEqual((got["a"], got["n"]), ("16876222", 3))
        self.assertEqual([m["x"] for m in got["messages"]], ["msg 2", "msg 1", "msg 0"])
        self.assertEqual(got["messages"][0]["v"], [["Stage", 15, 1, ["1.5"]]])
        self.assertIsNone(s.platform("deadbeef"))
        self.assertIsNone(s.platform("not hex"))

    def test_map_has_only_platforms_with_a_position(self):
        t = self.now - 300
        self.write(t, [message("16876222", "x", t, dcp={"description": "A", "lat": 40.1, "lon": -99.5},
                               values=[{"name": "Stage", "interval": 15, "reading_age": 1, "values": ["1"]}], kind="SHEF"),
                       message("16876223", "1.5 12.9\n1.6 12.9", t, dcp={"description": "B", "lat": -19.7, "lon": -69.2}),
                       message("16876224", "OTA@", t, dcp={"description": "C", "lat": 0.0, "lon": 0.0}),
                       message("16876225", "OTA@", t)])
        s = self.store()
        self.drain(s)
        got = s.map()
        self.assertEqual((got["heard"], got["placed"]), (4, 2))
        self.assertEqual(sorted((p[0], p[1], p[2], p[4]) for p in got["points"]),
                         [("16876222", 40.1, -99.5, 3), ("16876223", -19.7, -69.2, 2)])

    def test_history_is_trimmed_but_platforms_are_kept(self):
        old, new = self.now - 5 * 3600, self.now - 600
        self.write(old, [message("16876222", "old", old)])
        self.write(new, [message("16876223", "new", new)])
        s = self.store(history=10 * 3600)
        self.drain(s)
        s.history = 2 * 3600
        s.trim(self.db)
        self.assertEqual([r[0] for r in self.db.execute("SELECT txt FROM messages")], ["new"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM platforms").fetchone()[0], 2)
        self.drain(s)   # the old file is still on disk, and is not read back in
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertTrue(any("SatDump's files are not touched" in line for line in self.logged))

    def test_files_older_than_the_history_are_not_read(self):
        old = self.now - 5 * 86400
        self.write(old, [message("16876222", "old", old)])
        s = self.store(history=86400)
        self.drain(s)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_a_new_database_reads_packed_days_back(self):
        t = self.now - 3600
        path = self.write(t, [message("16876222", "packed", t)])
        os.makedirs(os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR))
        with zipfile.ZipFile(os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR, time.strftime("%Y-%m-%d", time.gmtime(t)) + ".zip"), "w") as zf:
            zf.write(path, os.path.basename(path))
        os.unlink(path)
        s = self.store()
        self.drain(s)
        self.assertEqual([r[0] for r in self.db.execute("SELECT txt FROM messages")], ["packed"])
        self.drain(s)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_noaas_table_names_what_satdump_does_not(self):
        table = os.path.join(self.dir.name, "pdt.txt")
        with open(table, "w") as f:
            f.write(PDT_LINE + "\n")
        t = self.now - 300
        self.write(t, [message("16876222", "x", t)])
        s = self.store(stations=table)
        self.drain(s)                   # heard before the table was loaded
        s.load_stations(self.db)        # ... and named afterwards
        self.assertEqual(self.db.execute("SELECT name, lat, lon FROM platforms").fetchone(),
                         ("FLAT RIVER AT SHED RD. NEAR BOS · LA · USGS01", 32.5614, -93.6461))
        self.write(t + 60, [message("16876222", "y", t + 60, dcp={"description": "FROM SATDUMP", "lat": 1.5, "lon": 2.5})])
        self.drain(s)
        self.assertEqual(self.db.execute("SELECT name, lat FROM platforms").fetchone(), ("FROM SATDUMP", 1.5))
        self.assertEqual(s.summary()["store"]["stations"], 1)

    def test_no_table_no_requests(self):
        """Off by default: nothing is fetched and nothing is named."""
        s = self.store()
        s.load_stations(self.db)
        self.assertEqual(s.stations, {})
        self.assertFalse(os.path.exists(os.path.join(self.cache, "dcs-platforms.txt")))

    def test_earlier_database_layouts_are_removed(self):
        for name in ("dcs-1.sqlite3", "dcs-2.sqlite3-wal", "monitor-1.sqlite3", "text-index-2.sqlite3"):
            touch(os.path.join(self.cache, name))
        self.store()
        self.assertEqual(sorted(n for n in os.listdir(self.cache) if not n.startswith("dcs-3")), ["monitor-1.sqlite3", "text-index-2.sqlite3"])


class Archive(StoreCase):
    def archiver(self, dry_run=False):
        return dcs.DcsArchiver(lambda: [self.run_folder], dry_run, self.logged.append, lambda st: st.st_size)

    def test_a_finished_day_is_packed_and_verified(self):
        yesterday, today = self.now - 86400 - 7200, self.now - 60
        old = [self.write(yesterday + i, [message("16876222", f"m{i}", yesterday + i)]) for i in range(3)]
        originals = {os.path.basename(p): read(p) for p in old}
        kept = self.write(today, [message("16876222", "today", today)])
        mine = touch(os.path.join(self.run_folder, "DCS", "notes.txt"), "mine")
        a = self.archiver()
        a.sweep()
        packed = os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR, time.strftime("%Y-%m-%d", time.gmtime(yesterday)) + ".zip")
        with zipfile.ZipFile(packed) as zf:
            self.assertEqual({n: zf.read(n) for n in zf.namelist()}, originals)
        self.assertFalse(any(os.path.exists(p) for p in old))
        self.assertTrue(os.path.exists(kept))      # today's files are left alone
        self.assertTrue(os.path.exists(mine))      # and so is anything that is not a DCS file
        self.assertEqual((a.status()["days"], a.status()["last"]["n"]), (1, 3))
        before = os.path.getmtime(packed)
        a.sweep()                                  # nothing left to do
        self.assertEqual(os.path.getmtime(packed), before)

    def test_dry_run_packs_nothing(self):
        t = self.now - 2 * 86400
        path = self.write(t, [message("16876222", "x", t)])
        self.archiver(dry_run=True).sweep()
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR)))
        self.assertTrue(any("would pack" in line for line in self.logged))

    def test_late_arrivals_join_the_days_archive(self):
        t = self.now - 2 * 86400
        self.write(t, [message("16876222", "first", t)])
        a = self.archiver()
        a.sweep()
        self.write(t + 5, [message("16876222", "late", t + 5)])
        a.sweep()
        packed = os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR, time.strftime("%Y-%m-%d", time.gmtime(t)) + ".zip")
        with zipfile.ZipFile(packed) as zf:
            self.assertEqual(sorted(zf.namelist()), [name_for(t), name_for(t + 5)])
        self.assertEqual(os.listdir(os.path.join(self.run_folder, "DCS")), [])

    def test_a_failed_archive_leaves_the_files(self):
        t = self.now - 2 * 86400
        path = self.write(t, [message("16876222", "x", t)])
        out = os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR)
        touch(out)   # a file where the archive folder should be: the zip cannot be written
        self.archiver().sweep()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(any("NOT packed" in line for line in self.logged))


class Retention(StoreCase):
    def test_only_dcs_files_and_day_archives_are_offered(self):
        t = self.now - 86400
        a = self.write(t, [message("16876222", "x", t)])
        b = self.write(t + 3600, [message("16876222", "y", t + 3600)])
        os.makedirs(os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR))
        day = os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR, "2026-11-01.zip")
        for other in (day, os.path.join(self.run_folder, dcs.DCS_ARCHIVE_DIR, "holiday.zip"),
                      os.path.join(self.run_folder, "DCS", "notes.txt")):
            touch(other)
        os.makedirs(os.path.join(self.run_folder, "EMWIN"))
        touch(os.path.join(self.run_folder, "EMWIN", name_for(t)))   # a DCS name in the wrong place
        units = dcs.retention_units([self.run_folder])
        self.assertEqual([u[2] for u in units], [day, a, b])
        self.assertEqual(units[0][0], calendar.timegm((2026, 11, 1, 23, 59, 59)))
        self.assertEqual([u[0] for u in units[1:]], [t, t + 3600])
        self.assertTrue(all(u[1] is None and u[3] is False for u in units))

    def test_a_run_without_dcs_offers_nothing(self):
        self.assertEqual(dcs.retention_units([os.path.join(self.dir.name, "nowhere")]), [])


if __name__ == "__main__":
    unittest.main()
