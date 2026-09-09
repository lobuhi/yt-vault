import gc
import hashlib
import io
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import warnings
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app
import downloader


class DownloaderQueueTests(unittest.TestCase):
    def test_windows_subprocess_has_no_console_or_interactive_stdin(self):
        completed = subprocess.CompletedProcess(['tool.exe'], 0, '', '')
        with patch.object(downloader.subprocess, 'run', return_value=completed) as run:
            downloader.run(['tool.exe'], platform='nt')
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertEqual(kwargs['errors'], 'replace')

    def test_log_closes_its_file_handle(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(downloader, 'LOG', Path(tmp) / 'downloader.log'), \
             warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            downloader.log('prueba')
            gc.collect()
        self.assertFalse(
            [item for item in caught if issubclass(item.category, ResourceWarning)],
            caught,
        )

    def test_startup_resumes_an_existing_pending_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(app, 'ROOT', root), \
                 patch.object(app, 'DB', root / 'data' / 'portal.sqlite3'), \
                 patch.object(app, 'VIDEOS', root / 'videos'), \
                 patch.object(app, 'THUMBS', root / 'thumbs'):
                app.init()
                with app.con() as connection:
                    connection.execute(
                        "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                        ('queued12345', 'https://youtu.be/queued12345', 'pending'),
                    )
                with patch.object(app, 'start_downloader') as start:
                    self.assertTrue(app.resume_pending_downloads())
                    start.assert_called_once_with()
                with app.con() as connection:
                    connection.execute("UPDATE videos SET status='done'")
                with patch.object(app, 'start_downloader') as start:
                    self.assertFalse(app.resume_pending_downloads())
                    start.assert_not_called()

    def test_startup_recovers_an_interrupted_running_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(app, 'ROOT', root), \
                 patch.object(app, 'DB', root / 'data' / 'portal.sqlite3'), \
                 patch.object(app, 'VIDEOS', root / 'videos'), \
                 patch.object(app, 'THUMBS', root / 'thumbs'):
                app.init()
                with app.con() as connection:
                    connection.execute(
                        "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                        ('running123', 'https://youtu.be/running123', 'running'),
                    )
                with patch.object(app, 'start_downloader') as start:
                    self.assertTrue(app.resume_pending_downloads())
                    start.assert_called_once_with()
                with app.con() as connection:
                    status = connection.execute(
                        "SELECT status FROM videos WHERE video_id='running123'"
                    ).fetchone()[0]
                self.assertEqual(status, 'pending')

    def test_startup_reserves_server_before_recovering_queue(self):
        source = Path(app.__file__).read_text(encoding='utf-8')
        entrypoint = source[source.index("if __name__ == '__main__':"):]
        self.assertLess(entrypoint.index('ThreadingHTTPServer('), entrypoint.index('init()'))
        self.assertLess(entrypoint.index('init()'), entrypoint.index('resume_pending_downloads()'))
        self.assertIn('server.server_close()', entrypoint)

    def test_queue_signal_while_worker_exits_triggers_another_pass(self):
        entered = threading.Event()
        release = threading.Event()
        second_pass = threading.Event()
        calls = []

        def fake_main():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            else:
                second_pass.set()

        previous = app._downloader_thread
        app._downloader_thread = None
        try:
            with patch.object(downloader, 'main', side_effect=fake_main):
                app.start_downloader()
                self.assertTrue(entered.wait(2))
                app.start_downloader()
                release.set()
                self.assertTrue(second_pass.wait(2), 'se perdió la segunda señal de cola')
                worker = app._downloader_thread
                if worker:
                    worker.join(2)
            self.assertEqual(calls, [1, 2])
        finally:
            release.set()
            app._downloader_thread = previous

    def test_windows_runtime_serves_in_background_and_stops_from_tray(self):
        served = threading.Event()

        class FakeServer:
            def __init__(self):
                self.shutdown_called = False
                self.closed = False

            def serve_forever(self):
                served.set()

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.closed = True

        class FakeTray:
            def __init__(self, url, stop):
                self.url = url
                self.stop = stop

            def run(self):
                self.assertion = served.wait(1)
                self.stop()

        server = FakeServer()
        created = []

        def tray_factory(url, stop):
            tray = FakeTray(url, stop)
            created.append(tray)
            return tray

        app.run_application(server, 'http://127.0.0.1:8802', use_tray=True, tray_factory=tray_factory)
        self.assertTrue(created[0].assertion)
        self.assertEqual(created[0].url, 'http://127.0.0.1:8802')
        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.closed)

    def test_windows_runtime_closes_server_when_tray_construction_fails(self):
        events = []

        class FakeServer:
            def serve_forever(self):
                events.append('serve')

            def shutdown(self):
                events.append('shutdown')

            def server_close(self):
                events.append('close')

        def broken_factory(_url, _stop):
            raise RuntimeError('factory failed')

        with self.assertRaisesRegex(RuntimeError, 'factory failed'):
            app.run_application(
                FakeServer(), 'http://127.0.0.1:8802',
                use_tray=True, tray_factory=broken_factory,
            )
        self.assertIn('shutdown', events)
        self.assertIn('close', events)

    def test_windows_runtime_still_closes_when_shutdown_fails(self):
        events = []

        class FakeServer:
            def serve_forever(self):
                events.append('serve')

            def shutdown(self):
                events.append('shutdown')
                raise RuntimeError('shutdown failed')

            def server_close(self):
                events.append('close')

        class FakeTray:
            def __init__(self, _url, _stop):
                pass

            def run(self):
                events.append('tray')

        with self.assertRaisesRegex(RuntimeError, 'shutdown failed'):
            app.run_application(
                FakeServer(), 'http://127.0.0.1:8802',
                use_tray=True, tray_factory=FakeTray,
            )
        self.assertIn('close', events)

    def test_documentation_defaults_to_english_and_links_spanish(self):
        root = Path(__file__).parent
        english = (root / 'README.md').read_text(encoding='utf-8')
        spanish_path = root / 'README.es.md'
        self.assertTrue(spanish_path.is_file())
        spanish = spanish_path.read_text(encoding='utf-8')
        self.assertIn('[Español](README.es.md)', english)
        self.assertIn('[English](README.md)', spanish)
        self.assertIn('system tray', english.casefold())
        self.assertIn('bandeja del sistema', spanish.casefold())

    def test_windows_build_embeds_brand_icon(self):
        workflow = (Path(__file__).parent / '.github' / 'workflows' / 'build-windows.yml').read_text()
        self.assertIn('--icon "assets/yt-vault.ico"', workflow)
        self.assertTrue((Path(__file__).parent / 'assets' / 'yt-vault.ico').is_file())

    def test_windows_portable_bundle_includes_javascript_runtime(self):
        workflow = (Path(__file__).parent / '.github' / 'workflows' / 'build-windows.yml').read_text()
        self.assertIn('deno-x86_64-pc-windows-msvc.zip', workflow)
        self.assertIn('deno.exe', workflow)
        self.assertIn('Get-FileHash', workflow)
        self.assertIn('Assert-Sha256 "deno.zip" "deno.sha256" ""', workflow)
        self.assertIn('foreach ($line in $lines)', workflow)
        self.assertNotIn('$lines | Select-Object -First 1', workflow)
        self.assertRegex(workflow, r'Compress-Archive[\s\S]*deno\.exe')
    def test_windows_docs_describe_automatic_verified_tool_install(self):
        for filename in ('README.md', 'README.es.md'):
            text = (Path(__file__).parent / filename).read_text(encoding='utf-8').casefold()
            with self.subTest(filename=filename):
                self.assertIn('localappdata', text)
                self.assertIn('sha-256', text)
                self.assertIn('deno', text)
                self.assertIn('autom', text)

    def test_metadata_exception_is_recorded_without_aborting_the_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(downloader, 'ROOT', root), \
                 patch.object(downloader, 'DB', root / 'data' / 'portal.sqlite3'), \
                 patch.object(downloader, 'VIDEOS', root / 'videos'), \
                 patch.object(downloader, 'THUMBS', root / 'thumbs'), \
                 patch.object(downloader, 'TMP', root / 'tmp'), \
                 patch.object(downloader, 'LOG', root / 'logs' / 'downloader.log'):
                downloader.init()
                with downloader.con() as connection:
                    connection.execute(
                        "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                        ('timeout1234', 'https://youtu.be/timeout1234', 'pending'),
                    )
                with patch.object(
                    downloader, 'fetch_metadata', side_effect=subprocess.TimeoutExpired('yt-dlp', 180)
                ):
                    downloader.metadata_phase()
                with downloader.con() as connection:
                    row = connection.execute(
                        "SELECT status,last_metadata_error FROM videos WHERE video_id='timeout1234'"
                    ).fetchone()
                self.assertEqual(row['status'], 'pending')
                self.assertIn('TimeoutExpired', row['last_metadata_error'])

    def test_permanent_error_does_not_delay_the_next_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(downloader, 'ROOT', root), \
                 patch.object(downloader, 'DB', root / 'data' / 'portal.sqlite3'), \
                 patch.object(downloader, 'VIDEOS', root / 'videos'), \
                 patch.object(downloader, 'THUMBS', root / 'thumbs'), \
                 patch.object(downloader, 'TMP', root / 'tmp'), \
                 patch.object(downloader, 'LOG', root / 'logs' / 'downloader.log'):
                downloader.init()
                with downloader.con() as connection:
                    connection.executemany(
                        "INSERT INTO videos(video_id,url,status,priority) VALUES(?,?,?,?)",
                        [
                            ('first_error', 'https://youtu.be/first_error', 'pending', 2),
                            ('second_done', 'https://youtu.be/second_done', 'pending', 1),
                        ],
                    )
                calls = []
                def download(row):
                    calls.append(row['video_id'])
                    if row['video_id'] == 'first_error':
                        raise RuntimeError('fallo permanente')
                    downloader.update(row['video_id'], status='done')
                with patch.object(downloader, 'ensure_runtime_tools'), \
                     patch.object(downloader, 'metadata_phase'), \
                     patch.object(downloader, 'download_one', side_effect=download), \
                     patch.object(downloader.time, 'sleep') as sleep:
                    downloader.main()
                self.assertEqual(calls, ['first_error', 'second_done'])
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [downloader.SUCCESS_DELAY])

    def test_pending_queue_only_contains_explicitly_queued_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'queue.sqlite3'
            with closing(sqlite3.connect(db)) as c:
                with c:
                    c.execute('''CREATE TABLE videos(
                        video_id TEXT PRIMARY KEY, status TEXT, priority INTEGER,
                        category TEXT, attempts INTEGER, title TEXT
                    )''')
                    c.executemany(
                        'INSERT INTO videos VALUES(?,?,?,?,?,?)',
                        [
                            ('queued', 'pending', 0, 'Curso', 0, 'En cola'),
                            ('failed', 'error', 0, 'Curso', 1, 'Fallido'),
                            ('remote', 'remote', 0, 'Curso', 0, 'Remoto'),
                        ],
                    )
            with patch.object(downloader, 'DB', db):
                result = downloader.pending()
            self.assertEqual([row['video_id'] for row in result], ['queued'])

    def test_app_find_tool_bootstraps_missing_tool_in_frozen_windows(self):
        with patch.object(app.shutil, 'which', return_value=None), \
             patch.object(downloader, 'tool', return_value=r'C:\Tools\yt-dlp.exe') as bootstrap:
            found = app.find_tool('yt-dlp', platform='nt', frozen=True)
        self.assertEqual(found, r'C:\Tools\yt-dlp.exe')
        bootstrap.assert_called_once_with('yt-dlp', platform='nt', frozen=True)

    def test_app_frozen_windows_delegates_before_using_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / 'yt-dlp.exe'
            old.write_bytes(b'MZ-old')
            with patch.object(app.shutil, 'which', return_value=str(old)), \
                 patch.object(downloader, 'tool', return_value=r'C:\managed\yt-dlp.exe') as managed:
                result = app.find_tool('yt-dlp', platform='nt', frozen=True)
        self.assertEqual(result, r'C:\managed\yt-dlp.exe')
        managed.assert_called_once_with('yt-dlp', platform='nt', frozen=True)

    @staticmethod
    def make_zip(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, payload in files.items():
                archive.writestr(name, payload)
        return output.getvalue()

    def test_windows_bootstrap_installs_verified_tools_atomically(self):
        yt_dlp = b'MZ-yt-dlp'
        ffmpeg_zip = self.make_zip({
            'ffmpeg-build/bin/ffmpeg.exe': b'MZ-ffmpeg',
            'ffmpeg-build/bin/ffprobe.exe': b'MZ-ffprobe',
        })
        deno_zip = self.make_zip({'deno.exe': b'MZ-deno'})
        urls = {
            'yt_dlp': ('https://example.test/yt-dlp.exe', 'https://example.test/yt.sha256'),
            'ffmpeg': ('https://example.test/ffmpeg.zip', 'https://example.test/ffmpeg.sha256'),
            'deno': ('https://example.test/deno.zip', 'https://example.test/deno.sha256'),
        }
        payloads = {
            urls['yt_dlp'][0]: yt_dlp,
            urls['yt_dlp'][1]: hashlib.sha256(yt_dlp).hexdigest().encode(),
            urls['ffmpeg'][0]: ffmpeg_zip,
            urls['ffmpeg'][1]: hashlib.sha256(ffmpeg_zip).hexdigest().encode(),
            urls['deno'][0]: deno_zip,
            urls['deno'][1]: hashlib.sha256(deno_zip).hexdigest().encode(),
        }

        class Response(io.BytesIO):
            def __init__(self, url):
                super().__init__(payloads[url])
                self.url = url
                self.headers = {'Content-Length': str(len(payloads[url]))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def geturl(self):
                return self.url

        def opener(request, timeout=0):
            self.assertGreater(timeout, 0)
            return Response(request.full_url)

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / 'tools'
            installed = downloader.install_windows_tools(tools, opener=opener, urls=urls)
            self.assertEqual((tools / 'yt-dlp.exe').read_bytes(), yt_dlp)
            self.assertEqual((tools / 'ffmpeg.exe').read_bytes(), b'MZ-ffmpeg')
            self.assertEqual((tools / 'ffprobe.exe').read_bytes(), b'MZ-ffprobe')
            self.assertEqual((tools / 'deno.exe').read_bytes(), b'MZ-deno')
            self.assertEqual(set(installed), {'yt-dlp', 'ffmpeg', 'ffprobe', 'deno'})
            self.assertFalse(any(path.name.endswith('.part') for path in tools.iterdir()))

    def test_windows_installer_can_fetch_only_the_requested_component(self):
        deno_payload = self.make_zip({'deno.exe': b'MZ-deno'})
        deno_sha = hashlib.sha256(deno_payload).hexdigest().encode()
        responses = {
            'https://example.test/deno.zip': deno_payload,
            'https://example.test/deno.sha': deno_sha,
        }
        urls = {
            'yt_dlp': ('https://example.test/yt-dlp.exe', 'https://example.test/yt-dlp.sha'),
            'ffmpeg': ('https://example.test/ffmpeg.zip', 'https://example.test/ffmpeg.sha'),
            'deno': ('https://example.test/deno.zip', 'https://example.test/deno.sha'),
        }

        class Response(io.BytesIO):
            headers = {}
            def __init__(self, url):
                super().__init__(responses[url]); self.url = url
            def geturl(self): return self.url

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / 'tools'
            installed = downloader.install_windows_tools(
                tools,
                opener=lambda request, timeout=0: Response(request.full_url),
                urls=urls,
                required={'deno'},
            )
            self.assertEqual(installed, ['deno'])
            self.assertEqual((tools / 'deno.exe').read_bytes(), b'MZ-deno')
            self.assertFalse((tools / 'yt-dlp.exe').exists())
            self.assertFalse((tools / 'ffmpeg.exe').exists())

    def test_missing_ffprobe_repairs_the_ffmpeg_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / 'tools'; binary = root / 'portable'
            tools.mkdir(); binary.mkdir()
            (tools / 'ffmpeg.exe').write_bytes(b'MZ-ffmpeg')

            def repair(*, required):
                self.assertEqual(required, {'ffmpeg'})
                (tools / 'ffprobe.exe').write_bytes(b'MZ-ffprobe')

            with patch.object(downloader, 'TOOLS', tools), \
                 patch.object(downloader, 'BINARY_DIR', binary), \
                 patch.object(downloader.shutil, 'which', return_value=None), \
                 patch.object(downloader, 'install_windows_tools', side_effect=repair) as installer:
                found = downloader.tool('ffprobe', platform='nt', frozen=True)
        self.assertEqual(found, str(tools / 'ffprobe.exe'))
        installer.assert_called_once_with(required={'ffmpeg'})

    def test_runtime_validation_requires_ffprobe_alongside_ffmpeg(self):
        with patch.object(downloader, 'ytdlp') as ytdlp, \
             patch.object(downloader, 'ffmpeg') as ffmpeg, \
             patch.object(downloader, 'ffprobe', create=True) as ffprobe, \
             patch.object(downloader, 'javascript_runtime') as javascript_runtime:
            downloader.ensure_runtime_tools(platform='nt', frozen=True)
        ytdlp.assert_called_once_with()
        ffmpeg.assert_called_once_with()
        ffprobe.assert_called_once_with()
        javascript_runtime.assert_called_once_with()

    def test_non_frozen_installation_does_not_require_javascript_runtime(self):
        with patch.object(downloader, 'ytdlp') as ytdlp, \
             patch.object(downloader, 'ffmpeg') as ffmpeg, \
             patch.object(downloader, 'ffprobe') as ffprobe, \
             patch.object(downloader, 'javascript_runtime') as javascript_runtime:
            downloader.ensure_runtime_tools(platform='posix', frozen=False)
        ytdlp.assert_called_once_with()
        ffmpeg.assert_called_once_with()
        ffprobe.assert_called_once_with()
        javascript_runtime.assert_not_called()

    def test_https_download_rejects_an_initial_http_url(self):
        called = False
        def opener(*_args, **_kwargs):
            nonlocal called
            called = True
            self.fail('HTTP URL reached opener')
        with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
            downloader._open_https(opener, 'http://example.test/tool.exe')
        self.assertFalse(called)

    def test_https_redirect_handler_rejects_an_http_hop(self):
        import urllib.request
        handler = downloader.HTTPSOnlyRedirectHandler()
        request = urllib.request.Request('https://example.test/tool.exe')
        with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
            handler.redirect_request(
                request, None, 302, 'Found', {}, 'http://mirror.test/tool.exe'
            )

    def test_frozen_windows_prefers_managed_tool_over_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / 'tools'; binary = root / 'portable'; path_dir = root / 'path'
            for directory in (tools, binary, path_dir):
                directory.mkdir(parents=True)
                (directory / 'yt-dlp.exe').write_bytes(b'MZ')
            with patch.object(downloader, 'TOOLS', tools), \
                 patch.object(downloader, 'BINARY_DIR', binary), \
                 patch.object(downloader.shutil, 'which', return_value=str(path_dir / 'yt-dlp.exe')):
                selected = downloader.tool('yt-dlp', platform='nt', frozen=True)
            self.assertEqual(selected, str(tools / 'yt-dlp.exe'))

    def test_windows_bootstrap_rejects_bad_checksum_without_installing(self):
        payload = b'not-the-expected-file'
        urls = {'yt_dlp': ('https://example.test/tool.exe', 'https://example.test/tool.sha256')}

        class Response(io.BytesIO):
            headers = {}

            def __init__(self, url):
                super().__init__(b'0' * 64 if url.endswith('.sha256') else payload)
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def geturl(self):
                return self.url

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / 'tools'
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                downloader.install_windows_tools(
                    tools,
                    opener=lambda request, timeout=0: Response(request.full_url),
                    urls=urls,
                )
            self.assertFalse((tools / 'yt-dlp.exe').exists())

    def test_common_uses_discovered_javascript_runtime_not_linux_path(self):
        paths = {
            'yt-dlp': r'C:\Tools\yt-dlp.exe',
            'deno': r'C:\Tools\deno.exe',
        }
        with patch.object(downloader, 'tool', side_effect=lambda name: paths[name]):
            command = downloader.common()
        runtime = command[command.index('--js-runtimes') + 1]
        self.assertEqual(runtime, r'deno:C:\Tools\deno.exe')
        self.assertNotIn('/usr/bin/node', command)
        self.assertNotIn('--extractor-args', command)
        self.assertNotIn('--remote-components', command)

    def test_javascript_runtime_falls_back_after_installer_error(self):
        with patch.object(
            downloader, 'tool', side_effect=[zipfile.BadZipFile('Deno corrupto'), r'C:\Tools\node.exe']
        ):
            self.assertEqual(downloader.javascript_runtime(), ('node', r'C:\Tools\node.exe'))

    def test_bootstrap_failure_does_not_fail_items_queued_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(downloader, 'ROOT', root), \
                 patch.object(downloader, 'DB', root / 'data' / 'portal.sqlite3'), \
                 patch.object(downloader, 'VIDEOS', root / 'videos'), \
                 patch.object(downloader, 'THUMBS', root / 'thumbs'), \
                 patch.object(downloader, 'TMP', root / 'tmp'), \
                 patch.object(downloader, 'LOG', root / 'logs' / 'downloader.log'):
                downloader.init()
                with downloader.con() as connection:
                    connection.execute(
                        "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                        ('initial1234', 'https://youtu.be/initial1234', 'pending'),
                    )
                def fail_after_new_item():
                    with downloader.con() as connection:
                        connection.execute(
                            "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                            ('later123456', 'https://youtu.be/later123456', 'pending'),
                        )
                    raise RuntimeError('red caída')
                with patch.object(downloader, 'ensure_runtime_tools', side_effect=fail_after_new_item):
                    with self.assertRaisesRegex(RuntimeError, 'red caída'):
                        downloader.main()
                with downloader.con() as connection:
                    states = dict(connection.execute('SELECT video_id,status FROM videos'))
                self.assertEqual(states['initial1234'], 'error')
                self.assertEqual(states['later123456'], 'pending')

    def test_bootstrap_failure_marks_pending_rows_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = (
                patch.object(downloader, 'ROOT', root),
                patch.object(downloader, 'DB', root / 'data' / 'portal.sqlite3'),
                patch.object(downloader, 'VIDEOS', root / 'videos'),
                patch.object(downloader, 'THUMBS', root / 'thumbs'),
                patch.object(downloader, 'TMP', root / 'tmp'),
                patch.object(downloader, 'LOG', root / 'logs' / 'downloader.log'),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                downloader.init()
                with downloader.con() as connection:
                    connection.execute(
                        "INSERT INTO videos(video_id,url,status) VALUES(?,?,?)",
                        ('queued01', 'https://youtu.be/queued01', 'pending'),
                    )
                with patch.object(
                    downloader, 'ensure_runtime_tools',
                    side_effect=RuntimeError('No se pudo instalar yt-dlp'),
                ):
                    with self.assertRaisesRegex(RuntimeError, 'instalar yt-dlp'):
                        downloader.main()
                with downloader.con() as connection:
                    row = connection.execute(
                        "SELECT status,error FROM videos WHERE video_id='queued01'"
                    ).fetchone()
                self.assertEqual(row['status'], 'error')
                self.assertIn('instalar yt-dlp', row['error'])


if __name__ == '__main__':
    unittest.main()
