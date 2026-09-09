import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import request

import app


class PortalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "portal.sqlite3"
        self.db_patch = patch.object(app, "DB", self.db)
        self.db_patch.start()
        app.init()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_init_creates_a_missing_application_root(self):
        root = Path(self.tmp.name) / "AppData" / "Local" / "YTVault"
        with (
            patch.object(app, "ROOT", root),
            patch.object(app, "DB", root / "data" / "portal.sqlite3"),
            patch.object(app, "VIDEOS", root / "videos"),
            patch.object(app, "THUMBS", root / "thumbs"),
        ):
            app.init()
            self.assertTrue((root / "data" / "portal.sqlite3").is_file())
            self.assertTrue((root / "videos").is_dir())
            self.assertTrue((root / "thumbs").is_dir())

    def test_create_category_requires_unique_nonempty_name(self):
        created = app.create_category("  Estudios nuevos  ")
        self.assertEqual(created["name"], "Estudios nuevos")
        with self.assertRaisesRegex(ValueError, "nombre"):
            app.create_category("   ")
        with self.assertRaisesRegex(ValueError, "existe"):
            app.create_category("estudios NUEVOS")
    def test_add_video_list_requires_a_valid_category(self):
        category = app.create_category("Tafsir nuevo")
        result = app.add_video_urls(
            category["id"],
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ\nhttps://youtu.be/aqz-KE-bpKQ",
        )
        self.assertEqual(result, {"added": 2, "existing": 0, "video_ids": ["dQw4w9WgXcQ", "aqz-KE-bpKQ"]})
        with app.con() as c:
            rows = c.execute("SELECT video_id,status,category_id FROM videos ORDER BY rowid").fetchall()
        self.assertEqual([(r["video_id"], r["status"], r["category_id"]) for r in rows], [
            ("dQw4w9WgXcQ", "remote", category["id"]),
            ("aqz-KE-bpKQ", "remote", category["id"]),
        ])
        with self.assertRaisesRegex(ValueError, "categoría"):
            app.add_video_urls(9999, "https://youtu.be/dQw4w9WgXcQ")
    def test_playlist_is_expanded_before_inserting(self):
        category = app.create_category("Lista privada")
        calls = []

        def fake_playlist_loader(url):
            calls.append(url)
            return [
                {"video_id": "M7lc1UVf-VE", "title": "Vídeo uno"},
                {"video_id": "aqz-KE-bpKQ", "title": "Vídeo dos"},
            ]

        result = app.add_video_urls(
            category["id"],
            "https://www.youtube.com/playlist?list=PLprueba123",
            playlist_loader=fake_playlist_loader,
        )
        self.assertEqual(result["added"], 2)
        self.assertEqual(calls, ["https://www.youtube.com/playlist?list=PLprueba123"])
        with app.con() as c:
            titles = [r[0] for r in c.execute("SELECT title FROM videos ORDER BY rowid")]
        self.assertEqual(titles, ["Vídeo uno", "Vídeo dos"])
    def test_queue_video_and_whole_category_skip_downloaded_items(self):
        category = app.create_category("Descargas")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ\naqz-KE-bpKQ\nM7lc1UVf-VE")
        with app.con() as c:
            c.execute("UPDATE videos SET status='done' WHERE video_id='M7lc1UVf-VE'")
        self.assertEqual(app.queue_video("dQw4w9WgXcQ"), 1)
        self.assertEqual(app.queue_category(category["id"]), 1)
        with app.con() as c:
            states = dict(c.execute("SELECT video_id,status FROM videos"))
        self.assertEqual(states, {
            "dQw4w9WgXcQ": "pending",
            "aqz-KE-bpKQ": "pending",
            "M7lc1UVf-VE": "done",
        })
    def test_init_backfills_existing_videos_into_visible_categories(self):
        with app.con() as c:
            c.execute(
                "INSERT INTO videos(video_id,url,title,status,category,upload_date) VALUES(?,?,?,?,?,?)",
                ("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "Lengua Árabe 1", "done", "Clases de árabe", "20251001"),
            )
        app.init()
        with app.con() as c:
            row = c.execute(
                "SELECT c.name FROM videos v JOIN categories c ON c.id=v.category_id WHERE v.video_id=?",
                ("dQw4w9WgXcQ",),
            ).fetchone()
        self.assertEqual(row["name"], "Árabe 25/26")
        self.assertIn("Árabe 25/26", [c["name"] for c in app.list_categories()])
    def test_frontend_exposes_management_forms_and_download_actions(self):
        category = app.create_category("Curso nuevo")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        page = app.landing_page()
        self.assertIn('id="categoryForm"', page)
        self.assertIn('id="videoForm"', page)
        self.assertIn('<option value="" selected disabled>Selecciona una categoría</option>', page)
        self.assertIn('data-download-video="dQw4w9WgXcQ"', page)
        self.assertIn(f'data-download-category="{category["id"]}"', page)

    def test_json_api_creates_adds_and_queues(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, payload):
            req = request.Request(
                f'http://127.0.0.1:{server.server_port}{path}',
                data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)

        try:
            status, category = post('/api/categories', {'name': 'Desde API'})
            self.assertEqual(status, 201)
            status, added = post('/api/videos/add', {'category_id': category['category']['id'], 'urls': 'dQw4w9WgXcQ'})
            self.assertEqual((status, added['added']), (201, 1))
            with patch.object(app, 'start_downloader') as starter:
                status, queued = post('/api/download/video', {'video_id': 'dQw4w9WgXcQ'})
            self.assertEqual((status, queued['queued']), (200, 1))
            starter.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
