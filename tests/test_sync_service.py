#!/usr/bin/env python3
"""
Unit tests for `kojima_sync_service.py`:
- Multi-Factor Enthusiasm Score formula (Words, Sentiment, Prestige, Recurrence)
- 1-sentence dismissal detection (`"Saw Madame Web at the theater."`)
- SQLite persistence & caching (`upsert_recommendation`, `build_recommendations_feed`)
- RSS 2.0 / Atom XML feed parser & movie post extractor (`extract_movie_from_post`, `ingest_raw_post`)
- CORS HTTP JSON endpoints (`GET /api/kojima-recommendations`, `POST /api/ingest-post`, `GET /health`)
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import HTTPServer

import kojima_sync_service as kss


class TestKojimaSyncService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_kojima.db")
        kss.init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_one_sentence_dismissal_detection(self):
        self.assertTrue(
            kss.is_one_sentence_dismissal("Saw Madame Web at the theater.", ["x_review"])
        )
        self.assertTrue(
            kss.is_one_sentence_dismissal("Watched '65' since Adam Driver and the dinosaur are starring.", ["x_review"])
        )
        # Curated list items are never dismissals even if short
        self.assertFalse(
            kss.is_one_sentence_dismissal("Saw 2001: A Space Odyssey.", ["gq_top_4"])
        )

    def test_enthusiasm_scoring_tiers(self):
        # S-Tier: All-time canon with multiple sources
        s_tier = kss.compute_enthusiasm_score(
            "Taxi Driver",
            (
                "This film was an eye-opener for me. I had no idea Americans, who seemed so cheerful, "
                "could feel lonely in the middle of a city. It resonated with me, given the loneliness "
                "that troubled me in my younger days, which is why it has a very special place in my heart. "
                "After I saw it I bought a jacket and a pair of boots to copy Robert De Niro. "
                "Needless to say, I did not get a mohawk."
            ),
            [
                {"label": "GQ All-Time Top 4", "type": "gq_top_4", "url": "https://example.com/gq"},
                {"label": "Junker HQ Top 30", "type": "junkerhq_top_30", "url": "https://example.com/jhq"},
            ],
        )
        self.assertGreaterEqual(s_tier.total_score, 90)
        self.assertEqual(s_tier.tier, "S-Tier: Essential Kojima DNA")
        self.assertFalse(s_tier.is_dismissal)

        # D-Tier: One-sentence dismissal capped <= 20
        d_tier = kss.compute_enthusiasm_score(
            "Madame Web",
            "Saw Madame Web at the theater.",
            [{"label": "X Review", "type": "x_review", "url": "https://example.com/x"}],
        )
        self.assertLessEqual(d_tier.total_score, 20)
        self.assertEqual(d_tier.word_count_pts, 0.0)
        self.assertTrue(d_tier.is_dismissal)
        self.assertEqual(d_tier.tier, "D-Tier: One-Sentence Dismissal")

        # A-Tier: Year-End Top List entry even when only an editorial list-inclusion blurb is present
        self.assertTrue(
            kss.is_editorial_list_blurb(
                "Named to Hideo Kojima's Top Foreign Theatrical Releases of the Year list.",
                ["annual_top_list"],
            )
        )
        editorial_top_list = kss.compute_enthusiasm_score(
            "Conclave",
            "Named to Hideo Kojima's Top Foreign Theatrical Releases of the Year list.",
            [
                {"label": "Top Films of 2025", "type": "annual_top_list", "url": "https://example.com/ig"},
                {"label": "2025 Report", "type": "annual_top_list", "url": "https://example.com/gt"},
            ],
        )
        self.assertGreaterEqual(editorial_top_list.total_score, 75)
        self.assertEqual(editorial_top_list.tier, "A-Tier: All-Time Favorite / Rave")

        # B-Tier: Standalone modern X rave without era-specific MGS/childhood keywords
        standalone_rave = kss.compute_enthusiasm_score(
            "Avatar: Fire and Ash",
            "This film shows absolutely no compromise, right down to the finest details, in the construction of the Avatar 'universe.' It was magnificent.",
            [{"label": "X Review", "type": "x_review", "url": "https://example.com/x-avatar"}],
        )
        self.assertGreaterEqual(standalone_rave.total_score, 60)
        self.assertEqual(standalone_rave.tier, "B-Tier: Strong Recommendation")

    def test_post_extractor_and_sqlite_ingest(self):
        extracted = kss.extract_movie_from_post("Watched 'Furiosa: A Mad Max Saga' (2024) at IMAX! George Miller is my god of cinema!")
        self.assertIsNotNone(extracted)
        title, year, _ = extracted
        self.assertEqual(title, "Furiosa: A Mad Max Saga")
        self.assertEqual(year, "2024")

        record = kss.ingest_raw_post(
            raw_text="Watched 'Furiosa: A Mad Max Saga' (2024) at IMAX! George Miller is my god of cinema! Masterpiece!",
            source_url="https://x.com/HIDEO_KOJIMA_EN/status/12345",
            explicit_tmdb_id="786892",
            db_path=self.db_path,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["title"], "Furiosa: A Mad Max Saga")
        self.assertEqual(record["tmdb_id"], "786892")
        self.assertGreaterEqual(record["kojima_score"], 45)

    def test_rss_xml_parser(self):
        sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Hideo Kojima</title>
            <item>
              <guid>post-1</guid>
              <title>Saw Nosferatu at the theater.</title>
              <description>Saw Nosferatu at the theater.</description>
              <link>https://x.com/HIDEO_KOJIMA_EN/status/999</link>
            </item>
          </channel>
        </rss>"""
        entries = kss.parse_rss_or_atom_xml(sample_rss)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["guid"], "post-1")
        ext = kss.extract_movie_from_post(entries[0]["text"])
        self.assertIsNotNone(ext)
        self.assertEqual(ext[0], "Nosferatu")

    def test_http_endpoints(self):
        old_db = kss.KOJIMA_DB_PATH
        kss.KOJIMA_DB_PATH = self.db_path
        server = HTTPServer(("127.0.0.1", 0), kss.KojimaSyncHandler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 1. GET /health
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                health = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")
                self.assertGreaterEqual(health["count"], 25)

            # 2. GET /api/kojima-recommendations
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/kojima-recommendations", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
                payload = json.loads(resp.read().decode("utf-8"))
                self.assertGreaterEqual(payload["count"], 25)
                scores = [item["kojima_score"] for item in payload["items"]]
                self.assertEqual(scores, sorted(scores, reverse=True))
        finally:
            server.shutdown()
            server.server_close()
            kss.KOJIMA_DB_PATH = old_db

    def test_sync_static_files_preserves_existing_and_writes_api_payload(self):
        seed_out = os.path.join(self.temp_dir.name, "seed-recommendations.json")
        api_out = os.path.join(self.temp_dir.name, "api", "kojima-recommendations.json")

        # Pre-populate seed file with an extra scraped item to verify stateless CI preservation
        with open(seed_out, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "title": "Furiosa: A Mad Max Saga",
                        "year": "2024",
                        "tmdb_id": "786892",
                        "director": "George Miller",
                        "runtime": "148",
                        "genres": ["Action", "Adventure", "Science Fiction"],
                        "poster": "https://example.com/furiosa.jpg",
                        "plot": "Young Furiosa is snatched from the Green Place of Many Mothers.",
                        "kojima_review": "Watched 'Furiosa: A Mad Max Saga' at IMAX! George Miller is my god of cinema! Masterpiece!",
                        "kojima_recommended_at": "2024-05-24",
                        "kojima_sources_json": json.dumps([
                            {"label": "X (@HIDEO_KOJIMA_EN)", "type": "x_review", "url": "https://x.com/furiosa"}
                        ]),
                    }
                ],
                fh,
            )

        payload = kss.sync_static_files(
            seed_json_path=seed_out,
            api_json_path=api_out,
            scrape_rss=False,
            force_refresh=False,
            enrich=False,
            db_path=self.db_path,
        )
        self.assertGreaterEqual(payload["count"], 26)
        self.assertTrue(payload["changed"])
        self.assertTrue(os.path.exists(seed_out))
        self.assertTrue(os.path.exists(api_out))
        self.assertTrue(os.path.exists(api_out[:-5]))  # extensionless alias

        with open(api_out, "r", encoding="utf-8") as fh:
            saved_api = json.load(fh)
        self.assertEqual(saved_api["count"], payload["count"])
        titles = {item["title"] for item in saved_api["items"]}
        self.assertIn("Furiosa: A Mad Max Saga", titles)
        self.assertIn("2001: A Space Odyssey", titles)
        self.assertNotIn("content_hash", saved_api["items"][0])

        # Ledger is an object with last_updated + per-item content_hash
        with open(seed_out, "r", encoding="utf-8") as fh:
            ledger = json.load(fh)
        self.assertIsInstance(ledger, dict)
        self.assertTrue(ledger["last_updated"])
        self.assertTrue(all(item.get("content_hash") for item in ledger["items"]))

        # Second run with a fresh DB restores from the ledger: no changes, last_updated preserved
        second_db = os.path.join(self.temp_dir.name, "second.db")
        second = kss.sync_static_files(
            seed_json_path=seed_out,
            api_json_path=api_out,
            scrape_rss=False,
            enrich=False,
            db_path=second_db,
        )
        self.assertFalse(second["changed"])
        self.assertEqual(second["last_updated"], ledger["last_updated"])
        self.assertEqual(second["count"], payload["count"])

    def test_catalog_loads_without_duplicating_seed(self):
        catalog = kss.load_catalog()
        slugs = [kss.make_slug(i["title"], str(i.get("year") or "0000")) for i in catalog]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertGreater(len(catalog), len(kss.SEED_RECOMMENDATIONS))

    def test_rss_since_filter_skips_old_posts(self):
        sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item><guid>old</guid><title>Saw Nosferatu at the theater.</title>
            <link>https://x.com/a/1</link><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
          <item><guid>new</guid><title>Saw Megalopolis at the theater.</title>
            <link>https://x.com/a/2</link><pubDate>Mon, 01 Sep 2025 10:00:00 GMT</pubDate></item>
        </channel></rss>"""
        feed_path = os.path.join(self.temp_dir.name, "feed.xml")
        with open(feed_path, "w", encoding="utf-8") as fh:
            fh.write(sample_rss)
        ingested = kss.poll_rss_feeds(
            feed_urls=["file://" + feed_path], db_path=self.db_path, since="2025-01-01T00:00:00Z"
        )
        self.assertEqual([i["title"] for i in ingested], ["Megalopolis"])

    def test_letterboxd_rss_xml_parser_extracts_film_tags_and_x_links(self):
        sample_lb_rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0" xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org">
          <channel>
            <item>
              <title>Caught Stealing, 2025</title>
              <link>https://letterboxd.com/kojima_hideo/film/caught-stealing/</link>
              <guid isPermaLink="false">letterboxd-review-1152802149</guid>
              <pubDate>Mon, 12 Jan 2026 05:42:05 +1300</pubDate>
              <letterboxd:watchedDate>2026-01-11</letterboxd:watchedDate>
              <letterboxd:filmTitle>Caught Stealing</letterboxd:filmTitle>
              <letterboxd:filmYear>2025</letterboxd:filmYear>
              <tmdb:movieId>1245993</tmdb:movieId>
              <description><![CDATA[
                <p><img src="https://a.ltrbxd.com/resized/film-poster/caught-stealing.jpg?v=1"/></p>
                <p>Darren’s new film has finally been released in Japan, so I went to see Darren Aronofsky’s “Caught Stealing.” Whoa!!! It was insanely good!! Highly recommend!</p>
                <p><a href="https://x.com/HIDEO_KOJIMA_EN/status/2010290439681982768/" rel="nofollow">x.com/HIDEO_KOJIMA_EN/status/2010290439681982768/</a></p>
              ]]></description>
            </item>
            <item>
              <title>Best Movies 2025 by Hideo Kojima</title>
              <link>https://letterboxd.com/kojima_hideo/list/best-movies-2025-by-hideo-kojima/</link>
              <guid isPermaLink="false">letterboxd-list-999</guid>
              <pubDate>Wed, 7 Jan 2026 15:24:23 +1300</pubDate>
              <description><![CDATA[<p>not ranked</p>]]></description>
            </item>
          </channel>
        </rss>"""
        entries = kss.parse_rss_or_atom_xml(sample_lb_rss)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["explicit_title"], "Caught Stealing")
        self.assertEqual(entry["explicit_year"], "2025")
        self.assertEqual(entry["explicit_tmdb_id"], "1245993")
        self.assertEqual(entry["date"], "2026-01-11")
        self.assertEqual(entry["url"], "https://x.com/HIDEO_KOJIMA_EN/status/2010290439681982768")
        self.assertNotIn("x.com/HIDEO_KOJIMA_EN", entry["text"])

    def test_extract_movie_from_real_2026_kojima_posts(self):
        cases = [
            (
                "Darren’s new film has finally been released in Japan, so I went to see Darren Aronofsky’s “Caught Stealing.” Whoa!!! It was insanely good!!",
                "Caught Stealing",
            ),
            (
                "At the David Keighley Theater, I watched Christopher Nolan’s “The Odyssey” (shot entirely with IMAX® film cameras) presented in 70mm IMAX® film.",
                "The Odyssey",
            ),
            (
                "I wasn’t particularly interested at first, but since it’s Zach Cregger’s latest film, I went to see “Resident Evil.”",
                "Resident Evil",
            ),
            (
                "I had been holding off on posting my thoughts until the interview was released. Director Iñárritu’s “DIGGER” is a film that all of us living in today’s world should see.",
                "DIGGER",
            ),
            (
                "After seeing the trailer, I had been curious about Patrick Hughes’ “War Machine.” Tonight I decided to watch this one first. Wow, it was quite entertaining.",
                "War Machine",
            ),
            (
                "I went to the Japan premiere of “Godzilla Minus Zero” at TOHO Cinemas Hibiya.",
                "Godzilla Minus Zero",
            ),
            (
                "I saw “Supergirl” in IMAX. It was a coming-of-age story about saving oneself.",
                "Supergirl",
            ),
        ]
        for raw, expected_title in cases:
            extracted = kss.extract_movie_from_post(raw)
            self.assertIsNotNone(extracted, f"Failed to extract from: {raw}")
            self.assertEqual(extracted[0], expected_title)

    def test_sync_static_files_ingests_unseen_rss_posts_regardless_of_ledger_timestamp(self):
        seed_out = os.path.join(self.temp_dir.name, "seed-recommendations.json")
        api_out = os.path.join(self.temp_dir.name, "api", "kojima-recommendations.json")
        with open(seed_out, "w", encoding="utf-8") as fh:
            json.dump({"last_updated": "2026-10-29T00:00:00Z", "items": []}, fh)

        feed_path = os.path.join(self.temp_dir.name, "feed_2026.xml")
        with open(feed_path, "w", encoding="utf-8") as fh:
            fh.write(
                """<?xml version="1.0" encoding="UTF-8"?>
                <rss version="2.0"><channel>
                  <item>
                    <guid>post-2026-odyssey</guid>
                    <title>Watched “The Odyssey” (2026) in 70mm IMAX. It was magnificent.</title>
                    <description>Watched “The Odyssey” (2026) in 70mm IMAX. It was magnificent.</description>
                    <link>https://x.com/HIDEO_KOJIMA_EN/status/2085501858232271245</link>
                    <pubDate>Thu, 06 Aug 2026 23:02:44 GMT</pubDate>
                  </item>
                </channel></rss>"""
            )

        old_urls = kss.KOJIMA_RSS_URLS
        kss.KOJIMA_RSS_URLS = ["file://" + feed_path]
        try:
            payload = kss.sync_static_files(
                seed_json_path=seed_out,
                api_json_path=api_out,
                scrape_rss=True,
                enrich=False,
                db_path=self.db_path,
                catalog_path=os.path.join(self.temp_dir.name, "empty.jsonl"),
            )
            titles = {i["title"] for i in payload["items"]}
            self.assertIn("The Odyssey", titles)
        finally:
            kss.KOJIMA_RSS_URLS = old_urls

    def test_catalog_includes_2026_recommendations(self):
        catalog = kss.load_catalog()
        years_released = {str(i.get("year") or "") for i in catalog}
        rec_years = {
            kss._infer_recommended_at(str(i["title"]), i, i.get("sources") or "")[:4]
            for i in catalog
        }
        self.assertIn("2026", years_released)
        self.assertIn("2026", rec_years)
        titles = {i["title"] for i in catalog}
        for expected in (
            "The Odyssey",
            "Spider-Man: Brand New Day",
            "Godzilla Minus Zero",
            "Whalefall",
            "Digger",
            "Resident Evil",
            "Heart of the Beast",
            "Backrooms",
            "Supergirl",
            "The Mandalorian and Grogu",
            "War Machine",
            "Crime 101",
            "Caught Stealing",
            "Hamnet",
        ):
            self.assertIn(expected, titles)


if __name__ == "__main__":
    unittest.main()
