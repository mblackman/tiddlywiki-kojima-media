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


if __name__ == "__main__":
    unittest.main()
