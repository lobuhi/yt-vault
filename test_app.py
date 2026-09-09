import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

import app


class PortalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "portal.sqlite3"
        self.db_patch = patch.object(app, "DB", self.db)
        self.root_patch = patch.object(app, "ROOT", Path(self.tmp.name))
        self.videos_patch = patch.object(app, "VIDEOS", Path(self.tmp.name) / "videos")
        self.thumbs_patch = patch.object(app, "THUMBS", Path(self.tmp.name) / "thumbs")
        self.db_patch.start()
        self.root_patch.start()
        self.videos_patch.start()
        self.thumbs_patch.start()
        app.init()

    def tearDown(self):
        self.thumbs_patch.stop()
        self.videos_patch.stop()
        self.root_patch.stop()
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
    def test_video_can_belong_to_multiple_categories(self):
        first = app.create_category("Tafsir")
        second = app.create_category("Ramadán")
        app.add_video_urls(first["id"], "dQw4w9WgXcQ")
        result = app.add_video_urls(second["id"], "dQw4w9WgXcQ")
        self.assertEqual(result["existing"], 1)
        self.assertEqual(app.video_category_ids("dQw4w9WgXcQ"), [first["id"], second["id"]])
        page = app.landing_page()
        self.assertEqual(page.count('data-manage-video="dQw4w9WgXcQ"'), 2)
        self.assertIn('Total: 1', page)

    def test_reorganize_video_replaces_its_categories(self):
        first = app.create_category("Primera")
        second = app.create_category("Segunda")
        third = app.create_category("Tercera")
        app.add_video_urls(first["id"], "dQw4w9WgXcQ")
        app.set_video_categories("dQw4w9WgXcQ", [second["id"], third["id"]])
        self.assertEqual(app.video_category_ids("dQw4w9WgXcQ"), [second["id"], third["id"]])
        with self.assertRaisesRegex(ValueError, "categoría"):
            app.set_video_categories("dQw4w9WgXcQ", [])

    def test_delete_video_keeps_local_files_unless_requested(self):
        category = app.create_category("Archivo")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        media = app.VIDEOS / "video local.mp4"
        thumb = app.THUMBS / "dQw4w9WgXcQ.jpg"
        media.write_bytes(b"video")
        thumb.write_bytes(b"thumb")
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, "dQw4w9WgXcQ"))
        result = app.delete_video("dQw4w9WgXcQ", delete_local=False)
        self.assertFalse(result["local_deleted"])
        self.assertTrue(media.is_file())
        self.assertTrue(thumb.is_file())
        with app.con() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM videos WHERE video_id=?", ("dQw4w9WgXcQ",)).fetchone())

    def test_delete_video_can_remove_its_safe_local_files(self):
        category = app.create_category("Archivo")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        media = app.VIDEOS / "video local.mp4"
        thumb = app.THUMBS / "dQw4w9WgXcQ.jpg"
        media.write_bytes(b"video")
        thumb.write_bytes(b"thumb")
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, "dQw4w9WgXcQ"))
        result = app.delete_video("dQw4w9WgXcQ", delete_local=True)
        self.assertTrue(result["local_deleted"])
        self.assertFalse(media.exists())
        self.assertFalse(thumb.exists())

    def test_delete_video_rejects_a_filename_outside_the_library(self):
        category = app.create_category("Archivo")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        outside = Path(self.tmp.name).parent / "no-borrar.mp4"
        outside.write_bytes(b"protegido")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with app.con() as c:
            c.execute("UPDATE videos SET filename=? WHERE video_id=?", (str(outside), "dQw4w9WgXcQ"))
        with self.assertRaisesRegex(ValueError, "segura"):
            app.delete_video("dQw4w9WgXcQ", delete_local=True)
        self.assertTrue(outside.is_file())
        with app.con() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM videos WHERE video_id=?", ("dQw4w9WgXcQ",)).fetchone())

    def test_delete_video_rejects_active_downloads(self):
        category = app.create_category("En curso")
        for index, state in enumerate(("pending", "running")):
            video_id = f"ACTIVE{index:05d}"
            app.add_video_urls(category["id"], video_id)
            with app.con() as c:
                c.execute("UPDATE videos SET status=? WHERE video_id=?", (state, video_id))
            with self.assertRaisesRegex(ValueError, "descarga"):
                app.delete_video(video_id, delete_local=True)

    def test_delete_video_does_not_race_with_queueing(self):
        category = app.create_category("Carrera")
        video_id = "RACE0000001"
        app.add_video_urls(category["id"], video_id)
        real_con = app.con

        class InterleavedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                if sql.strip().upper().startswith("DELETE FROM VIDEOS"):
                    with real_con() as other:
                        other.execute("UPDATE videos SET status='pending' WHERE video_id=?", (video_id,))
                return self.connection.execute(sql, params)

        @contextlib.contextmanager
        def interleaved_con():
            with real_con() as connection:
                yield InterleavedConnection(connection)

        with patch.object(app, "con", interleaved_con):
            with self.assertRaisesRegex(ValueError, "descarga"):
                app.delete_video(video_id, delete_local=False)
        with real_con() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM videos WHERE video_id=?", (video_id,)).fetchone()["status"],
                "pending",
            )

    def test_delete_video_unlinks_a_symlink_without_following_it(self):
        category = app.create_category("Enlace")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        outside = Path(self.tmp.name).parent / "objetivo-protegido.mp4"
        outside.write_bytes(b"protegido")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        link = app.VIDEOS / "enlace.mp4"
        link.symlink_to(outside)
        with app.con() as c:
            c.execute("UPDATE videos SET filename=? WHERE video_id=?", (link.name, "dQw4w9WgXcQ"))
        result = app.delete_video("dQw4w9WgXcQ", delete_local=True)
        self.assertTrue(result["local_deleted"])
        self.assertFalse(link.exists())
        self.assertTrue(outside.is_file())

    def test_delete_video_refuses_a_symlinked_library_root(self):
        category = app.create_category("Raíz enlazada")
        video_id = "ROOTLINK001"
        app.add_video_urls(category["id"], video_id)
        outside_dir = Path(self.tmp.name).parent / "yt-vault-protected-dir"
        outside_dir.mkdir(exist_ok=True)
        victim = outside_dir / "victim.mp4"
        victim.write_bytes(b"protegido")
        self.addCleanup(lambda: outside_dir.rmdir() if outside_dir.exists() else None)
        self.addCleanup(lambda: victim.unlink(missing_ok=True))
        linked_root = Path(self.tmp.name) / "videos-link"
        try:
            linked_root.symlink_to(outside_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"El sistema no permite crear symlinks: {exc}")
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (victim.name, video_id))
        with patch.object(app, "VIDEOS", linked_root):
            result = app.delete_video(video_id, delete_local=True)
        self.assertTrue(victim.is_file())
        self.assertTrue(result["file_errors"])

    def test_delete_video_reports_cleanup_errors_after_removing_the_record(self):
        category = app.create_category("Error de disco")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        media = app.VIDEOS / "bloqueado.mp4"
        media.write_bytes(b"video")
        with app.con() as c:
            c.execute("UPDATE videos SET filename=? WHERE video_id=?", (media.name, "dQw4w9WgXcQ"))
        with patch.object(app, "_unlink_library_file", side_effect=OSError("disco ocupado")):
            result = app.delete_video("dQw4w9WgXcQ", delete_local=True)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["file_errors"])
        with app.con() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM videos WHERE video_id=?", ("dQw4w9WgXcQ",)).fetchone())

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

    def test_init_repairs_an_orphan_legacy_category(self):
        with app.con() as c:
            c.execute(
                "INSERT INTO videos(video_id,url,title,status,category_id,category) VALUES(?,?,?,?,?,?)",
                ("dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "Vídeo legado", "done", 99999, "Curso legado"),
            )
        app.init()
        with app.con() as c:
            row = c.execute(
                "SELECT v.category_id,c.name FROM videos v JOIN categories c ON c.id=v.category_id WHERE v.video_id=?",
                ("dQw4w9WgXcQ",),
            ).fetchone()
            membership = c.execute("SELECT count(*) FROM video_categories WHERE video_id=?", ("dQw4w9WgXcQ",)).fetchone()[0]
        self.assertEqual(row["name"], "Curso legado")
        self.assertEqual(membership, 1)

    def test_frontend_exposes_management_forms_and_download_actions(self):
        category = app.create_category("Curso nuevo")
        app.add_video_urls(category["id"], "dQw4w9WgXcQ")
        page = app.landing_page()
        self.assertIn('id="categoryForm"', page)
        self.assertIn('id="videoForm"', page)
        self.assertIn('<option value="" selected disabled>Selecciona una categoría</option>', page)
        self.assertIn('data-download-video="dQw4w9WgXcQ"', page)
        self.assertIn('data-manage-video="dQw4w9WgXcQ"', page)
        self.assertIn('id="videoManageDialog"', page)
        self.assertIn('id="deleteLocal"', page)
        self.assertIn('name="managed_categories"', page)
        self.assertIn("file_errors.join('\\n')", page)
        self.assertIn(f'data-download-category="{category["id"]}"', page)

    def test_json_api_creates_adds_and_queues(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, payload):
            req = request.Request(
                f'http://127.0.0.1:{server.server_port}{path}',
                data=json.dumps(payload).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Origin': f'http://127.0.0.1:{server.server_port}',
                },
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
    def test_json_api_reorganizes_and_deletes_a_video(self):
        first = app.create_category("Primera")
        second = app.create_category("Segunda")
        app.add_video_urls(first["id"], "dQw4w9WgXcQ")
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, payload):
            req = request.Request(
                f'http://127.0.0.1:{server.server_port}{path}',
                data=json.dumps(payload).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'Origin': f'http://127.0.0.1:{server.server_port}',
                },
                method='POST',
            )
            with request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)

        try:
            status, changed = post('/api/videos/categories', {
                'video_id': 'dQw4w9WgXcQ', 'category_ids': [first['id'], second['id']],
            })
            self.assertEqual((status, changed['category_ids']), (200, [first['id'], second['id']]))
            status, deleted = post('/api/videos/delete', {
                'video_id': 'dQw4w9WgXcQ', 'delete_local': False,
            })
            self.assertEqual((status, deleted['deleted']), (200, True))
        finally:
            server.shutdown()
            server.server_close()

    def test_json_api_returns_each_video_once_with_all_categories(self):
        first = app.create_category("Primera")
        second = app.create_category("Segunda")
        app.add_video_urls(first["id"], "dQw4w9WgXcQ")
        app.set_video_categories("dQw4w9WgXcQ", [first["id"], second["id"]])
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with request.urlopen(f'http://127.0.0.1:{server.server_port}/api/videos', timeout=5) as response:
                payload = json.load(response)
            self.assertEqual(len(payload["videos"]), 1)
            self.assertEqual(payload["videos"][0]["category_ids"], [first["id"], second["id"]])
        finally:
            server.shutdown()
            server.server_close()

    def test_mutating_api_rejects_non_json_and_cross_origin_requests(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/categories'
        try:
            plain = request.Request(url, data=b'{"name":"Ataque"}', headers={'Content-Type': 'text/plain'}, method='POST')
            with self.assertRaises(error.HTTPError) as rejected_type:
                request.urlopen(plain, timeout=5)
            self.assertEqual(rejected_type.exception.code, 415)

            foreign = request.Request(
                url,
                data=b'{"name":"Ataque"}',
                headers={'Content-Type': 'application/json', 'Origin': 'https://sitio-ajeno.example'},
                method='POST',
            )
            with self.assertRaises(error.HTTPError) as rejected_origin:
                request.urlopen(foreign, timeout=5)
            self.assertEqual(rejected_origin.exception.code, 403)

            missing_origin = request.Request(
                url,
                data=b'{"name":"Sin origen"}',
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with self.assertRaises(error.HTTPError) as rejected_missing_origin:
                request.urlopen(missing_origin, timeout=5)
            self.assertEqual(rejected_missing_origin.exception.code, 403)

            wrong_scheme = request.Request(
                url,
                data=b'{"name":"Esquema incorrecto"}',
                headers={
                    'Content-Type': 'application/json',
                    'Origin': f'https://127.0.0.1:{server.server_port}',
                },
                method='POST',
            )
            with self.assertRaises(error.HTTPError) as rejected_scheme:
                request.urlopen(wrong_scheme, timeout=5)
            self.assertEqual(rejected_scheme.exception.code, 403)

            same_origin = request.Request(
                url,
                data=b'{"name":"Permitida"}',
                headers={'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{server.server_port}'},
                method='POST',
            )
            with request.urlopen(same_origin, timeout=5) as accepted:
                self.assertEqual(accepted.status, 201)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
