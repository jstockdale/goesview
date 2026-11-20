"""classify(): from a path inside the SatDump folder to where it shows in the viewer.

The paths here are SatDump 1.2's real names for GOES HRIT output. A wrong answer
from classify() files a product under the wrong page or drops it, so each product
kind the README lists gets a case.
"""
import calendar
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import RUN  # noqa: E402

goesview = fx.goesview

SLOT = "2026-11-17_20-10-21"
SLOT_TIME = calendar.timegm((2026, 11, 17, 20, 10, 21))


def classify(path):
    return goesview.classify(path.split("/"))


class ImageryTests(unittest.TestCase):
    def test_abi_band(self):
        """A GOES channel: Imagery, grouped by satellite and sector, dated by its scan folder."""
        spec = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/G18_2_20261117T201021Z.png")
        self.assertEqual((spec.section, spec.group, spec.label, spec.kind),
                         ("imagery", "GOES-18 · Full Disk", "Band 2 · Red visible", "image"))
        self.assertEqual(spec.time, SLOT_TIME)
        self.assertEqual(spec.slot, SLOT)
        self.assertTrue(spec.raw)
        self.assertFalse(spec.is_map)

    def test_band_descriptions_and_order(self):
        """Bands sort by number (13 after 2), after the composites."""
        ranks = {}
        for band, desc in ((1, "Blue"), (2, "Red visible"), (7, "Shortwave IR"), (13, "Clean longwave IR"), (16, "CO2 longwave IR")):
            spec = classify(f"IMAGES/GOES-18/Full Disk/{SLOT}/G18_{band}_20261117T201021Z.png")
            self.assertEqual(spec.label, f"Band {band} · {desc}")
            ranks[band] = spec.rank
        self.assertEqual(sorted(ranks, key=ranks.get), [1, 2, 7, 13, 16])
        composite = classify(f"IMAGES/GOES-18/Full Disk/{SLOT}/abi_rgb_ABI_False_Color.png")
        self.assertLess(composite.rank, min(ranks.values()))

    def test_other_satellites_relayed_by_goes_18(self):
        """GOES-18 HRIT also relays GOES-19 and Himawari; Himawari has its own band names."""
        g19 = classify(f"{RUN}/IMAGES/GOES-19/Full Disk/{SLOT}/G19_13_20261117T201021Z.png")
        self.assertEqual((g19.group, g19.label), ("GOES-19 · Full Disk", "Band 13 · Clean longwave IR"))
        him = classify(f"{RUN}/IMAGES/Himawari-9/Full Disk/{SLOT}/HIM_3_20261117T200000Z.png")
        self.assertEqual((him.group, him.label), ("Himawari-9 · Full Disk", "Band 3 · Red visible"))
        unnamed = classify(f"{RUN}/IMAGES/Himawari-9/Full Disk/{SLOT}/HIM_5_20261117T200000Z.png")
        self.assertEqual(unnamed.label, "Band 5")

    def test_band_file_with_a_duplicate_suffix(self):
        spec = classify(f"IMAGES/GOES-18/Full Disk/{SLOT}/G18_13_20261117T201021Z_1.png")
        self.assertEqual(spec.label, "Band 13 · Clean longwave IR")

    def test_mesoscale_sector_is_its_own_group(self):
        spec = classify(f"{RUN}/IMAGES/GOES-18/Mesoscale 1/{SLOT}/G18_2_20261117T201021Z.png")
        self.assertEqual(spec.group, "GOES-18 · Mesoscale 1")

    def test_satdump_composite(self):
        """SatDump's own colour image: a processed composite, listed first."""
        spec = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/abi_rgb_ABI_False_Color.png")
        self.assertEqual((spec.section, spec.group, spec.label), ("imagery", "GOES-18 · Full Disk", "ABI False Color"))
        self.assertEqual(spec.rank, 1.0)
        self.assertFalse(spec.raw)
        self.assertEqual(spec.time, SLOT_TIME)
        other = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/abi_rgb_Infrared_Enhanced.png")
        self.assertEqual((other.label, other.rank), ("Infrared Enhanced", 2.0))

    def test_composite_with_map_overlay(self):
        """The _map twin is the same type as its base image, flagged as the overlay."""
        spec = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/abi_rgb_ABI_False_Color_map.png")
        self.assertEqual(spec.label, "ABI False Color")
        self.assertTrue(spec.is_map)

    def test_unrecognised_image_in_a_scan_folder(self):
        spec = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/something_else.jpg")
        self.assertEqual((spec.section, spec.label, spec.time), ("imagery", "something_else", SLOT_TIME))

    def test_image_outside_any_scan_folder(self):
        spec = classify(f"{RUN}/IMAGES/Unknown/whatever.png")
        self.assertEqual((spec.group, spec.label, spec.time, spec.slot), ("Unknown", "Other images", None, None))

    def test_scan_folder_with_an_impossible_date(self):
        """A garbled folder date is no reason to drop the image: it just has no data time."""
        spec = classify("IMAGES/GOES-18/Full Disk/2026-13-45_99-99-99/G18_2_20261117T201021Z.png")
        self.assertEqual(spec.label, "Band 2 · Red visible")
        self.assertIsNone(spec.time)


class WeatherTests(unittest.TestCase):
    def test_level_2_products(self):
        """Level 2 data goes to Weather, in a group marked Level 2, with its product name."""
        spec = classify(f"{RUN}/L2/GOES-18/Full Disk/{SLOT}/G18_ACHA_20261117T201021Z.png")
        self.assertEqual((spec.section, spec.group, spec.label),
                         ("weather", "GOES-18 · Full Disk · Level 2", "ACHA · Cloud top height (raw)"))
        self.assertTrue(spec.raw)
        self.assertEqual(spec.time, SLOT_TIME)
        sst = classify(f"{RUN}/L2/GOES-18/Full Disk/{SLOT}/G18_SST_20261117T201021Z.png")
        self.assertEqual(sst.label, "SST · Sea surface temperature (raw)")
        unknown = classify(f"{RUN}/L2/GOES-18/Full Disk/{SLOT}/G18_XYZ_20261117T201021Z.png")
        self.assertEqual(unknown.label, "XYZ · data (raw)")

    def test_nws_chart(self):
        """NWS charts are dated by the time in their name and labelled by chart."""
        spec = classify(f"{RUN}/IMAGES/NWS/20261117201100-pac_sfc_analysis.png")
        self.assertEqual((spec.section, spec.group, spec.label, spec.kind),
                         ("weather", "NWS charts", "pac_sfc_analysis", "image"))
        self.assertEqual(spec.time, calendar.timegm((2026, 11, 17, 20, 11, 0)))

    def test_nws_chart_name_variants(self):
        """The "_latest" and ".lrit" decorations do not make a different chart."""
        for name in ("20261117201100-pac_sfc_analysis_latest.png", "20261117201100-pac_sfc_analysis.lrit.gif",
                     "20261117201100-pac_sfc_analysis_latest72.lrit.jpg"):
            with self.subTest(name=name):
                self.assertEqual(classify(f"IMAGES/NWS/{name}").label, "pac_sfc_analysis")
        odd = classify("IMAGES/NWS/sketch.png")
        self.assertEqual((odd.group, odd.label, odd.time), ("NWS charts", "sketch", None))

    def test_emwin_graphic(self):
        spec = classify(f"{RUN}/EMWIN/Z_EIGZ00KWIN172011_C_KWIN_20261117201100_000101-2-RADNTHES.GIF")
        self.assertEqual((spec.section, spec.group, spec.label, spec.kind),
                         ("weather", "EMWIN graphics", "RADNTHES", "image"))
        self.assertEqual(spec.time, calendar.timegm((2026, 11, 17, 20, 11, 0)))


class TextTests(unittest.TestCase):
    def test_emwin_bulletin(self):
        """A bulletin: Text page, typed by product code, named by product, WMO header and office,
        dated by the issue time in the file name."""
        spec = classify(f"{RUN}/EMWIN/A_FXUS66KSEW172011_C_KWIN_20261117201100_000042-2-AFDSEWWA.TXT")
        self.assertEqual((spec.section, spec.group, spec.label, spec.kind),
                         ("text", "EMWIN", "AFD · Area forecast discussion", "text"))
        self.assertEqual(spec.name, "AFDSEWWA · FXUS66 KSEW")
        self.assertEqual(spec.time, calendar.timegm((2026, 11, 17, 20, 11, 0)))

    def test_emwin_bulletin_names_from_the_fixtures_are_real_enough(self):
        """The fixture names used across these tests are recognised the same way."""
        spec = classify(f"{RUN}/EMWIN/" + fx.emwin_name(SLOT_TIME, "TORSEWWA", serial=7, ext="txt"))
        self.assertEqual((spec.label, spec.time), ("TOR · Tornado warning", SLOT_TIME))

    def test_emwin_names_are_case_insensitive(self):
        spec = classify("EMWIN/a_fxus66ksew172011_c_kwin_20261117201100_000042-2-afdsewwa.txt")
        self.assertEqual((spec.label, spec.name), ("AFD · Area forecast discussion", "AFDSEWWA · FXUS66 KSEW"))

    def test_emwin_product_without_a_known_code(self):
        spec = classify("EMWIN/A_NOUS41KWBC172011_C_KWIN_20261117201100_000042-2-QQQWBCUS.TXT")
        self.assertEqual(spec.label, "QQQ")

    def test_emwin_file_with_another_name(self):
        text = classify(f"{RUN}/EMWIN/readme.txt")
        self.assertEqual((text.section, text.group, text.label, text.name, text.time),
                         ("text", "EMWIN", "Other", "readme", None))
        image = classify(f"{RUN}/EMWIN/picture.jpg")
        self.assertEqual((image.section, image.group, image.label), ("weather", "EMWIN", "Other"))

    def test_emwin_bulletin_with_an_impossible_date(self):
        spec = classify("EMWIN/A_FXUS66KSEW172011_C_KWIN_20261317201100_000042-2-AFDSEWWA.TXT")
        self.assertEqual(spec.label, "AFD · Area forecast discussion")
        self.assertIsNone(spec.time)

    def test_admin_message(self):
        spec = classify(f"{RUN}/Admin Messages/20261117T201100Z.txt")
        self.assertEqual((spec.section, spec.group, spec.label, spec.kind, spec.name),
                         ("text", "Admin Messages", "Admin Messages", "text", "20261117T201100Z"))


class IgnoredTests(unittest.TestCase):
    def test_files_outside_the_known_folders(self):
        """Only IMAGES/, L2/, EMWIN/ and Admin Messages/ are shown."""
        for path in (f"{RUN}/dataset.json", f"{RUN}/notes.txt", "picture.png", f"{RUN}/DCS/DCS_1.json",
                     f"{RUN}/DCS/screenshot.png", "IMAGES", "EMWIN.txt", f"{RUN}/images/lowercase.png"):
            with self.subTest(path=path):
                self.assertIsNone(classify(path))

    def test_other_file_kinds(self):
        """SatDump's sidecar files and anything that is not an image or text."""
        for name in ("product.cbor", "G18_2_20261117T201021Z.png.tmp", "notes.md", "data.json", "noextension", "movie.mp4"):
            with self.subTest(name=name):
                self.assertIsNone(classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/{name}"))

    def test_extension_case_does_not_matter(self):
        self.assertEqual(classify(f"IMAGES/GOES-18/Full Disk/{SLOT}/G18_2_20261117T201021Z.PNG").kind, "image")
        self.assertEqual(classify("EMWIN/whatever.TXT").kind, "text")
        self.assertEqual(classify("IMAGES/NWS/20261117201100-chart.JPEG").kind, "image")

    def test_root_that_is_itself_a_run(self):
        """Pointed at a run folder, paths start at IMAGES/ and classify the same."""
        inside = classify(f"IMAGES/GOES-18/Full Disk/{SLOT}/G18_2_20261117T201021Z.png")
        under = classify(f"{RUN}/IMAGES/GOES-18/Full Disk/{SLOT}/G18_2_20261117T201021Z.png")
        self.assertEqual((inside.section, inside.group, inside.label), (under.section, under.group, under.label))


class LooseTwinTests(unittest.TestCase):
    def test_archived_bulletin_maps_back_to_its_loose_path(self):
        """An archived bulletin is classified as the loose file it was."""
        self.assertEqual(goesview.loose_twin(f"z/{RUN}/EMWIN-archive/2026-11-17.zip/A.TXT"), f"s/{RUN}/EMWIN/A.TXT")
        self.assertEqual(goesview.loose_twin("z/EMWIN-archive/2026-11-17.zip/A.TXT"), "s/EMWIN/A.TXT")
        self.assertEqual(goesview.loose_twin("z/a/b/EMWIN-archive/2026-11-17.zip/A.TXT"), "s/a/b/EMWIN/A.TXT")


if __name__ == "__main__":
    unittest.main()
