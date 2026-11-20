"""The HTTP layer: goesview.Handler on a real socket.

Each test starts ThreadingHTTPServer on 127.0.0.1 with an ephemeral port, in a
thread, and shuts it down afterwards. Requests are made with http.client so the
path reaches the server exactly as written (no client-side tidying of "..").
"""
import gzip
import http.client
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, HOUR, RUN  # noqa: E402

goesview = fx.goesview

BAND2 = "imagery/GOES-18 · Full Disk/Band 2 · Red visible"
AFD = "text/EMWIN/AFD · Area forecast discussion"
LATIN = "000\r\nFXUS66 KSEW\r\nTemp\xe9rature 12\xb0C near Seattle.\r\n".encode("latin-1")


class Response:
    def __init__(self, raw, body):
        self.status, self.headers, self.body = raw.status, raw.headers, body

    def json(self):
        return json.loads(self.body)


class HttpCase(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        self.build()
        self.app = fx.make_app(self, self.tree)
        self.app.lut_name = "usradioguy"
        self.catalog = self.app.catalog
        self.addCleanup(setattr, goesview.Handler, "app", goesview.Handler.app)
        goesview.Handler.app = self.app
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), goesview.Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def build(self):
        t, now = self.tree, self.now
        self.t1, self.t2 = now - 2 * HOUR, now - HOUR
        self.s1, self.s2 = t.slot(self.t1), t.slot(self.t2, bands=(2,))
        self.b1 = t.bulletin(self.t1 + 300, "AFDSEWWA", text=LATIN)
        self.b2 = t.bulletin(self.t2 + 300, "TORSEWWA", text="Tornado warning.\r\n" + "Take cover now. " * 200)
        self.b3 = t.bulletin(self.t2 + 400, "AFDPDTOR", office="KPDT")
        self.band = f"s/{self.s1}/{fx.band_name(self.t1)}"

    def connect(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        return conn

    def get(self, path, headers=None, method="GET", conn=None):
        conn = conn or self.connect()
        conn.request(method, path, headers=headers or {})
        raw = conn.getresponse()
        return Response(raw, raw.read())

    def api(self, name, **params):
        response = self.get(f"/api/{name}" + ("?" + urllib.parse.urlencode(params) if params else ""))
        self.assertEqual(response.status, 200, response.body[:200])
        self.assertEqual(response.headers["Content-Type"], "application/json")
        return response.json()

    @staticmethod
    def url(vpath):
        return "/f/" + urllib.parse.quote(vpath)


# ------------------------------------------------------------------------ API

class ApiTests(HttpCase):
    def test_types(self):
        body = self.api("types")
        self.assertEqual(set(body), {"now", "types", "lut"})
        self.assertEqual(body["lut"], "usradioguy")
        self.assertEqual(body["types"], self.catalog.type_list())
        self.assertEqual([t["id"] for t in body["types"]][:2],
                         [BAND2, "imagery/GOES-18 · Full Disk/Band 13 · Clean longwave IR"])
        self.assertAlmostEqual(body["now"], time.time(), delta=5)

    def test_items_newest_first(self):
        body = self.api("items")
        self.assertFalse(body["more"])
        self.assertEqual(len(body["items"]), 6)
        self.assertEqual([row["p"] for row in body["items"]], fx.paths(self.catalog.feed(limit=50)[0]))
        self.assertEqual(body["items"][0]["p"], "s/" + self.b3)
        self.assertEqual(set(body["items"][0]), {"id", "p", "k", "ty", "t", "r", "nm"})

    def test_items_paging(self):
        first = self.api("items", limit=4)
        self.assertTrue(first["more"])
        rest = self.api("items", limit=4, before=first["items"][-1]["id"])
        self.assertFalse(rest["more"])
        self.assertEqual([row["p"] for row in first["items"] + rest["items"]], fx.paths(self.catalog.feed(limit=50)[0]))

    def test_items_filters(self):
        self.assertEqual({row["k"] for row in self.api("items", kind="t")["items"]}, {"t"})
        self.assertEqual(len(self.api("items", kind="i")["items"]), 3)
        self.assertEqual(len(self.api("items", sec="text")["items"]), 3)
        self.assertEqual(len(self.api("items", sec="weather")["items"]), 0)
        self.assertEqual([row["p"] for row in self.api("items", type=BAND2)["items"]],
                         [f"s/{self.s2}/{fx.band_name(self.t2)}", self.band])
        self.assertEqual(len(self.api("items", raw="1")["items"]), 3)
        self.assertEqual(len(self.api("items", raw="0")["items"]), 3)   # bulletins are not raw channels
        self.assertEqual(len(self.api("items", group="GOES-18 · Full Disk")["items"]), 3)
        self.assertEqual([row["p"] for row in self.api("items", q="kpdt")["items"]], ["s/" + self.b3])

    def test_items_with_text_bodies(self):
        """The Text page asks for the start of each bulletin along with the list."""
        rows = {row["p"]: row for row in self.api("items", kind="t", body="1")["items"]}
        self.assertEqual(rows["s/" + self.b1]["b"], "000\r\nFXUS66 KSEW\r\nTemp\xe9rature 12\xb0C near Seattle.\r\n".replace("\r", ""))
        self.assertFalse(rows["s/" + self.b1]["more"])
        self.assertTrue(rows["s/" + self.b2]["more"])            # a long one is cut short
        self.assertTrue(rows["s/" + self.b2]["b"].startswith("Tornado warning.\nTake cover now. Take cover"))
        self.assertLessEqual(len(rows["s/" + self.b2]["b"]), 1400)
        self.assertNotIn("b", self.api("items", kind="i", body="1")["items"][0])

    def test_items_limit_is_capped(self):
        for n in range(230):
            self.tree.bulletin(self.t1 + 1000 + n, "RWRSEWWA")
        self.catalog.poll()
        body = self.api("items", limit=100000)
        self.assertEqual(len(body["items"]), 200)
        self.assertTrue(body["more"])

    def test_bad_numbers_are_a_client_error(self):
        for query in ("limit=abc", "before=xyz", "limit=1.5"):
            with self.subTest(query=query):
                self.assertEqual(self.get("/api/items?" + query).status, 400)

    def test_timeline(self):
        body = self.api("timeline", type=BAND2)
        self.assertEqual(body["type"]["id"], BAND2)
        self.assertEqual([frame[:2] for frame in body["frames"]],
                         [[self.t1, self.band], [self.t2, f"s/{self.s2}/{fx.band_name(self.t2)}"]])
        self.assertEqual(self.get("/api/timeline?type=nothing").status, 404)
        self.assertEqual(self.get("/api/timeline").status, 404)

    def test_stats(self):
        body = self.api("stats")
        self.assertEqual(set(body), {"now", "items", "total", "rate", "last", "scans", "storage", "dcs", "health"})
        self.assertEqual(body["items"], 6)
        self.assertEqual(body["total"], {"imagery": 3, "text": 3})
        storage = body["storage"]
        self.assertEqual(set(storage), {"free", "total", "cache", "satdump", "scope", "archive", "dcs", "dry", "last"})
        self.assertGreater(storage["total"], storage["free"])
        # nothing is configured to delete or pack anything, and the page can say so
        self.assertEqual((storage["cache"], storage["satdump"], storage["scope"], storage["archive"], storage["dry"]),
                         (None, None, None, None, False))
        self.assertEqual(storage["dcs"], {"limits": None, "archive": None})
        self.assertIsNone(body["dcs"])
        self.assertIsNone(body["health"])

    def test_stats_show_the_retention_policy(self):
        """The Live page shows free space, both policies and the last sweep of each."""
        Limits = goesview.Limits
        pruner = fx.make_pruner(self.app, cache=Limits(keep=30 * DAY), satdump=Limits(keep=90 * DAY, disk_cap=0.9),
                                dry_run=True)
        storage = self.api("stats")["storage"]
        self.assertIn("30 days", storage["cache"])
        self.assertIn("90 days", storage["satdump"])
        self.assertIn("90%", storage["satdump"])
        self.assertTrue(storage["dry"])
        self.assertEqual(storage["scope"]["runs"], [RUN])
        self.assertEqual(storage["last"], pruner.last)

    def test_unknown_api_names_are_not_found(self):
        for name in ("nothing", "", "types/extra", "monitor", "monitor/history", "dcs/summary", "dcs/platforms", "dcs/nope"):
            with self.subTest(name=name):
                self.assertEqual(self.get("/api/" + name).status, 404)

    def test_api_is_never_cached_and_compresses_large_answers(self):
        plain = self.get("/api/items?kind=t&body=1")
        self.assertEqual(plain.headers["Cache-Control"], "no-store")
        self.assertIsNone(plain.headers["Content-Encoding"])
        packed = self.get("/api/items?kind=t&body=1", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertLess(len(packed.body), len(plain.body))
        self.assertEqual(json.loads(gzip.decompress(packed.body)), plain.json())
        self.assertEqual(int(packed.headers["Content-Length"]), len(packed.body))

    def test_only_reading_is_offered(self):
        """The viewer is read-only: there is nothing to POST, PUT or DELETE."""
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.assertEqual(self.get(self.url(self.band), method=method).status, 501)
        self.assertTrue(self.tree.exists(self.band[2:]))

    def test_several_requests_on_one_connection(self):
        """HTTP/1.1 keep-alive: every answer says exactly how long it is."""
        conn = self.connect()
        first = self.get("/api/types", conn=conn)
        second = self.get(self.url(self.band), conn=conn)
        third = self.get("/api/stats", conn=conn, method="HEAD")
        fourth = self.get("/", conn=conn)
        self.assertEqual([r.status for r in (first, second, third, fourth)], [200] * 4)
        self.assertEqual(second.body, fx.png_bytes())
        self.assertEqual(third.body, b"")
        self.assertIn(b"<title>GOESview</title>", fourth.body)

    def test_text_search_without_an_index_falls_back_to_names(self):
        """--no-text-index: the Text page filters by product and office only."""
        body = self.api("items", q="sew", kind="t")
        self.assertEqual([row["p"] for row in body["items"]], ["s/" + self.b2, "s/" + self.b1])
        self.assertNotIn("index", body)
        self.assertEqual(self.api("items", q="seattle", kind="t")["items"], [])

    @unittest.skipUnless(fx.have_fts5(), "this Python's SQLite has no FTS5")
    def test_text_search_with_the_index(self):
        os.makedirs(self.tree.cache, exist_ok=True)
        index = self.app.text_index = goesview.TextIndex(self.catalog, self.tree.cache)
        db = index.connect()
        self.addCleanup(db.close)
        index.catch_up(db)
        body = self.api("items", q="seattle", kind="t")
        self.assertEqual(set(body), {"items", "more", "cursor", "index"})
        self.assertEqual([row["p"] for row in body["items"]], ["s/" + self.b1])
        row = body["items"][0]
        self.assertEqual(row["hit"], "000 \nFXUS66 KSEW \nTemp\xe9rature 12\xb0C near \x02Seattle\x03.")
        self.assertIn("Seattle", row["b"])
        self.assertFalse(body["more"])
        self.assertEqual(body["index"], {"ready": False, "done": 3, "total": 3})
        self.assertEqual(len(self.api("items", q='"take cover"', kind="t")["items"]), 1)
        self.assertEqual(self.api("items", q='"cover take"', kind="t")["items"], [])


class TelemetryTests(unittest.TestCase):
    def test_flatten_makes_satdumps_nested_stats_one_level(self):
        """--satdump-api: SatDump's own HTTP stats, flattened for the Live page."""
        nested = {"viterbi": {"ber": 0.00123456, "lock": True}, "snr": 12.5, "name": "goes_hrit",
                  "list": [1, 2], "none": None, "deep": {"a": {"b": 7}}}
        self.assertEqual(goesview.flatten(nested), {"viterbi.ber": 0.0012, "viterbi.lock": True, "snr": 12.5,
                                                    "name": "goes_hrit", "deep.a.b": 7})

    def test_flatten_is_bounded(self):
        self.assertEqual(len(goesview.flatten({str(n): n for n in range(500)})), 60)


# --------------------------------------------------------------- static files

class StaticTests(HttpCase):
    def web(self, name):
        with open(os.path.join(goesview.WEB, name), "rb") as f:
            return f.read()

    def test_front_page(self):
        response = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.web("index.html"))
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")   # a new version is picked up at once
        self.assertEqual(self.get("/index.html").body, response.body)

    def test_script_and_stylesheet(self):
        script = self.get("/app.js")
        self.assertEqual((script.status, script.body), (200, self.web("app.js")))
        self.assertRegex(script.headers["Content-Type"], r"^(text|application)/javascript; charset=utf-8$")
        style = self.get("/style.css")
        self.assertEqual((style.status, style.body), (200, self.web("style.css")))
        self.assertEqual(style.headers["Content-Type"], "text/css; charset=utf-8")

    def test_query_string_does_not_matter(self):
        self.assertEqual(self.get("/app.js?v=12").body, self.web("app.js"))

    def test_only_the_web_folder_is_served(self):
        for path in ("/nope.js", "/goesview.py", "/README.md", "/web/app.js", "/web", "/favicon.ico", "/f", "/p", "/api"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)


# ---------------------------------------------------------------- data files

class FileTests(HttpCase):
    def test_image_is_served_as_it_is_on_disk(self):
        response = self.get(self.url(self.band))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, fx.png_bytes())
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(int(response.headers["Content-Length"]), len(response.body))
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        self.assertEqual(response.headers["Cache-Control"], "max-age=86400")
        self.assertTrue(response.headers["Last-Modified"].endswith("GMT"))

    def test_bulletin_is_served_as_latin_1_text(self):
        """Bulletins are not UTF-8; the browser is told so and gets the bytes untouched."""
        response = self.get(self.url("s/" + self.b1))
        self.assertEqual((response.status, response.body), (200, LATIN))
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=iso-8859-1")

    def test_head_gives_headers_without_the_file(self):
        response = self.get(self.url(self.band), method="HEAD")
        self.assertEqual((response.status, response.body), (200, b""))
        self.assertEqual(int(response.headers["Content-Length"]), len(fx.png_bytes()))

    def test_byte_ranges(self):
        data = fx.png_bytes()
        middle = self.get(self.url(self.band), headers={"Range": "bytes=8-23"})
        self.assertEqual((middle.status, middle.body), (206, data[8:24]))
        self.assertEqual(middle.headers["Content-Range"], f"bytes 8-23/{len(data)}")
        tail = self.get(self.url(self.band), headers={"Range": "bytes=-12"})
        self.assertEqual((tail.status, tail.body), (206, data[-12:]))
        open_ended = self.get(self.url(self.band), headers={"Range": "bytes=40-"})
        self.assertEqual((open_ended.status, open_ended.body), (206, data[40:]))
        past_end = self.get(self.url(self.band), headers={"Range": "bytes=10-999999"})
        self.assertEqual((past_end.status, past_end.body), (206, data[10:]))
        beyond = self.get(self.url(self.band), headers={"Range": f"bytes={len(data) + 5}-"})
        self.assertEqual(beyond.status, 416)
        self.assertEqual(beyond.headers["Content-Range"], f"bytes */{len(data)}")

    def test_not_modified(self):
        first = self.get(self.url(self.band))
        again = self.get(self.url(self.band), headers={"If-Modified-Since": first.headers["Last-Modified"]})
        self.assertEqual((again.status, again.body), (304, b""))
        older = self.get(self.url(self.band), headers={"If-Modified-Since": "Mon, 01 Jan 2001 00:00:00 GMT"})
        self.assertEqual((older.status, older.body), (200, fx.png_bytes()))
        garbled = self.get(self.url(self.band), headers={"If-Modified-Since": "yesterday-ish"})
        self.assertEqual(garbled.status, 200)

    def test_missing_files_and_folders_are_not_found(self):
        for vpath in ("s/nothing.png", f"s/{self.s1}", f"s/{RUN}", "s/", "s", "", "d/nothing.jpg", "q/x.png",
                      f"s/{self.s1}/{fx.band_name(self.t1, 9)}"):
            with self.subTest(vpath=vpath):
                self.assertEqual(self.get(self.url(vpath)).status, 404)

    def test_colour_frame_from_the_cache(self):
        """d/ paths are GOESview's own derived files, served from the cache folder."""
        frame = os.path.join(self.catalog.derived_root, *self.s1.split("/"), "False_Color_usradioguy.jpg")
        os.makedirs(os.path.dirname(frame))
        with open(frame, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0 not really a jpeg")
        response = self.get(self.url(f"d/{self.s1}/False_Color_usradioguy.jpg"))
        self.assertEqual((response.status, response.body), (200, b"\xff\xd8\xff\xe0 not really a jpeg"))
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")

    def test_archived_bulletin_is_served_like_a_loose_one(self):
        """README: archived bulletins are "listed, shown, linked ... exactly as before"."""
        old = self.tree.bulletin(self.now - 3 * DAY, "AFDSEWWA", text=LATIN)
        self.catalog.poll()
        item = self.catalog.by_path["s/" + old]
        fx.make_archiver(self.app).sweep()
        self.assertTrue(item.path.startswith("z/"))
        self.assertFalse(self.tree.exists(old))
        response = self.get(self.url(item.path))
        self.assertEqual((response.status, response.body), (200, LATIN))
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=iso-8859-1")
        self.assertEqual(int(response.headers["Content-Length"]), len(LATIN))
        part = self.get(self.url(item.path), headers={"Range": "bytes=5-14"})
        self.assertEqual((part.status, part.body), (206, LATIN[5:15]))
        self.assertEqual(self.get(self.url(item.path), method="HEAD").body, b"")
        # and the feed still lists it, with its text
        rows = {row["id"]: row for row in self.api("items", kind="t", body="1")["items"]}
        self.assertEqual(rows[item.id]["p"], item.path)
        self.assertIn("near Seattle", rows[item.id]["b"])
        self.assertEqual(self.get(self.url(item.path + ".missing")).status, 404)
        self.assertEqual(self.get(self.url("s/" + old)).status, 404)

    def test_preview_requests_that_cannot_be_met(self):
        for path in ("/p/999/" + self.band, "/p/abc/" + self.band, "/p/320/s/nothing.png", "/p/320/", "/p/",
                     "/p/-320/" + self.band, "/p/320/q/x.png"):
            with self.subTest(path=path):
                self.assertEqual(self.get(urllib.parse.quote(path)).status, 404)

    @unittest.skipUnless(fx.HAVE_PIL, "needs Pillow")
    def test_preview_is_a_small_jpeg_kept_in_the_cache(self):
        response = self.get("/p/320/" + urllib.parse.quote(self.band))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "max-age=604800, immutable")
        self.assertEqual(response.body[:3], b"\xff\xd8\xff")
        cached = [os.path.join(base, name) for base, _, names in os.walk(self.app.previews.folder) for name in names]
        self.assertEqual(len(cached), 1)
        with open(cached[0], "rb") as f:
            self.assertEqual(f.read(), response.body)


# ------------------------------------------------------------- path traversal

class TraversalTests(HttpCase):
    SECRET = b"TOP-SECRET-0c1d2e3f"

    def build(self):
        super().build()
        t = self.tree
        self.secret = os.path.join(t.tmp, "secret.txt")              # beside the data folder
        sibling = os.path.join(t.tmp, "satdump-private")             # shares the data folder's name as a prefix
        os.makedirs(sibling)
        for path in (self.secret, os.path.join(sibling, "secret.txt"), os.path.join(sibling, "secret.png")):
            with open(path, "wb") as f:
                f.write(self.SECRET)
        os.symlink(self.secret, t.path(f"{RUN}/EMWIN/link.txt"))     # links out of the data folder
        os.symlink(self.secret, t.path(f"{RUN}/IMAGES/link.png"))
        os.symlink(t.tmp, t.path("up"))
        t.write("notes.txt", b"inside the data folder")

    def assertRefused(self, path):
        response = self.get(path)
        self.assertIn(response.status, (400, 404), f"{path} -> {response.status}")
        self.assertNotIn(self.SECRET, response.body, path)
        self.assertNotIn(b"root:", response.body, path)

    def test_the_fixture_is_reachable_the_honest_way(self):
        """(So that a refusal below means something.)"""
        self.assertEqual(self.get("/f/s/notes.txt").body, b"inside the data folder")
        self.assertEqual(self.get("/f/s/" + urllib.parse.quote(RUN) + "/../notes.txt").body, b"inside the data folder")

    def test_dot_dot_cannot_leave_the_data_folder(self):
        run, tmp = urllib.parse.quote(RUN), urllib.parse.quote(os.path.basename(self.tree.tmp))
        for path in ("/f/s/../secret.txt",
                     "/f/s/../../" + tmp + "/secret.txt",
                     f"/f/s/{run}/../../secret.txt",
                     f"/f/s/{run}/EMWIN/../../../secret.txt",
                     "/f/s/./../secret.txt",
                     "/f/s/x/../../secret.txt",
                     "/f/s/../satdump-private/secret.txt",
                     "/f/s/../satdump/../secret.txt",
                     "/f/s/../../../../../../../../../../etc/passwd",
                     "/f/s/..",
                     "/f/s/../",
                     "/f/../secret.txt",
                     "/f/s/....//secret.txt",
                     "/f/s/..\\secret.txt",
                     "/f/s/..;/secret.txt"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_encoded_dot_dot_is_no_different(self):
        for path in ("/f/s/%2e%2e/secret.txt",
                     "/f/s/%2E%2E/secret.txt",
                     "/f/s/..%2fsecret.txt",
                     "/f/s/..%2Fsecret.txt",
                     "/f/s/%2e%2e%2fsecret.txt",
                     "/f/s%2f..%2fsecret.txt",
                     "/f%2fs%2f..%2fsecret.txt",
                     "/f/s/%252e%252e/secret.txt",
                     "/f/s/..%252fsecret.txt",
                     "/f/s/%c0%ae%c0%ae/secret.txt",
                     "/f/s/..%5csecret.txt",
                     "/f/s/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
                     "/f/s/../secret.txt%00.png",
                     "/f/s/notes.txt%00/../../secret.txt"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_absolute_paths_are_not_honoured(self):
        absolute = urllib.parse.quote(self.secret)
        for path in ("/f/s/" + absolute,                      # /f/s//tmp/.../secret.txt
                     "/f/s" + absolute,
                     "/f/" + absolute,
                     "/f/s/%2F" + absolute.lstrip("/"),
                     "/f/s//etc/passwd",
                     "/f/s/%2Fetc%2Fpasswd",
                     "/f//etc/passwd",
                     "/f/d//etc/passwd",
                     "//etc/passwd",
                     "/f/s/file:///etc/passwd",
                     "/etc/passwd"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_links_out_of_the_data_folder_are_not_followed(self):
        run = urllib.parse.quote(RUN)
        for path in (f"/f/s/{run}/EMWIN/link.txt", f"/f/s/{run}/IMAGES/link.png", "/f/s/up/secret.txt",
                     "/f/s/up/satdump-private/secret.txt", f"/p/320/s/{run}/IMAGES/link.png", "/p/320/s/up/satdump-private/secret.png"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_cache_prefix_cannot_reach_the_data_or_beyond(self):
        for path in ("/f/d/../../secret.txt", "/f/d/../../satdump/notes.txt", "/f/d/../previews/x", "/f/d/..",
                     "/f/d/%2e%2e/%2e%2e/secret.txt"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_archive_and_preview_routes_are_held_to_the_same_rule(self):
        for path in ("/f/z/../secret.txt",
                     "/f/z/../satdump-private/EMWIN-archive/2026-01-01.zip/secret.txt",
                     "/f/z/" + urllib.parse.quote(self.secret),
                     "/p/320/s/../secret.txt",
                     "/p/320/s/../satdump-private/secret.png",
                     "/p/320/s/%2e%2e/satdump-private/secret.png",
                     "/p/320/" + urllib.parse.quote(self.secret),
                     "/p/320/d/../../satdump-private/secret.png",
                     "/p/../f/s/../secret.txt"):
            with self.subTest(path=path):
                self.assertRefused(path)

    def test_static_route_serves_nothing_outside_the_web_folder(self):
        for path in ("/../goesview.py", "/%2e%2e/goesview.py", "/..%2fgoesview.py", "/..%2f..%2fetc%2fpasswd",
                     "/web/../goesview.py", "/./../goesview.py", "/..", "/.", "/%2e%2e", "/..%5cgoesview.py",
                     "/" + urllib.parse.quote(self.secret), "/%2F" + urllib.parse.quote(self.secret).lstrip("/"),
                     "/app.js/../../goesview.py", "/app.js%00.py", "/../README.md"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertIn(response.status, (400, 404), path)
                self.assertNotIn(self.SECRET, response.body)
                self.assertNotIn(b"def main(", response.body)

    def test_server_survives_all_that(self):
        self.test_encoded_dot_dot_is_no_different()
        self.assertEqual(self.get("/api/types").status, 200)


# ------------------------------------------------------------- live event stream

class EventStreamTests(HttpCase):
    def open_stream(self, headers=None):
        conn = self.connect()
        conn.request("GET", "/api/events", headers=headers or {})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        return response

    def next_event(self, stream):
        """One server-sent event as (name, data, id), skipping comments and the retry hint."""
        name = data = event_id = None
        while True:
            line = stream.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line.startswith("id: "):
                event_id = int(line[4:])
            elif line == "" and name:
                return name, data, event_id

    def wait_for_subscriber(self, count=1):
        deadline = time.time() + 5
        while len(self.catalog.subscribers) < count and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(self.catalog.subscribers), count)

    def test_stream_opens_with_stats_then_pushes_new_arrivals(self):
        """New images, weather products and bulletins appear as they are received."""
        stream = self.open_stream()
        self.assertEqual(stream.readline(), b"retry: 3000\n")
        name, data, _ = self.next_event(stream)
        self.assertEqual(name, "stats")
        self.assertEqual(data["items"], 6)
        self.wait_for_subscriber()
        new = self.tree.bulletin(self.now - 120, "TORSEWWA", text="Tornado warning.\r\n")
        self.catalog.poll()
        name, data, event_id = self.next_event(stream)
        self.assertEqual(name, "item")
        self.assertEqual(data["p"], "s/" + new)
        self.assertEqual(event_id, data["id"])

    def test_reconnecting_tab_is_sent_what_it_missed(self):
        self.catalog.poll()
        first = self.tree.bulletin(self.now - 200, "TORSEWWA")
        self.catalog.poll()
        second = self.tree.slot(self.now - 150, bands=(13,))
        self.catalog.poll()
        seen = self.catalog.by_path["s/" + first].id
        stream = self.open_stream({"Last-Event-ID": str(seen)})
        name, data, event_id = self.next_event(stream)
        self.assertEqual((name, data["p"]), ("item", f"s/{second}/{fx.band_name(self.now - 150, 13)}"))
        self.assertEqual(self.next_event(stream)[0], "stats")


if __name__ == "__main__":
    unittest.main()
