import contextlib
import inspect
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import types
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

    def test_app_windows_subprocess_options_hide_console(self):
        options = app.subprocess_options(platform='nt')
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
        self.assertEqual(options['encoding'], 'utf-8')
        self.assertEqual(options['errors'], 'replace')

    def test_playlist_rejects_option_injection_and_foreign_hosts(self):
        for value in (
            '--exec-before-download=calc.exe&rem?list=PLattack',
            'https://evil.example/playlist?list=PLattack',
            'http://www.youtube.com/playlist?list=PLattack',
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'YouTube'):
                app.expand_youtube_inputs(value, playlist_loader=lambda _url: self.fail('loader called'))

    def test_playlist_command_terminates_options_before_url(self):
        completed = types.SimpleNamespace(returncode=0, stdout='{"entries":[{"id":"M7lc1UVf-VE"}]}', stderr='')
        with patch.object(app, 'find_tool', return_value='yt-dlp'), \
             patch.object(app.subprocess, 'run', return_value=completed) as run:
            app.load_playlist('https://www.youtube.com/playlist?list=PLsafe12345')
        self.assertEqual(run.call_args.args[0][-2:], ['--', 'https://www.youtube.com/playlist?list=PLsafe12345'])

    def test_video_can_belong_to_multiple_categories(self):
        first = app.create_category("Tafsir")
        second = app.create_category("Ramadán")
        app.add_video_urls(first["id"], "dQw4w9WgXcQ")
        result = app.add_video_urls(second["id"], "dQw4w9WgXcQ")
        self.assertEqual(result["existing"], 1)
        self.assertEqual(app.video_category_ids("dQw4w9WgXcQ"), [first["id"], second["id"]])
        page = app.landing_page()
        self.assertEqual(page.count('data-manage-video="dQw4w9WgXcQ"'), 2)
        self.assertIn('<b>1</b>Total', page)

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
        self.assertTrue(result["local_deleted"], result)
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
        self.assertTrue(result["local_deleted"], result)
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

    @unittest.skipIf(os.name == 'nt', 'La variante Windows usa handles WinAPI.')
    def test_delete_video_detects_root_replacement_between_validation_and_open(self):
        category = app.create_category("Raíz sustituida")
        video_id = "ROOTSWAP001"
        app.add_video_urls(category["id"], video_id)
        original_media = app.VIDEOS / "victim.mp4"
        original_media.write_bytes(b"original")
        replacement = Path(self.tmp.name) / "replacement-videos"
        replacement.mkdir()
        replacement_media = replacement / original_media.name
        replacement_media.write_bytes(b"no borrar")
        moved_original = Path(self.tmp.name) / "original-videos"
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (original_media.name, video_id))

        real_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path) == app.VIDEOS:
                app.VIDEOS.rename(moved_original)
                replacement.rename(app.VIDEOS)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with patch.object(app.os, 'open', side_effect=swapping_open):
            result = app.delete_video(video_id, delete_local=True)
        self.assertTrue(result['file_errors'])
        self.assertTrue((app.VIDEOS / original_media.name).is_file())
        self.assertTrue((moved_original / original_media.name).is_file())

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
            self.assertGreater(c.execute('SELECT count(*) FROM file_cleanup_queue').fetchone()[0], 0)
        retried = app.retry_file_cleanup()
        self.assertTrue(retried['local_deleted'], retried)
        self.assertFalse(media.exists())
        with app.con() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM file_cleanup_queue').fetchone()[0], 0)

    def test_pending_cleanup_never_deletes_a_file_reused_by_a_new_record(self):
        category = app.create_category('Reutilización segura')
        video_id = 'dQw4w9WgXcQ'
        app.add_video_urls(category['id'], video_id)
        media = app.VIDEOS / 'reused.mp4'
        media.write_bytes(b'original')
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, video_id))
        with patch.object(app, '_unlink_library_file', side_effect=OSError('ocupado')):
            app.delete_video(video_id, delete_local=True)
        app.add_video_urls(category['id'], video_id)
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, video_id))
        retried = app.retry_file_cleanup()
        self.assertTrue(retried['file_errors'])
        self.assertTrue(media.is_file())
        with app.con() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM file_cleanup_queue').fetchone()[0], 0)

    def test_pending_cleanup_cancels_case_and_win32_filename_aliases(self):
        category = app.create_category('Alias Windows')
        old_video = 'dQw4w9WgXcQ'
        new_video = 'abcdefghijk'
        app.add_video_urls(category['id'], old_video)
        media = app.VIDEOS / 'Movie.mp4'
        media.write_bytes(b'original')
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, old_video))
        with patch.object(app, '_unlink_library_file', side_effect=OSError('ocupado')):
            app.delete_video(old_video, delete_local=True)
        app.add_video_urls(category['id'], new_video)
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", ('ＭＯＶＩＥ.MP4. ', new_video))
        retried = app.retry_file_cleanup()
        self.assertTrue(retried['file_errors'])
        self.assertTrue(media.is_file())
        with app.con() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM file_cleanup_queue').fetchone()[0], 0)

    def test_cleanup_serializes_filename_reuse_with_unlink(self):
        category = app.create_category('Carrera de limpieza')
        video_id = 'dQw4w9WgXcQ'
        app.add_video_urls(category['id'], video_id)
        media = app.VIDEOS / 'same.mp4'
        media.write_bytes(b'old')
        with app.con() as c:
            c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, video_id))
        with patch.object(app, '_unlink_library_file', side_effect=OSError('ocupado')):
            app.delete_video(video_id, delete_local=True)
        with app.con() as c:
            c.execute("DELETE FROM file_cleanup_queue WHERE root_kind='thumbs'")

        writer_started = threading.Event()
        writer_done = threading.Event()

        def reuse_filename():
            writer_started.set()
            app.add_video_urls(category['id'], video_id)
            with app.con() as c:
                c.execute("UPDATE videos SET filename=?, status='done' WHERE video_id=?", (media.name, video_id))
            media.write_bytes(b'new')
            writer_done.set()

        real_unlink = app._unlink_library_file
        writer = None

        def unlink_while_writer_waits(root, filename):
            nonlocal writer
            writer = threading.Thread(target=reuse_filename)
            writer.start()
            self.assertTrue(writer_started.wait(1))
            self.assertFalse(writer_done.wait(0.1))
            return real_unlink(root, filename)

        with patch.object(app, '_unlink_library_file', side_effect=unlink_while_writer_waits):
            cleaned = app.retry_file_cleanup()
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertTrue(cleaned['local_deleted'], cleaned)
        self.assertEqual(media.read_bytes(), b'new')

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

    def test_init_migrates_a_legacy_schema_without_priority_or_category_tables(self):
        app.DB.unlink()
        with contextlib.closing(sqlite3.connect(app.DB)) as c, c:
            c.execute('''CREATE TABLE videos(
                video_id TEXT PRIMARY KEY, url TEXT NOT NULL, source TEXT,
                title TEXT, filename TEXT, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                duration REAL, filesize INTEGER
            )''')
            c.execute(
                "INSERT INTO videos(video_id,url,source,title,status) VALUES(?,?,?,?,?)",
                ('dQw4w9WgXcQ', 'https://youtu.be/dQw4w9WgXcQ', 'legado', 'Vídeo legado', 'done'),
            )
        app.init()
        with app.con() as c:
            columns = {row[1] for row in c.execute('PRAGMA table_info(videos)')}
            memberships = c.execute('SELECT video_id,category_id FROM video_categories').fetchall()
            self.assertIn('priority', columns)
            self.assertEqual(len(memberships), 1)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_frontend_uses_the_redesigned_responsive_components(self):
        css = app.base_css()
        page = app.landing_page('en')
        self.assertIn('--accent:#1ed760', css)
        self.assertIn('.language-switch', css)
        self.assertIn('.stat', css)
        self.assertIn('.search-wrap', css)
        self.assertIn('@media(max-width:650px)', css)
        self.assertIn('class="brand-mark"', page)
        self.assertIn('class="stat"', page)

    def test_frontend_can_render_english_and_spanish(self):
        category = app.create_category('Language course')
        app.add_video_urls(category['id'], 'dQw4w9WgXcQ')
        english = app.landing_page('en')
        spanish = app.landing_page('es')
        watch = app.watch_page('dQw4w9WgXcQ', 'en')
        self.assertIn('<html lang="en">', english)
        self.assertIn('Video library', english)
        self.assertIn('Search videos', english)
        self.assertIn('Manage library', english)
        self.assertIn('Manage / delete', english)
        self.assertIn('Español', english)
        self.assertIn('<html lang="es">', spanish)
        self.assertIn('Gestionar videoteca', spanish)
        self.assertIn('Back to the library', watch)

    def test_language_switch_has_distinct_mobile_labels_and_current_page(self):
        css = app.base_css()
        english = app.landing_page('en')
        spanish = app.landing_page('es')
        self.assertIn('content:attr(data-short)', css)
        self.assertNotIn('::first-letter', css)
        self.assertIn('data-short="ES"', english)
        self.assertIn('data-short="EN" aria-current="page"', english)
        self.assertIn('data-short="ES" aria-current="page"', spanish)

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

    def test_watch_page_shows_all_video_categories(self):
        first = app.create_category('Primera categoría')
        second = app.create_category('Segunda categoría')
        app.add_video_urls(first['id'], 'dQw4w9WgXcQ')
        app.set_video_categories('dQw4w9WgXcQ', [first['id'], second['id']])
        page = app.watch_page('dQw4w9WgXcQ')
        self.assertIn('Primera categoría, Segunda categoría', page)

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

    def test_http_language_query_renders_english(self):
        app.init()
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with request.urlopen(f'http://127.0.0.1:{server.server_port}/?lang=en', timeout=5) as response:
                page = response.read().decode()
            self.assertIn('<html lang="en">', page)
            self.assertIn('Video library', page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

            rebinding = request.Request(
                url,
                data=b'{"name":"Rebinding"}',
                headers={
                    'Content-Type': 'application/json',
                    'Host': 'attacker.example',
                    'Origin': 'http://attacker.example',
                },
                method='POST',
            )
            with self.assertRaises(error.HTTPError) as rejected_rebinding:
                request.urlopen(rebinding, timeout=5)
            self.assertEqual(rejected_rebinding.exception.code, 403)

            non_object = request.Request(
                url,
                data=b'[]',
                headers={
                    'Content-Type': 'application/json',
                    'Origin': f'http://127.0.0.1:{server.server_port}',
                },
                method='POST',
            )
            with self.assertRaises(error.HTTPError) as rejected_shape:
                request.urlopen(non_object, timeout=5)
            self.assertEqual(rejected_shape.exception.code, 400)

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


class FakeWin32Function:
    def __init__(self, implementation=lambda *args: 1):
        self.implementation = implementation
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.implementation(*args)


class FakeWin32:
    def __init__(
        self, event, *, failing_api=None, false_api=None, post_quit_fails=False,
        track_command=0,
    ):
        self.events = list(event) if isinstance(event, list) else [event]
        self.quit_requested = False
        self.post_quit_fails = post_quit_fails
        self.callback = None
        self.dispatch_results = []

        def register(window_class):
            self.callback = window_class._obj.lpfnWndProc
            return 1

        def get_message(message, *_args):
            if not self.events:
                return 0
            hwnd, win_message, wparam, lparam = self.events.pop(0)
            msg = message._obj
            msg.hWnd = hwnd
            msg.message = win_message
            msg.wParam = wparam
            msg.lParam = lparam
            return 1

        def dispatch(message):
            msg = message._obj
            result = self.callback(msg.hWnd, msg.message, msg.wParam, msg.lParam)
            self.dispatch_results.append(result)
            return result

        def post_quit(_code):
            if self.post_quit_fails:
                raise RuntimeError('PostQuitMessage failed')
            self.quit_requested = True

        def maybe_fail(name, value=1):
            def implementation(*_args):
                if failing_api == name:
                    raise RuntimeError(f'{name} failed')
                if false_api == name:
                    return 0
                return value
            return implementation

        user32_names = (
            'RegisterClassW', 'UnregisterClassW', 'CreateWindowExW',
            'DefWindowProcW', 'CreatePopupMenu', 'AppendMenuW', 'GetCursorPos',
            'SetForegroundWindow', 'TrackPopupMenu', 'DestroyMenu',
            'DestroyWindow', 'PostQuitMessage', 'GetMessageW',
            'TranslateMessage', 'DispatchMessageW', 'LoadImageW', 'DestroyIcon',
        )
        self.user32 = types.SimpleNamespace(**{
            name: FakeWin32Function() for name in user32_names
        })
        self.user32.RegisterClassW.implementation = register
        self.user32.CreateWindowExW.implementation = lambda *_args: 100
        self.user32.DefWindowProcW.implementation = maybe_fail('DefWindowProcW', 37)
        self.user32.CreatePopupMenu.implementation = lambda: 200
        self.user32.GetCursorPos.implementation = maybe_fail('GetCursorPos')
        self.user32.TrackPopupMenu.implementation = lambda *_args: track_command
        self.user32.PostQuitMessage.implementation = post_quit
        self.user32.GetMessageW.implementation = get_message
        self.user32.DispatchMessageW.implementation = dispatch
        self.user32.LoadImageW.implementation = lambda *_args: 300
        for name in ('UnregisterClassW', 'DestroyMenu', 'DestroyWindow', 'DestroyIcon'):
            getattr(self.user32, name).implementation = maybe_fail(name)

        def shell_notify(command, *_args):
            if failing_api == 'Shell_NotifyIconW':
                raise RuntimeError('Shell_NotifyIconW failed')
            if false_api == 'Shell_NotifyIconW' and command == 2:
                return 0
            return 1

        self.shell32 = types.SimpleNamespace(
            ExtractIconExW=FakeWin32Function(lambda *_args: 0),
            Shell_NotifyIconW=FakeWin32Function(shell_notify),
        )
        self.kernel32 = types.SimpleNamespace(
            GetModuleHandleW=FakeWin32Function(lambda *_args: 1),
        )
        self.windll = types.SimpleNamespace(
            user32=self.user32, shell32=self.shell32, kernel32=self.kernel32,
        )


class WindowsTrayRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = inspect.getsource(app.WindowsTray.run)

    def test_win32_api_signatures_are_complete_and_pointer_safe(self):
        APIs = (
            'GetModuleHandleW', 'RegisterClassW', 'UnregisterClassW',
            'CreateWindowExW', 'DefWindowProcW', 'CreatePopupMenu',
            'AppendMenuW', 'GetCursorPos', 'SetForegroundWindow',
            'TrackPopupMenu', 'DestroyMenu', 'DestroyWindow',
            'PostQuitMessage', 'GetMessageW', 'TranslateMessage',
            'DispatchMessageW', 'LoadImageW', 'DestroyIcon',
            'ExtractIconExW', 'Shell_NotifyIconW',
        )
        for api in APIs:
            with self.subTest(api=api):
                self.assertIn(f'{api}.argtypes =', self.source)
                self.assertIn(f'{api}.restype =', self.source)
        self.assertIn(
            'wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR',
            self.source,
        )
        self.assertIn('wintypes.HWND, ctypes.POINTER(wintypes.RECT)', self.source)
        self.assertIn('user32.TrackPopupMenu.restype = wintypes.BOOL', self.source)

    def test_message_error_is_not_treated_as_normal_shutdown(self):
        error_check = self.source.index('if result == -1:')
        winerror = self.source.index('raise ctypes.WinError()', error_check)
        normal_shutdown = self.source.index('if result == 0:', winerror)
        self.assertLess(error_check, winerror)
        self.assertLess(winerror, normal_shutdown)

    def test_lifetime_cleanup_is_centralized_and_idempotent(self):
        finally_block = self.source.rsplit('        finally:', 1)[1]
        expected_cleanup = (
            'remove_tray_icon()', 'destroy_window()', 'release_owned_icons()',
            'cleanup_bool(user32.UnregisterClassW, class_name, instance)',
        )
        positions = [finally_block.index(call) for call in expected_cleanup]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('self._nid = None', self.source)
        self.assertIn('self._hwnd = None', self.source)
        self.assertIn('self._owned_icons = []', self.source)
        self.assertEqual(self.source.count('cleanup_bool(user32.DestroyIcon'), 1)
        self.assertLess(
            self.source.index('if not shell32.Shell_NotifyIconW(NIM_ADD'),
            self.source.index('self._nid = nid'),
        )

    def test_popup_menu_has_unconditional_release(self):
        show_menu = self.source[
            self.source.index('        def show_menu'):
            self.source.index('        @WNDPROC')
        ]
        self.assertIn('finally:', show_menu)
        self.assertIn('user32.DestroyMenu(menu)', show_menu)

    def run_tray_event(self, event, stop=None, **fake_options):
        import ctypes

        fake = FakeWin32(event, **fake_options)
        tray = app.WindowsTray('http://127.0.0.1', stop or (lambda: None))
        with (
            patch.object(ctypes, 'windll', fake.windll, create=True),
            patch.object(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE, create=True),
        ):
            tray.run()
        return fake

    def test_cleanup_false_results_are_reported(self):
        import ctypes

        for api, event in (
            ('DestroyWindow', (100, 0x0111, 0, 0)),
            ('Shell_NotifyIconW', (100, 0x0111, 0, 0)),
            ('UnregisterClassW', (100, 0x0111, 0, 0)),
            ('DestroyMenu', (100, 0x8001, 0, 0x0205)),
        ):
            with self.subTest(api=api), \
                 patch.object(ctypes, 'WinError', return_value=OSError(f'{api} returned FALSE'), create=True), \
                 self.assertRaisesRegex(OSError, f'{api} returned FALSE'):
                self.run_tray_event(event, false_api=api)

    def test_cleanup_false_does_not_mask_callback_error(self):
        import ctypes

        callback_error = RuntimeError('browser failed')
        with patch.object(app.webbrowser, 'open', side_effect=callback_error), \
             patch.object(ctypes, 'WinError', return_value=OSError('cleanup failed'), create=True), \
             self.assertRaises(RuntimeError) as caught:
            self.run_tray_event(
                (100, 0x8001, 0, 0x0203), false_api='DestroyWindow',
            )
        self.assertIs(caught.exception, callback_error)

    def test_cleanup_helpers_check_all_bool_results(self):
        for call in (
            'shell32.Shell_NotifyIconW, NIM_DELETE',
            'user32.DestroyWindow, current',
            'user32.DestroyIcon, owned_icon',
            'user32.UnregisterClassW, class_name, instance',
        ):
            with self.subTest(call=call):
                self.assertIn(f'cleanup_bool({call}', self.source)
        self.assertIn('if not user32.DestroyMenu(menu):', self.source)

    def test_open_failure_is_rethrown_after_a_deterministic_callback_return(self):
        failure = RuntimeError('browser failed')
        with patch.object(app.webbrowser, 'open', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                self.run_tray_event((100, 0x8001, 0, 0x0203))
        self.assertIs(caught.exception, failure)

    def test_show_menu_and_default_window_proc_failures_are_rethrown(self):
        cases = (
            ((100, 0x8001, 0, 0x0205), 'GetCursorPos'),
            ((100, 0x1234, 0, 0), 'DefWindowProcW'),
        )
        for event, failing_api in cases:
            with self.subTest(failing_api=failing_api):
                with self.assertRaisesRegex(RuntimeError, f'{failing_api} failed'):
                    self.run_tray_event(event, failing_api=failing_api)

    def test_callback_error_signaling_is_best_effort(self):
        failure = RuntimeError('browser failed')
        fake = FakeWin32((100, 0x8001, 0, 0x0203), post_quit_fails=True)
        tray = app.WindowsTray('http://127.0.0.1', lambda: None)
        import ctypes
        with (
            patch.object(ctypes, 'windll', fake.windll, create=True),
            patch.object(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE, create=True),
            patch.object(app.webbrowser, 'open', side_effect=failure),
            self.assertRaises(RuntimeError) as caught,
        ):
            tray.run()
        self.assertIs(caught.exception, failure)
        self.assertEqual(fake.dispatch_results, [0])
        self.assertEqual(len(fake.user32.PostQuitMessage.calls), 1)

    def test_only_the_first_callback_failure_is_rethrown(self):
        first_failure = RuntimeError('first callback failure')
        events = [
            (100, 0x8001, 0, 0x0203),
            (100, 0x1234, 0, 0),
        ]
        with patch.object(app.webbrowser, 'open', side_effect=first_failure):
            with self.assertRaises(RuntimeError) as caught:
                self.run_tray_event(events, failing_api='DefWindowProcW')
        self.assertIs(caught.exception, first_failure)

    def test_exit_command_leaves_server_shutdown_to_run_application(self):
        stops = []
        fake = self.run_tray_event(
            (100, 0x8001, 0, 0x0205), stops.append, track_command=1002,
        )
        self.assertTrue(fake.quit_requested)
        self.assertEqual(fake.dispatch_results, [0])
        self.assertEqual(stops, [])


if __name__ == "__main__":
    unittest.main()
