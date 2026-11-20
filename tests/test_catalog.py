"""Catalog: the in-memory list of everything received, and the queries the pages make.

scan() and poll() are called directly (App.watch, which loops over poll(), is
never started). The tree is a fake SatDump run with a few scans, a Level 2
product, an NWS chart, bulletins and an EMWIN graphic.
"""
import os
import queue
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, HOUR, RUN  # noqa: E402

goesview = fx.goesview

FD = "imagery/GOES-18 · Full Disk/"
BAND2, BAND13, COLOUR = FD + "Band 2 · Red visible", FD + "Band 13 · Clean longwave IR", FD + "ABI False Color"
MESO_BAND2 = "imagery/GOES-18 · Mesoscale 1/Band 2 · Red visible"
AFD, TOR = "text/EMWIN/AFD · Area forecast discussion", "text/EMWIN/TOR · Tornado warning"
ACHA = "weather/GOES-18 · Full Disk · Level 2/ACHA · Cloud top height (raw)"
CHART, GRAPHIC = "weather/NWS charts/pac_sfc_analysis", "weather/EMWIN graphics/RADNTHES"


class CatalogCase(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t, now = self.tree, self.now
        self.t1, self.t2, self.t3 = now - 3 * HOUR, now - 2 * HOUR, now - HOUR // 2
        self.s1 = t.slot(self.t1)
        self.s2 = t.slot(self.t2, extra=("abi_rgb_ABI_False_Color.png", "abi_rgb_ABI_False_Color_map.png"))
        self.s3 = t.slot(self.t3, bands=(2,))
        self.meso = t.slot(self.t2 + 60, group=fx.MESO, bands=(2,))
        self.l2 = t.slot(self.t2 + 120, group="L2/GOES-18/Full Disk", bands=("ACHA",))
        self.chart = t.chart(self.t2 + 180)
        self.b1 = t.bulletin(self.t1 + 300, "AFDSEWWA")
        self.b2 = t.bulletin(self.t2 + 300, "TORSEWWA")
        self.b3 = t.bulletin(self.t3 + 300, "AFDPDTOR", office="KPDT")
        self.gif = t.bulletin(self.t2 + 400, "RADNTHES", ext="GIF", text=b"GIF89a")
        # things the viewer has no business listing
        t.write(f"{self.s1}/product.cbor", b"\xa0")
        t.write(f"{RUN}/dataset.json", b"{}")
        t.write(f"{RUN}/DCS/DCS_1.json", b"[]")
        t.write("notes.txt")
        t.png(f"{RUN}/IMAGES/.thumbnails/{fx.slot_name(self.t1)}/{fx.band_name(self.t1)}")

    def scan(self, **options):
        self.app = fx.make_app(self, self.tree, **options)
        self.catalog = self.app.catalog
        return self.catalog

    def band(self, slot, ts, band=2):
        return f"s/{slot}/{fx.band_name(ts, band)}"


class ScanTests(CatalogCase):
    def test_scan_finds_every_product_and_nothing_else(self):
        catalog = self.scan()
        self.assertEqual(len(catalog.items), 13)
        self.assertEqual(set(catalog.types), {BAND2, BAND13, COLOUR, MESO_BAND2, AFD, TOR, ACHA, CHART, GRAPHIC})
        self.assertEqual({len(catalog.types[t].items) for t in (BAND2,)}, {3})
        listed = {item.path for item in catalog.items}
        self.assertIn(self.band(self.s1, self.t1), listed)
        self.assertIn("s/" + self.b1, listed)
        self.assertFalse(any("product.cbor" in p or "dataset" in p or "DCS" in p or "notes" in p or ".thumbnails" in p
                             for p in listed))

    def test_scan_does_not_write_inside_the_satdump_folder(self):
        """README: "It never writes inside the SatDump folder." """
        before = fx.snapshot(self.tree.root)
        catalog = self.scan()
        catalog.poll()
        catalog.stats()
        self.assertEqual(fx.snapshot(self.tree.root), before)

    def test_item_fields(self):
        catalog = self.scan()
        image = catalog.by_path[self.band(self.s1, self.t1)]
        self.assertEqual((image.kind, image.type.id, image.time, image.name), ("image", BAND2, self.t1, None))
        self.assertEqual(image.recv, self.t1 + 30)                       # when the file landed
        self.assertEqual(image.size, len(fx.png_bytes()))
        text = catalog.by_path["s/" + self.b1]
        self.assertEqual((text.kind, text.type.id, text.time), ("text", AFD, self.t1 + 300))
        self.assertEqual(text.name, "AFDSEWWA · FXUS66 KSEW")
        row = image.as_dict()
        self.assertEqual(row, {"id": image.id, "p": image.path, "k": "i", "ty": BAND2, "t": self.t1,
                               "r": self.t1 + 30, "s": image.size})
        self.assertEqual(text.as_dict()["nm"], "AFDSEWWA · FXUS66 KSEW")
        self.assertEqual(text.as_dict()["k"], "t")

    def test_map_overlay_rides_on_its_base_image(self):
        """The _map twin is not an item of its own: the composite carries it."""
        catalog = self.scan()
        base = f"s/{self.s2}/abi_rgb_ABI_False_Color.png"
        overlay = f"s/{self.s2}/abi_rgb_ABI_False_Color_map.png"
        self.assertEqual(catalog.by_path[base].map, overlay)
        self.assertEqual(catalog.by_path[base].as_dict()["m"], overlay)
        self.assertEqual(len(catalog.types[COLOUR].items), 1)
        self.assertNotIn(overlay, fx.paths(catalog.items))

    def test_map_overlay_that_landed_before_its_base_image(self):
        slot = self.tree.slot(self.t3 + 600, bands=())
        self.tree.png(f"{slot}/abi_rgb_ABI_False_Color_map.png", mtime=self.t3 + 610)
        self.tree.png(f"{slot}/abi_rgb_ABI_False_Color.png", mtime=self.t3 + 640)
        catalog = self.scan()
        self.assertEqual(catalog.by_path[f"s/{slot}/abi_rgb_ABI_False_Color.png"].map,
                         f"s/{slot}/abi_rgb_ABI_False_Color_map.png")
        self.assertEqual(catalog.pending_maps, {})

    def test_every_run_under_the_root_is_picked_up(self):
        """README: "SatDump starts a new run folder each time it restarts, and goesview
        picks up every run underneath." Same types, whichever run a file sits in."""
        earlier = self.tree.slot(self.t1 - DAY, run=fx.OLD_RUN)
        catalog = self.scan()
        item = catalog.by_path[f"s/{earlier}/{fx.band_name(self.t1 - DAY)}"]
        self.assertEqual(item.type.id, BAND2)
        self.assertEqual(len(catalog.types[BAND2].items), 4)

    def test_cache_inside_the_data_folder_is_not_listed_as_data(self):
        """Colour frames GOESview built mirror the IMAGES/ paths; with --cache under
        the root they must not come back as if SatDump had received them."""
        t = self.tree
        t.cache = t.path("goesview-cache")
        t.png(f"goesview-cache/derived/{self.s1}/False_Color_usradioguy.jpg")
        catalog = self.scan()
        self.assertEqual(len(catalog.items), 13)
        self.assertFalse(any("goesview-cache" in item.path for item in catalog.items))

    def test_file_still_being_written_waits(self):
        """A file modified in the last few seconds is not listed until it has settled."""
        rel = self.tree.slot(self.now - 300, bands=())
        fresh = self.tree.png(f"{rel}/{fx.band_name(self.now - 300)}", mtime=time.time())
        catalog = self.scan(settle=60.0)       # (the default is 4 s; a minute keeps a slow machine from mattering)
        self.assertNotIn("s/" + fresh, catalog.by_path)
        self.assertEqual(len(catalog.items), 13)
        catalog.poll()
        self.assertNotIn("s/" + fresh, catalog.by_path)
        settled = time.time() - 120
        os.utime(self.tree.path(fresh), (settled, settled))
        catalog.poll()
        self.assertIn("s/" + fresh, catalog.by_path)

    def test_resolve_maps_virtual_paths_to_files_inside_their_root_only(self):
        catalog = self.scan()
        t = self.tree
        self.assertEqual(catalog.resolve("s/" + self.b1), t.path(self.b1))
        self.assertEqual(catalog.resolve("d/a/b.jpg"), os.path.join(catalog.derived_root, "a", "b.jpg"))
        secret = os.path.join(t.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("secret")
        os.symlink(secret, t.path(f"{RUN}/link.txt"))
        os.symlink(t.tmp, t.path(f"{RUN}/up"))
        for vpath in ("s/../secret.txt", "s/" + RUN + "/../../secret.txt", "s//etc/passwd", "s/" + secret,
                      "d/../../secret.txt", "d/../satdump/notes.txt", "s/", "s", "s/.", "", "x/notes.txt", "/etc/passwd",
                      f"s/{RUN}/link.txt", f"s/{RUN}/up/secret.txt", "s/..", "d/.."):
            with self.subTest(vpath=vpath):
                self.assertIsNone(catalog.resolve(vpath))
        # a ".." that stays inside the root is just a path
        self.assertEqual(catalog.resolve(f"s/{RUN}/EMWIN/../dataset.json"), t.path(f"{RUN}/dataset.json"))


class FeedTests(CatalogCase):
    def test_feed_is_newest_arrival_first(self):
        """The "everything in the order it arrived" view."""
        catalog = self.scan()
        items, more = catalog.feed(limit=100)
        self.assertFalse(more)
        self.assertEqual(len(items), 13)
        arrivals = [(item.recv, item.id) for item in items]
        self.assertEqual(arrivals, sorted(arrivals, reverse=True))
        self.assertEqual(items[0].path, "s/" + self.b3)
        self.assertEqual({items[-1].path, items[-2].path},
                         {self.band(self.s1, self.t1), self.band(self.s1, self.t1, 13)})

    def test_feed_orders_by_arrival_not_by_data_time(self):
        """An old scan that only arrives now is at the top of the feed but at the
        start of its timeline."""
        late = self.tree.slot(self.t1 - HOUR, bands=(2,), mtime=time.time() - 40)
        catalog = self.scan()
        vpath = self.band(late, self.t1 - HOUR)
        self.assertEqual(catalog.feed(limit=1)[0][0].path, vpath)
        frames = catalog.timeline(BAND2)["frames"]
        self.assertEqual(frames[0][:2], [self.t1 - HOUR, vpath])
        self.assertEqual([f[0] for f in frames], sorted(f[0] for f in frames))

    def test_paging_with_before_walks_the_whole_feed_once(self):
        catalog = self.scan()
        everything = fx.paths(catalog.feed(limit=100)[0])
        seen, before, pages = [], None, 0
        while True:
            items, more = catalog.feed(before=before, limit=4)
            seen += fx.paths(items)
            pages += 1
            if not more:
                break
            self.assertEqual(len(items), 4)
            before = items[-1].id
        self.assertEqual(seen, everything)
        self.assertEqual(pages, 4)  # 4 + 4 + 4 + 1

    def test_more_flag_is_exact_at_the_page_boundary(self):
        catalog = self.scan()
        self.assertEqual([len(catalog.feed(limit=n)[0]) for n in (12, 13, 14)], [12, 13, 13])
        self.assertEqual([catalog.feed(limit=n)[1] for n in (12, 13, 14)], [True, False, False])

    def test_section_filter(self):
        catalog = self.scan()
        counts = {sec: len(catalog.feed(section=sec, limit=100)[0]) for sec in ("imagery", "weather", "text", "nothing")}
        self.assertEqual(counts, {"imagery": 7, "weather": 3, "text": 3, "nothing": 0})

    def test_type_filter(self):
        catalog = self.scan()
        items, more = catalog.feed(type_id=BAND2, limit=100)
        self.assertEqual(fx.paths(items), [self.band(self.s3, self.t3), self.band(self.s2, self.t2), self.band(self.s1, self.t1)])
        self.assertEqual(catalog.feed(type_id="imagery/nothing/like this", limit=100), ([], False))
        # paging inside one type
        first, more = catalog.feed(type_id=BAND2, limit=2)
        self.assertTrue(more)
        rest, more = catalog.feed(type_id=BAND2, limit=2, before=first[-1].id)
        self.assertEqual((fx.paths(rest), more), ([self.band(self.s1, self.t1)], False))

    def test_kind_filter(self):
        catalog = self.scan()
        self.assertEqual(len(catalog.feed(kind="text", limit=100)[0]), 3)
        self.assertEqual(len(catalog.feed(kind="image", limit=100)[0]), 10)
        self.assertEqual(len(catalog.feed(section="weather", kind="text", limit=100)[0]), 0)

    def test_raw_filter_separates_channels_from_composites(self):
        """README: "Processed composites and raw channels can be filtered." """
        catalog = self.scan()
        raw = catalog.feed(section="imagery", raw=True, limit=100)[0]
        processed = catalog.feed(section="imagery", raw=False, limit=100)[0]
        self.assertEqual(len(raw), 6)
        self.assertEqual(fx.paths(processed), [f"s/{self.s2}/abi_rgb_ABI_False_Color.png"])
        self.assertTrue(all(item.type.raw for item in raw))

    def test_group_filter(self):
        catalog = self.scan()
        self.assertEqual(fx.paths(catalog.feed(group="GOES-18 · Mesoscale 1", limit=100)[0]),
                         [self.band(self.meso, self.t2 + 60)])
        self.assertEqual(len(catalog.feed(group="GOES-18 · Full Disk", limit=100)[0]), 6)
        self.assertEqual(len(catalog.feed(group="EMWIN", limit=100)[0]), 3)

    def test_query_matches_names_without_regard_to_case(self):
        """The quick filter: product and office codes for bulletins, the type label for the rest."""
        catalog = self.scan()
        self.assertEqual(fx.paths(catalog.feed(query="sew", kind="text", limit=100)[0]), ["s/" + self.b2, "s/" + self.b1])
        self.assertEqual(fx.paths(catalog.feed(query="KPDT", limit=100)[0]), ["s/" + self.b3])
        self.assertEqual(len(catalog.feed(query="band 13", limit=100)[0]), 2)
        self.assertEqual(catalog.feed(query="no such thing", limit=100), ([], False))


class QueryTests(CatalogCase):
    def test_type_list_shape_and_order(self):
        """Types come grouped for the pages: by section, satellite products before
        EMWIN's graphics, composites before channels, channels by number."""
        catalog = self.scan()
        types = catalog.type_list()
        self.assertEqual([t["id"] for t in types], [COLOUR, BAND2, BAND13, MESO_BAND2, AFD, TOR, ACHA, CHART, GRAPHIC])
        band2 = types[1]
        self.assertEqual({k: band2[k] for k in ("sec", "grp", "label", "kind", "raw", "n")},
                         {"sec": "imagery", "grp": "GOES-18 · Full Disk", "label": "Band 2 · Red visible",
                          "kind": "i", "raw": 1, "n": 3})
        self.assertEqual(band2["last"]["p"], self.band(self.s3, self.t3))   # the latest by data time
        self.assertEqual(types[0]["raw"], 0)
        self.assertEqual(types[4]["kind"], "t")

    def test_timeline_is_in_data_time_order(self):
        catalog = self.scan()
        timeline = catalog.timeline(BAND2)
        self.assertEqual(timeline["type"]["id"], BAND2)
        size = len(fx.png_bytes())
        self.assertEqual(timeline["frames"], [[self.t1, self.band(self.s1, self.t1), 0, 0, size],
                                              [self.t2, self.band(self.s2, self.t2), 0, 0, size],
                                              [self.t3, self.band(self.s3, self.t3), 0, 0, size]])
        overlay = f"s/{self.s2}/abi_rgb_ABI_False_Color_map.png"
        self.assertEqual(catalog.timeline(COLOUR)["frames"][0][2], overlay)
        self.assertIsNone(catalog.timeline("imagery/none/none"))

    def test_stats_shape(self):
        self.tree.slot(self.t3 - 2 * DAY)     # a scan from before the day shown under "scans"
        catalog = self.scan()
        stats = catalog.stats()
        self.assertEqual(set(stats), {"now", "items", "total", "rate", "last", "scans"})
        self.assertAlmostEqual(stats["now"], time.time(), delta=5)
        self.assertEqual(stats["items"], 15)
        self.assertEqual(stats["total"], {"imagery": 9, "weather": 3, "text": 3})
        # the newest arrival in each section
        self.assertEqual(stats["last"], {"imagery": self.t3 + 30, "weather": self.t2 + 400, "text": self.t3 + 300})
        # arrivals per minute over the last hour: one image and one bulletin, nothing in Weather
        self.assertEqual(set(stats["rate"]), {"imagery", "text"})
        for section in ("imagery", "text"):
            self.assertEqual(len(stats["rate"][section]), 60)
            self.assertEqual(sum(stats["rate"][section]), 1)
        self.assertEqual(sum(stats["rate"]["imagery"][28:32]), 1)   # about half an hour ago
        # which bands each full-disk scan of the last day has
        self.assertEqual(stats["scans"], [{"g": "GOES-18 · Full Disk", "slots": [
            {"t": self.t1, "b": [2, 13]}, {"t": self.t2, "b": [2, 13]}, {"t": self.t3, "b": [2]}]}])

    def test_slot_snapshot_lists_the_files_of_each_scan(self):
        catalog = self.scan()
        groups = dict(catalog.slot_snapshot())
        self.assertEqual(set(groups), {"GOES-18 · Full Disk", "GOES-18 · Mesoscale 1"})
        folder, files = groups["GOES-18 · Full Disk"][fx.slot_name(self.t1)]
        self.assertEqual(folder, self.s1)
        self.assertEqual(files, {fx.band_name(self.t1, 2), fx.band_name(self.t1, 13)})


class LiveTests(CatalogCase):
    def test_poll_picks_up_a_new_scan_and_tells_open_pages(self):
        """New images appear as they are received."""
        catalog = self.scan()
        events = catalog.subscribe()
        count = len(catalog.items)
        new = self.tree.slot(self.now - 300, bands=(2,))
        catalog.poll()
        vpath = self.band(new, self.now - 300)
        item = catalog.by_path[vpath]
        self.assertEqual(len(catalog.items), count + 1)
        self.assertEqual(catalog.feed(limit=1)[0][0], item)
        event, data, event_id = events.get_nowait()
        self.assertEqual((event, data, event_id), ("item", item.as_dict(), item.id))
        self.assertTrue(events.empty())
        self.assertEqual(catalog.since(item.id - 1), [item.as_dict()])
        self.assertEqual(catalog.since(item.id), [])

    def test_poll_picks_up_a_new_bulletin_with_its_arrival_time_and_size(self):
        catalog = self.scan()
        data = b"URGENT\r\n"
        landed = time.time() - 20
        new = self.tree.bulletin(self.now - 400, "TORSEWWA", text=data, mtime=landed)
        catalog.poll()
        item = catalog.by_path["s/" + new]
        self.assertEqual(item.type.id, TOR)
        self.assertEqual(item.time, self.now - 400)       # issued
        self.assertAlmostEqual(item.recv, landed, places=3)  # received
        self.assertEqual(item.size, len(data))
        self.assertEqual(catalog.feed(limit=1)[0][0], item)

    def test_poll_picks_up_a_new_run_folder(self):
        """SatDump restarted: a new run folder appears beside the old one."""
        catalog = self.scan()
        new_run = "2026-11-19_23-40_goes_hrit_1.6941 GHz"
        new = self.tree.slot(self.now - 200, run=new_run, bands=(13,))
        catalog.poll()
        self.assertEqual(catalog.by_path[self.band(new, self.now - 200, 13)].type.id, BAND13)

    def test_poll_with_nothing_new_changes_nothing(self):
        catalog = self.scan()
        events = catalog.subscribe()
        for _ in range(45):
            catalog.poll()
        self.assertEqual(len(catalog.items), 13)
        self.assertTrue(events.empty())

    def test_a_late_file_in_an_old_scan_folder_is_found_eventually(self):
        """Old timestamp folders are only looked at now and then, but they are looked at."""
        self.scan(scan=False)
        self.tree.age_dirs(3 * HOUR)
        self.catalog.scan()
        late = self.tree.png(f"{self.s1}/{fx.band_name(self.t1, 7)}")
        for _ in range(40):
            self.catalog.poll()
        self.assertIn("s/" + late, self.catalog.by_path)

    def test_stalled_subscriber_is_dropped_not_waited_for(self):
        catalog = self.scan()
        stalled, healthy = catalog.subscribe(), catalog.subscribe()
        for n in range(1000):
            stalled.put_nowait(("filler", n, None))
        catalog.publish("stats", {"x": 1})
        self.assertNotIn(stalled, catalog.subscribers)
        self.assertIn(healthy, catalog.subscribers)
        self.assertEqual(healthy.get_nowait(), ("stats", {"x": 1}, None))
        catalog.unsubscribe(healthy)
        catalog.publish("stats", {"x": 2})
        with self.assertRaises(queue.Empty):
            healthy.get_nowait()

    def test_add_derived_slots_a_late_colour_frame_in_by_arrival(self):
        """A frame built now for an old scan sits beside that scan's bands in the feed."""
        catalog = self.scan()
        spec = goesview.Spec("imagery", "GOES-18 · Full Disk", "False Color · test", "image", 0.0, self.t1)
        vpath = f"d/{self.s1}/False_Color_test.jpg"
        item = catalog.add_derived(vpath, spec, self.t1, self.t1 + 31, 1234, "band 2 from the 20:00 scan", live=False)
        self.assertEqual(item.as_dict()["n"], "band 2 from the 20:00 scan")
        order = fx.paths(catalog.feed(limit=100)[0])
        self.assertEqual(order.index(vpath), len(order) - 3)   # just after (newer than) the two bands of that scan
        self.assertIsNone(catalog.add_derived(vpath, spec, self.t1, self.t1 + 31, 1234, None, live=False))  # once only
        self.assertEqual(catalog.type_list()[0]["id"], FD + "False Color · test")  # rank 0: ahead of SatDump's own


class ForgetTests(CatalogCase):
    def test_forget_a_scan_folder(self):
        catalog = self.scan()
        gone = [self.band(self.s1, self.t1), self.band(self.s1, self.t1, 13)]
        ids = [catalog.by_path[v].id for v in gone]
        self.assertEqual(catalog.forget({self.s1}, set()), 2)
        self.assertEqual(len(catalog.items), 11)
        for vpath, item_id in zip(gone, ids):
            self.assertNotIn(vpath, catalog.by_path)
            self.assertNotIn(item_id, catalog.ids)
        self.assertNotIn(vpath, fx.paths(catalog.feed(limit=100)[0]))
        self.assertEqual(len(catalog.types[BAND2].items), 2)
        self.assertEqual(len(catalog.types[BAND2].arrivals), 2)
        self.assertNotIn(fx.slot_name(self.t1), dict(catalog.slot_snapshot())["GOES-18 · Full Disk"])
        self.assertEqual([f[0] for f in catalog.timeline(BAND2)["frames"]], [self.t2, self.t3])

    def test_forget_single_files(self):
        catalog = self.scan()
        self.assertEqual(catalog.forget(set(), {"s/" + self.b1, "s/" + self.chart}), 2)
        self.assertNotIn("s/" + self.b1, catalog.by_path)
        self.assertEqual(fx.paths(catalog.feed(type_id=AFD, limit=10)[0]), ["s/" + self.b3])
        self.assertNotIn(CHART, catalog.types, "a type with nothing left in it is dropped")
        self.assertNotIn(CHART, [t["id"] for t in catalog.type_list()])

    def test_forget_takes_the_map_overlay_too(self):
        catalog = self.scan()
        catalog.forget({self.s2}, set())
        self.assertFalse(any(self.s2 in vpath for vpath in catalog.by_path))
        self.assertNotIn(COLOUR, catalog.types)

    def test_forget_nothing(self):
        catalog = self.scan()
        self.assertEqual(catalog.forget(set(), set()), 0)
        self.assertEqual(catalog.forget({"no/such/folder"}, {"s/no/such/file.png"}), 0)
        self.assertEqual(len(catalog.items), 13)

    def test_forgotten_items_drop_out_of_the_replay_buffer(self):
        catalog = self.scan()
        new = self.tree.slot(self.now - 300, bands=(2,))
        catalog.poll()
        self.assertEqual(len(catalog.since(0)), 1)
        catalog.forget({new}, set())
        self.assertEqual(catalog.since(0), [])

    def test_names_written_again_after_being_forgotten_are_noticed(self):
        """If SatDump writes a deleted folder or file again, it is listed again."""
        catalog = self.scan()
        vpath = self.band(self.s3, self.t3)
        shutil.rmtree(self.tree.path(self.s3))
        os.unlink(self.tree.path(self.b3))
        catalog.forget({self.s3}, {"s/" + self.b3})
        self.tree.age_dirs()
        catalog.poll()
        self.assertNotIn(vpath, catalog.by_path)
        self.assertEqual(len(catalog.items), 11)
        self.tree.slot(self.t3, bands=(2,))
        self.tree.write(self.b3, b"again\n", mtime=time.time() - 30)
        catalog.poll()
        self.assertIn(vpath, catalog.by_path)
        self.assertIn("s/" + self.b3, catalog.by_path)
        self.assertEqual(len(catalog.items), 13)


if __name__ == "__main__":
    unittest.main()
