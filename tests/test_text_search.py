"""Full-text search over the bulletins: parse_query, passage, and TextIndex.

README: "every word must appear, and a phrase in quotes must match exactly."

TextIndex is a thread, but its work is done by methods that take a database
connection (catch_up, drain, rebuild), so these tests open a connection with
index.connect() and call those directly; the thread's own loop is never run.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gv_fixtures as fx  # noqa: E402
from gv_fixtures import DAY, HOUR  # noqa: E402

goesview = fx.goesview
B, E = "\x02", "\x03"  # how passage() marks the start and end of a match


class ParseQueryTests(unittest.TestCase):
    def test_bare_words_match_from_the_start_of_a_word(self):
        self.assertEqual(goesview.parse_query("flood warning"),
                         ('"flood"* "warning"*', ["flood", "warning"], []))

    def test_quoted_phrase(self):
        """A phrase narrows by its words in the index, and is kept to be checked against the text."""
        self.assertEqual(goesview.parse_query('"flash flood" seattle'),
                         ('"flash" "flood" "seattle"*', ["seattle"], [["flash", "flood"]]))
        self.assertEqual(goesview.parse_query('king "red flag warning" "high wind"'),
                         ('"king"* "red" "flag" "warning" "high" "wind"',
                          ["king"], [["red", "flag", "warning"], ["high", "wind"]]))

    def test_punctuation_separates_words(self):
        match, words, phrases = goesview.parse_query("wind/gust 60-mph (KSEW)")
        self.assertEqual(words, ["wind", "gust", "60", "mph", "KSEW"])
        self.assertEqual(match, '"wind"* "gust"* "60"* "mph"* "KSEW"*')

    def test_nothing_to_search_for(self):
        for text in ("", "   ", '""', '"..."', "?!", '" "'):
            with self.subTest(text=text):
                self.assertEqual(goesview.parse_query(text), ("", [], []))

    def test_search_syntax_is_never_passed_through(self):
        """Whatever is typed, every term reaches SQLite quoted: no operators, no column
        filters, no stray quotes that could make the query a syntax error."""
        for text in ('flood" OR "wind', "NOT flood", "a AND b OR c", "name:afd", "flood*", "NEAR(a b)", '"unclosed phrase',
                     "^flood", "a + b", "{a b}", "-flood", "x'y"):
            with self.subTest(text=text):
                match, words, phrases = goesview.parse_query(text)
                for term in match.split(" "):
                    self.assertRegex(term, r'^"[0-9A-Za-z]+"\*?$')
        self.assertEqual(goesview.parse_query("NOT flood")[1], ["NOT", "flood"])


class PassageTests(unittest.TestCase):
    def test_marks_the_match(self):
        self.assertEqual(goesview.passage("Heavy rain near Seattle tonight.", ["seattle"], []),
                         f"Heavy rain near {B}Seattle{E} tonight.")

    def test_word_matches_from_the_start_of_a_word_only(self):
        text = "Flooding upstream; the preflood survey; a flood."
        self.assertEqual(goesview.passage(text, ["flood"], []),
                         f"{B}Flooding{E} upstream; the preflood survey; a {B}flood{E}.")

    def test_every_search_word_is_marked(self):
        marked = goesview.passage("High wind warning. Gusts to 60 mph.", ["gust", "wind"], [])
        self.assertEqual(marked, f"High {B}wind{E} warning. {B}Gusts{E} to 60 mph.")

    def test_phrase_must_be_in_the_text_exactly(self):
        """None means "not a hit after all": the index only knew the words were all there."""
        phrase = [["flash", "flood"]]
        self.assertIsNone(goesview.passage("Flash flooding is possible. A flood watch follows.", [], phrase))
        self.assertIsNone(goesview.passage("flood, flash", [], phrase))
        self.assertIsNone(goesview.passage("aflash flood", [], phrase))
        self.assertEqual(goesview.passage("A FLASH FLOOD warning.", [], phrase), f"A {B}FLASH FLOOD{E} warning.")

    def test_phrase_may_run_across_line_ends_and_punctuation(self):
        text = "...FLASH\r\n  FLOOD WARNING..."
        self.assertEqual(goesview.passage(text, [], [["flash", "flood", "warning"]]), f"...{B}FLASH\r\n  FLOOD WARNING{E}...")

    def test_one_missing_phrase_of_several_is_enough_to_reject(self):
        self.assertIsNone(goesview.passage("red flag warning", [], [["red", "flag"], ["high", "wind"]]))

    def test_nothing_in_the_text_matched(self):
        """"" means the hit was on the product or office code, not the text."""
        self.assertEqual(goesview.passage("Area forecast discussion.", ["afd"], []), "")
        self.assertEqual(goesview.passage("Area forecast discussion.", [], []), "")
        self.assertEqual(goesview.passage("", ["afd"], []), "")

    def test_long_bulletin_is_cut_to_a_passage_around_the_first_match(self):
        text = ("lorem ipsum " * 60) + "TORNADO sighted " + ("dolor sit amet " * 60) + "tornado again"
        out = goesview.passage(text, ["tornado"], [])
        self.assertTrue(out.startswith("… "))
        self.assertTrue(out.endswith(" …"))
        self.assertIn(f"{B}TORNADO{E} sighted", out)
        self.assertLess(len(out), 320)
        self.assertNotIn("again", out)
        body = out[2:-2]
        self.assertIn(body.replace(B, "").replace(E, ""), text)       # a verbatim stretch of the bulletin
        self.assertFalse(body.startswith(" ") or body.endswith(" "))
        self.assertTrue(body.startswith(("lorem", "ipsum")), "starts mid-word")

    def test_match_at_the_very_start_and_end(self):
        self.assertEqual(goesview.passage("flood", ["flood"], []), f"{B}flood{E}")
        self.assertEqual(goesview.passage("a flood", ["flood"], []), f"a {B}flood{E}")


@unittest.skipUnless(fx.have_fts5(), "this Python's SQLite has no FTS5")
class TextIndexTests(fx.GoesviewCase):
    def setUp(self):
        super().setUp()
        t, now = self.tree, self.now
        self.flood = t.bulletin(now - 5 * HOUR, "FFWSEWWA",
                                text="...FLASH FLOOD WARNING for King County...\r\nHeavy rain continues near Seattle.\r\n")
        self.flooding = t.bulletin(now - 4 * HOUR, "AFDSEWWA",
                                   text="Area forecast discussion.\r\nFlash flooding is possible. A flood watch may follow.\r\n")
        self.wind = t.bulletin(now - 3 * HOUR, "NPWPDTOR", office="KPDT",
                               text="High wind warning for the Columbia Gorge.\r\nGusts to 60 mph.\r\n")
        self.tornado = t.bulletin(now - 2 * HOUR, "TORSEWWA", text="Tornado warning.\x01\x1e Take cover now.\r\n")
        t.slot(now - HOUR)
        self.app = fx.make_app(self, t)
        self.catalog = self.app.catalog
        os.makedirs(t.cache, exist_ok=True)
        self.index, self.db = self.open_index(self.catalog)
        self.app.text_index = self.index
        self.index.catch_up(self.db)

    def open_index(self, catalog):
        index = goesview.TextIndex(catalog, self.tree.cache)
        db = index.connect()
        self.addCleanup(db.close)
        return index, db

    def search(self, text, **options):
        hits, cursor, more = self.catalog.search_text(self.index, text, **options)
        return [item.path[2:] for item, _ in hits]

    def excerpt(self, text):
        hits, _, _ = self.catalog.search_text(self.index, text)
        return hits[0][1]

    def test_finds_a_word_in_the_text(self):
        self.assertEqual(self.search("seattle"), [self.flood])
        self.assertEqual(self.search("SEATTLE"), [self.flood])
        self.assertEqual(self.search("columbia gorge"), [self.wind])
        self.assertEqual(self.search("nowhere"), [])

    def test_every_word_must_appear(self):
        self.assertEqual(self.search("warning"), [self.tornado, self.wind, self.flood])
        self.assertEqual(self.search("flood warning"), [self.flood])
        self.assertEqual(self.search("flood warning tornado"), [])

    def test_words_match_from_their_start(self):
        self.assertEqual(self.search("gust"), [self.wind])
        self.assertEqual(self.search("flood"), [self.flooding, self.flood])
        self.assertEqual(self.search("ooding"), [])

    def test_quoted_phrase_must_match_exactly(self):
        """Both bulletins hold the words "flash" and "flood"; only one holds the phrase."""
        self.assertEqual(self.search("flash flood"), [self.flooding, self.flood])
        self.assertEqual(self.search('"flash flood"'), [self.flood])
        self.assertEqual(self.search('"flood watch"'), [self.flooding])
        self.assertEqual(self.search('"flood watch" seattle'), [])

    def test_results_are_newest_first(self):
        self.assertEqual(self.search("warning"), [self.tornado, self.wind, self.flood])

    def test_product_and_office_codes_are_searchable(self):
        """"The search box looks through the full text of every bulletin as well as
        product and office codes." """
        self.assertEqual(self.search("NPW"), [self.wind])
        self.assertEqual(self.search("kpdt"), [self.wind])
        self.assertEqual(self.search("FFWSEWWA"), [self.flood])
        self.assertEqual(self.search("AFD SEW"), [self.flooding])
        self.assertEqual(self.search("FXUS66 seattle"), [self.flood])

    def test_excerpt_shows_the_match_in_the_bulletins_own_text(self):
        # (control characters, carriage returns among them, are shown as spaces)
        self.assertEqual(self.excerpt("seattle"),
                         f"...FLASH FLOOD WARNING for King County... \nHeavy rain continues near {B}Seattle{E}.")
        self.assertEqual(self.excerpt("NPW"), "")   # found by its product code: nothing to mark in the text
        self.assertEqual(self.excerpt("tornado warning"), f"{B}Tornado{E} {B}warning{E}.   Take cover now.")

    def test_type_filter(self):
        tor = "text/EMWIN/TOR · Tornado warning"
        self.assertEqual(self.search("warning", type_id=tor), [self.tornado])
        self.assertEqual(self.search("seattle", type_id=tor), [])

    def test_paging_returns_every_hit_exactly_once(self):
        expected = [self.tornado, self.wind, self.flood]
        seen, cursor = [], None
        for _ in range(5):
            hits, cursor, more = self.catalog.search_text(self.index, "warning", before=cursor, limit=1)
            seen += [item.path[2:] for item, _ in hits]
            if not more:
                break
        self.assertEqual(seen, expected)
        hits, cursor, more = self.catalog.search_text(self.index, "warning", limit=3)
        self.assertEqual((len(hits), more), (3, False))

    def test_empty_and_hostile_queries_are_harmless(self):
        for text in ("", "   ", '"', '""', "?", "NOT", "a OR b", 'x" OR "y', "*", "name:x", "NEAR(flood warning)", "flood*"):
            with self.subTest(text=text):
                hits, cursor, more = self.catalog.search_text(self.index, text)
                self.assertIsInstance(hits, list)
        self.assertEqual(self.search("warning OR nothing"), [])   # OR is a word here, and no bulletin has it
        self.assertEqual(self.search("flood*"), [self.flooding, self.flood])

    def test_new_bulletin_is_indexed_when_it_arrives(self):
        new = self.tree.bulletin(self.now - 600, "SVRSEWWA", text="Severe thunderstorm with large hail.\r\n")
        image = self.tree.slot(self.now - 500, bands=(2,))
        self.catalog.poll()
        self.assertEqual(self.index.jobs.qsize(), 1, "only text arrivals are queued for indexing")
        self.assertEqual(self.search("hail"), [])
        self.index.drain(self.db)
        self.assertEqual(self.search("hail"), [new])
        self.assertEqual(self.search("thunderstorm SVR"), [new])
        self.assertIn(f"s/{image}/{fx.band_name(self.now - 500)}", self.catalog.by_path)

    def test_deleted_bulletin_is_forgotten(self):
        vpath = "s/" + self.wind
        self.index.forget({vpath, "s/some/image.png"})
        self.index.drain(self.db)
        match = goesview.parse_query("gorge")[0]
        self.assertEqual([path for _, path in self.index.search(match)], [None])  # its words linger, its path does not
        self.assertEqual(self.search("gorge"), [])
        self.assertEqual(self.index.dropped, 1)
        self.assertEqual(self.search("warning"), [self.tornado, self.flood])

    def test_archived_bulletins_are_searched_as_before(self):
        """README, Bulletin archive: "Archived bulletins are listed, shown, linked and
        searched exactly as before." """
        old = self.tree.bulletin(self.now - 3 * DAY, "RERSEWWA", text="Record rainfall at Quillayute airport.\r\n")
        self.catalog.poll()
        self.index.drain(self.db)
        self.assertEqual(self.search("quillayute"), [old])
        fx.make_archiver(self.app).sweep()
        self.assertFalse(self.tree.exists(old))
        self.index.drain(self.db)
        hits, _, _ = self.catalog.search_text(self.index, '"record rainfall" quillayute')
        self.assertEqual(len(hits), 1)
        item, excerpt = hits[0]
        self.assertTrue(item.path.startswith("z/"))
        self.assertTrue(item.path.endswith(os.path.basename(old)))
        self.assertEqual(excerpt, f"{B}Record rainfall{E} at {B}Quillayute{E} airport.")
        self.assertEqual(self.search("RER"), [item.path[2:]])

    def test_restart_indexes_only_what_is_new_and_drops_what_is_gone(self):
        os.unlink(self.tree.path(self.wind))
        new = self.tree.bulletin(self.now - 900, "SVRSEWWA", text="Severe thunderstorm with large hail.\r\n")
        self.db.close()
        self.app = fx.make_app(self, self.tree)             # GOESview starts again
        self.catalog = self.app.catalog
        self.index, self.db = self.open_index(self.catalog)
        self.index.indexed = self.db.execute("SELECT value FROM meta WHERE key = 'indexed'").fetchone()[0]
        self.assertEqual(self.index.indexed, 4)
        self.index.catch_up(self.db)
        self.assertEqual(self.index.indexed, 5, "bulletins already in the index were indexed again")
        self.assertEqual(self.index.total, 1)
        self.assertEqual(self.search("hail"), [new])
        self.assertEqual(self.search("gorge"), [])
        self.assertEqual(self.search("warning"), [self.tornado, self.flood])
        self.assertEqual(self.db.execute("SELECT count(*) FROM files").fetchone()[0], 4)

    def test_rebuild_sheds_deleted_bulletins_and_keeps_the_rest(self):
        self.catalog.forget(set(), {"s/" + self.wind})
        self.index.forget({"s/" + self.wind})
        self.index.drain(self.db)
        match = goesview.parse_query("gorge")[0]
        self.assertEqual(len(self.index.search(match)), 1)
        self.db = self.index.rebuild(self.db)
        self.addCleanup(self.db.close)
        self.assertEqual(self.index.search(match), [])
        self.assertEqual((self.index.indexed, self.index.dropped), (3, 0))
        self.assertEqual(self.search("warning"), [self.tornado, self.flood])
        self.assertFalse([name for name in os.listdir(self.tree.cache) if ".new" in name], "rebuild left its scratch files")

    def test_index_does_not_keep_the_text(self):
        """README: "It records only which bulletins contain which words"; excerpts
        are read from the bulletins themselves."""
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(self.index.path, "rb") as f:
            stored = f.read()
        self.assertNotIn(b"Heavy rain continues", stored)
        self.assertNotIn(b"Columbia Gorge", stored)
        self.assertEqual(self.index.text_of("s/" + self.wind), "High wind warning for the Columbia Gorge. \nGusts to 60 mph. \n")
        self.assertIsNone(self.index.text_of("s/no/such/bulletin.txt"))

    def test_first_designs_index_file_is_cleared_away_and_nothing_else(self):
        """Starting up removes the old text-index.sqlite3 (which held a second copy of
        every bulletin) from the cache folder; every other file there is left."""
        cache = self.tree.cache
        for name in ("text-index.sqlite3", "text-index.sqlite3-wal", "dcs-2.sqlite3", "monitor-1.sqlite3", "notes.txt"):
            with open(os.path.join(cache, name), "wb") as f:
                f.write(b"x")
        self.open_index(self.catalog)
        left = set(os.listdir(cache))
        self.assertFalse({"text-index.sqlite3", "text-index.sqlite3-wal"} & left)
        self.assertTrue({"dcs-2.sqlite3", "monitor-1.sqlite3", "notes.txt", "text-index-2.sqlite3"} <= left)

    def test_progress(self):
        self.assertEqual(self.index.progress(), {"ready": False, "done": 4, "total": 4})


if __name__ == "__main__":
    unittest.main()
