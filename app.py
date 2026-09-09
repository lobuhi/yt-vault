#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, 'frozen', False))
BINARY_DIR = Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR
if FROZEN:
    default_root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'YTVault'
else:
    default_root = SOURCE_DIR
ROOT = Path(os.environ.get('VIDEOTECA_HOME', default_root)).expanduser().resolve()
DB = ROOT / 'data' / 'portal.sqlite3'
VIDEOS = ROOT / 'videos'
THUMBS = ROOT / 'thumbs'
_num_re = re.compile(r'(\d+)')
_video_id_re = re.compile(r'^[A-Za-z0-9_-]{6,20}$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos(
  video_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  source TEXT,
  title TEXT,
  filename TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  duration REAL,
  filesize INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,
  category TEXT,
  category_id INTEGER,
  upload_date TEXT,
  metadata_attempts INTEGER NOT NULL DEFAULT 0,
  last_metadata_error TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS video_categories(
  video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(video_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_video_categories_category ON video_categories(category_id, video_id);
CREATE TABLE IF NOT EXISTS file_cleanup_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  root_kind TEXT NOT NULL CHECK(root_kind IN ('videos','thumbs')),
  filename TEXT NOT NULL,
  filename_key TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(root_kind, filename_key)
);
CREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(priority DESC, title COLLATE NOCASE);
"""


def natural_key(value):
    normalized = re.sub(r'\s+', ' ', str(value or '')).strip()
    return tuple(int(part) if part.isdigit() else part.casefold() for part in _num_re.split(normalized))


@contextlib.contextmanager
def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=5000')
    try:
        with c:
            yield c
    finally:
        c.close()


def ensure_cols(c):
    cols = {r[1] for r in c.execute('pragma table_info(videos)')}
    for name, typ, default in [
        ('priority', 'INTEGER', '0'), ('category', 'TEXT', 'NULL'),
        ('category_id', 'INTEGER', 'NULL'),
        ('upload_date', 'TEXT', 'NULL'), ('metadata_attempts', 'INTEGER', '0'),
        ('last_metadata_error', 'TEXT', 'NULL'),
    ]:
        if name not in cols:
            c.execute(f'ALTER TABLE videos ADD COLUMN {name} {typ} DEFAULT {default}')


def init():
    (ROOT / 'data').mkdir(parents=True, exist_ok=True)
    VIDEOS.mkdir(parents=True, exist_ok=True)
    THUMBS.mkdir(parents=True, exist_ok=True)
    with con() as c:
        try:
            c.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            if 'priority' not in str(e):
                raise
        ensure_cols(c)
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(priority DESC, title COLLATE NOCASE)')
        c.execute('''UPDATE videos SET category_id=NULL
                     WHERE category_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM categories WHERE id=videos.category_id)''')
        for video in c.execute('SELECT * FROM videos WHERE category_id IS NULL').fetchall():
            name = display_group(video)
            c.execute('INSERT OR IGNORE INTO categories(name) VALUES(?)', (name,))
            category_id = c.execute('SELECT id FROM categories WHERE name=? COLLATE NOCASE', (name,)).fetchone()['id']
            c.execute('UPDATE videos SET category_id=? WHERE video_id=?', (category_id, video['video_id']))
        c.execute('''INSERT OR IGNORE INTO video_categories(video_id, category_id)
                     SELECT video_id, category_id FROM videos WHERE category_id IS NOT NULL''')
    retry_file_cleanup()


def create_category(name):
    clean = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not clean:
        raise ValueError('El nombre de la categoría es obligatorio.')
    try:
        with con() as c:
            cur = c.execute('INSERT INTO categories(name) VALUES(?)', (clean,))
            row = c.execute('SELECT * FROM categories WHERE id=?', (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError('Ya existe una categoría con ese nombre.') from exc
    return dict(row)


def list_categories():
    with con() as c:
        result = [dict(row) for row in c.execute('SELECT * FROM categories').fetchall()]
    return sorted(result, key=lambda row: group_order(row['name']))


def parse_video_id(value):
    value = str(value or '').strip()
    if _video_id_re.fullmatch(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.casefold().removeprefix('www.')
    video_id = ''
    if host in ('youtube.com', 'm.youtube.com', 'music.youtube.com'):
        video_id = urllib.parse.parse_qs(parsed.query).get('v', [''])[0]
        if not video_id:
            parts = [part for part in parsed.path.split('/') if part]
            if len(parts) >= 2 and parts[0] in ('shorts', 'embed', 'live'):
                video_id = parts[1]
    elif host == 'youtu.be':
        video_id = parsed.path.strip('/').split('/')[0]
    return video_id if _video_id_re.fullmatch(video_id or '') else None


def canonical_playlist_url(value):
    parsed = urllib.parse.urlparse(str(value or '').strip())
    host = (parsed.hostname or '').casefold().removeprefix('www.')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('URL de playlist de YouTube no válida.') from exc
    allowed_hosts = {'youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}
    playlist_id = urllib.parse.parse_qs(parsed.query).get('list', [''])[0]
    if (
        parsed.scheme.casefold() != 'https'
        or host not in allowed_hosts
        or port not in (None, 443)
        or not re.fullmatch(r'[A-Za-z0-9_-]{10,128}', playlist_id)
    ):
        raise ValueError('URL de playlist de YouTube no válida.')
    return 'https://www.youtube.com/playlist?' + urllib.parse.urlencode({'list': playlist_id})


def subprocess_options(platform=None):
    platform = platform or os.name
    return {
        'stdin': subprocess.DEVNULL,
        'text': True,
        'encoding': 'utf-8',
        'errors': 'replace',
        'capture_output': True,
        'creationflags': (
            getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
            if platform == 'nt' else 0
        ),
    }


def load_playlist(url):
    url = canonical_playlist_url(url)
    executable = find_tool('yt-dlp')
    proc = subprocess.run(
        [executable, '--flat-playlist', '--dump-single-json', '--no-warnings', '--', url],
        timeout=300,
        **subprocess_options(),
    )
    if proc.returncode != 0:
        raise ValueError(f'No se pudo leer la playlist: {(proc.stderr or proc.stdout)[-500:]}')
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('YouTube devolvió datos no válidos para la playlist.') from exc
    entries = []
    for item in payload.get('entries') or []:
        video_id = item.get('id')
        if _video_id_re.fullmatch(video_id or ''):
            entries.append({'video_id': video_id, 'title': item.get('title')})
    if not entries:
        raise ValueError('La playlist no contiene vídeos accesibles.')
    return entries


def expand_youtube_inputs(raw_urls, playlist_loader=None):
    playlist_loader = playlist_loader or load_playlist
    values = [part.strip() for part in re.split(r'[\s,]+', str(raw_urls or '')) if part.strip()]
    entries = []
    seen = set()
    for value in values:
        parsed = urllib.parse.urlparse(value)
        is_playlist = bool(urllib.parse.parse_qs(parsed.query).get('list')) or parsed.path.rstrip('/').endswith('/playlist')
        candidates = playlist_loader(canonical_playlist_url(value)) if is_playlist else [
            {'video_id': parse_video_id(value), 'title': None}
        ]
        for candidate in candidates:
            video_id = candidate.get('video_id')
            if not _video_id_re.fullmatch(video_id or ''):
                raise ValueError(f'URL o identificador de YouTube no válido: {value}')
            if video_id not in seen:
                seen.add(video_id)
                entries.append({'video_id': video_id, 'title': candidate.get('title')})
    if not entries:
        raise ValueError('Añade al menos un vídeo o playlist.')
    return entries


def add_video_urls(category_id, raw_urls, playlist_loader=None):
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        raise ValueError('Debes seleccionar una categoría válida.')
    with con() as c:
        category = c.execute('SELECT * FROM categories WHERE id=?', (category_id,)).fetchone()
        if not category:
            raise ValueError('Debes seleccionar una categoría válida.')
        entries = expand_youtube_inputs(raw_urls, playlist_loader)
        video_ids = [entry['video_id'] for entry in entries]
        placeholders = ','.join('?' for _ in video_ids)
        existing_ids = {
            r['video_id'] for r in c.execute(
                f'SELECT video_id FROM videos WHERE video_id IN ({placeholders})', video_ids
            )
        }
        for entry in entries:
            video_id = entry['video_id']
            url = f'https://www.youtube.com/watch?v={video_id}'
            c.execute(
                '''INSERT INTO videos(video_id,url,source,title,status,category_id)
                   VALUES(?,?,?,?,'remote',?)
                   ON CONFLICT(video_id) DO UPDATE SET
                     source=COALESCE(videos.source, excluded.source), updated_at=CURRENT_TIMESTAMP''',
                (video_id, url, category['name'], entry.get('title'), category_id),
            )
            c.execute(
                'INSERT OR IGNORE INTO video_categories(video_id, category_id) VALUES(?,?)',
                (video_id, category_id),
            )
    return {'added': len(video_ids) - len(existing_ids), 'existing': len(existing_ids), 'video_ids': video_ids}


def video_category_ids(video_id):
    with con() as c:
        if not c.execute('SELECT 1 FROM videos WHERE video_id=?', (video_id,)).fetchone():
            raise ValueError('El vídeo no existe.')
        return [row['category_id'] for row in c.execute(
            'SELECT category_id FROM video_categories WHERE video_id=? ORDER BY category_id',
            (video_id,),
        )]


def set_video_categories(video_id, category_ids):
    try:
        selected = sorted({int(category_id) for category_id in category_ids})
    except (TypeError, ValueError):
        raise ValueError('Debes seleccionar al menos una categoría válida.')
    if not selected:
        raise ValueError('Debes seleccionar al menos una categoría.')
    with con() as c:
        if not c.execute('SELECT 1 FROM videos WHERE video_id=?', (video_id,)).fetchone():
            raise ValueError('El vídeo no existe.')
        placeholders = ','.join('?' for _ in selected)
        valid = {row['id'] for row in c.execute(
            f'SELECT id FROM categories WHERE id IN ({placeholders})', selected
        )}
        if valid != set(selected):
            raise ValueError('Alguna categoría seleccionada no existe.')
        c.execute('DELETE FROM video_categories WHERE video_id=?', (video_id,))
        c.executemany(
            'INSERT INTO video_categories(video_id, category_id) VALUES(?,?)',
            [(video_id, category_id) for category_id in selected],
        )
        c.execute(
            'UPDATE videos SET category_id=?, updated_at=CURRENT_TIMESTAMP WHERE video_id=?',
            (selected[0], video_id),
        )
    return selected


def _safe_library_name(filename):
    name = str(filename or '')
    if not name or name in ('.', '..') or Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('El archivo local no tiene una ruta segura dentro de la videoteca.')
    return name


def _filename_identity(filename):
    # Clave deliberadamente conservadora: evita borrar alias de un mismo nombre
    # en Windows y en otros sistemas de archivos que ignoran mayúsculas o normalizan Unicode.
    name = _safe_library_name(filename)
    return unicodedata.normalize('NFKC', name).rstrip(' .').casefold()


def _windows_unlink_from_root(root, name):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    final_path.restype = wintypes.DWORD
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_info.restype = wintypes.BOOL

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ('attributes', wintypes.DWORD), ('creation_low', wintypes.DWORD),
            ('creation_high', wintypes.DWORD), ('access_low', wintypes.DWORD),
            ('access_high', wintypes.DWORD), ('write_low', wintypes.DWORD),
            ('write_high', wintypes.DWORD), ('volume_serial', wintypes.DWORD),
            ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
            ('links', wintypes.DWORD), ('file_index_high', wintypes.DWORD),
            ('file_index_low', wintypes.DWORD),
        ]

    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_info.restype = wintypes.BOOL

    delete_access = 0x00010000
    read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value

    def open_path(path, access, flags):
        handle = create_file(str(path), access, share_all, None, open_existing, flags, None)
        if handle == invalid_handle:
            code = ctypes.get_last_error()
            if code in (2, 3):
                return None
            raise ctypes.WinError(code)
        return handle

    def opened_path(handle):
        size = final_path(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        if not final_path(handle, buffer, len(buffer), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        value = buffer.value
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        return Path(value)

    root_handle = open_path(root, read_attributes, backup_semantics | open_reparse_point)
    if root_handle is None:
        raise OSError('El directorio de la videoteca no existe.')
    try:
        root_info = ByHandleFileInformation()
        if not get_info(root_handle, ctypes.byref(root_info)):
            raise ctypes.WinError(ctypes.get_last_error())
        directory_attribute = 0x10
        reparse_attribute = 0x400
        if not root_info.attributes & directory_attribute or root_info.attributes & reparse_attribute:
            raise OSError('El directorio de la videoteca no es una raíz segura.')
        root_opened = opened_path(root_handle)
        file_handle = open_path(root / name, delete_access | read_attributes, open_reparse_point)
        if file_handle is None:
            return False
        try:
            file_opened = opened_path(file_handle)
            root_still_opened = opened_path(root_handle)
            if os.path.normcase(str(root_still_opened)) != os.path.normcase(str(root_opened)):
                raise OSError('La raíz de la videoteca cambió durante el borrado.')
            if os.path.normcase(str(file_opened.parent)) != os.path.normcase(str(root_opened)):
                raise OSError('El archivo local no pertenece a la raíz abierta de la videoteca.')

            class FileDispositionInfo(ctypes.Structure):
                _fields_ = [('DeleteFile', wintypes.BOOL)]

            disposition = FileDispositionInfo(True)
            if not set_info(file_handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
        finally:
            close_handle(file_handle)
    finally:
        close_handle(root_handle)


def _unlink_library_file(root, filename):
    name = _safe_library_name(filename)
    root = Path(os.path.abspath(root))
    if os.name == 'nt':
        return _windows_unlink_from_root(root, name)
    if os.unlink not in os.supports_dir_fd:
        raise OSError('El sistema no permite un borrado local seguro mediante descriptor de directorio.')
    before = os.lstat(root)
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or getattr(before, 'st_file_attributes', 0) & reparse_flag:
        raise OSError('El directorio de la videoteca no es una raíz segura.')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    directory_fd = os.open(root, flags)
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError('La raíz de la videoteca cambió durante el borrado.')
        try:
            os.unlink(name, dir_fd=directory_fd)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(directory_fd)


def retry_file_cleanup(task_ids=None):
    params = []
    where = ''
    if task_ids is not None:
        ids = [int(task_id) for task_id in task_ids]
        if not ids:
            return {'local_deleted': False, 'file_errors': []}
        where = f" WHERE id IN ({','.join('?' for _ in ids)})"
        params = ids
    with con() as c:
        pending_ids = [row['id'] for row in c.execute(
            f'SELECT id FROM file_cleanup_queue{where} ORDER BY id', params,
        ).fetchall()]
    local_deleted = False
    file_errors = []
    roots = {'videos': VIDEOS, 'thumbs': THUMBS}
    for task_id in pending_ids:
        with con() as c:
            # Serializa cualquier alta/reutilización del nombre hasta terminar el unlink.
            c.execute('BEGIN IMMEDIATE')
            task = c.execute(
                '''SELECT id,video_id,root_kind,filename,filename_key
                   FROM file_cleanup_queue WHERE id=?''',
                (task_id,),
            ).fetchone()
            if not task:
                continue
            reused = c.execute('SELECT 1 FROM videos WHERE video_id=?', (task['video_id'],)).fetchone()
            if not reused and task['root_kind'] == 'videos':
                reused = any(
                    _filename_identity(row['filename']) == task['filename_key']
                    for row in c.execute('SELECT filename FROM videos WHERE filename IS NOT NULL')
                )
            if reused:
                # La intención antigua deja de ser válida: nunca se aplicará a una generación futura.
                c.execute('DELETE FROM file_cleanup_queue WHERE id=?', (task_id,))
                file_errors.append(
                    f"{task['filename']}: limpieza cancelada porque el archivo vuelve a estar en uso"
                )
                continue
            try:
                removed = _unlink_library_file(roots[task['root_kind']], task['filename'])
                local_deleted = removed or local_deleted
                c.execute('DELETE FROM file_cleanup_queue WHERE id=?', (task_id,))
            except OSError as exc:
                file_errors.append(f"{task['filename']}: {exc}")
                c.execute('''UPDATE file_cleanup_queue
                             SET attempts=attempts+1,last_error=? WHERE id=?''',
                          (str(exc), task_id))
    return {'local_deleted': local_deleted, 'file_errors': file_errors}


def delete_video(video_id, delete_local=False):
    if not _video_id_re.fullmatch(str(video_id or '')):
        raise ValueError('El identificador del vídeo no es válido.')
    with con() as c:
        video = c.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not video:
            raise ValueError('El vídeo no existe.')
        if video['status'] in ('pending', 'running'):
            raise ValueError('No se puede borrar mientras la descarga está pendiente o en curso.')
        shared_file = bool(video['filename'] and c.execute(
            'SELECT 1 FROM videos WHERE video_id<>? AND filename=?',
            (video_id, video['filename']),
        ).fetchone())
        files = []
        if delete_local:
            if video['filename'] and not shared_file:
                files.append(('videos', _safe_library_name(video['filename'])))
            files.append(('thumbs', _safe_library_name(f'{video_id}.jpg')))
        cleanup_ids = []
        for root_kind, filename in files:
            filename_key = _filename_identity(filename)
            c.execute(
                '''INSERT OR IGNORE INTO file_cleanup_queue
                   (video_id,root_kind,filename,filename_key) VALUES(?,?,?,?)''',
                (video_id, root_kind, filename, filename_key),
            )
            cleanup_ids.append(c.execute(
                'SELECT id FROM file_cleanup_queue WHERE root_kind=? AND filename_key=?',
                (root_kind, filename_key),
            ).fetchone()['id'])
        deleted = c.execute(
            "DELETE FROM videos WHERE video_id=? AND status NOT IN ('pending','running')",
            (video_id,),
        )
        if deleted.rowcount != 1:
            current = c.execute('SELECT status FROM videos WHERE video_id=?', (video_id,)).fetchone()
            if current and current['status'] in ('pending', 'running'):
                raise ValueError('No se puede borrar mientras la descarga está pendiente o en curso.')
            raise ValueError('El vídeo ya no existe.')

    cleanup = retry_file_cleanup(cleanup_ids)
    return {
        'deleted': True,
        'local_deleted': cleanup['local_deleted'],
        'shared_file_kept': shared_file,
        'file_errors': cleanup['file_errors'],
    }


def queue_video(video_id):
    with con() as c:
        exists = c.execute('SELECT 1 FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not exists:
            raise ValueError('El vídeo no existe.')
        cur = c.execute(
            "UPDATE videos SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE video_id=? AND status IN ('remote','error')",
            (video_id,),
        )
        return cur.rowcount


def queue_category(category_id):
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        raise ValueError('Debes seleccionar una categoría válida.')
    with con() as c:
        if not c.execute('SELECT 1 FROM categories WHERE id=?', (category_id,)).fetchone():
            raise ValueError('Debes seleccionar una categoría válida.')
        cur = c.execute(
            "UPDATE videos SET status='pending', error=NULL, updated_at=CURRENT_TIMESTAMP "
            "WHERE video_id IN (SELECT video_id FROM video_categories WHERE category_id=?) "
            "AND status IN ('remote','error')",
            (category_id,),
        )
        return cur.rowcount


def find_tool(name, platform=None, frozen=None):
    platform = platform or os.name
    frozen = FROZEN if frozen is None else frozen
    if platform == 'nt' and frozen:
        import downloader
        return downloader.tool(name, platform=platform, frozen=frozen)
    suffix = '.exe' if platform == 'nt' else ''
    candidates = [
        shutil.which(name),
        ROOT / 'tools' / f'{name}{suffix}',
        Path.home() / '.local' / 'bin' / f'{name}{suffix}',
        BINARY_DIR / f'{name}{suffix}',
        SOURCE_DIR / '.venv' / ('Scripts' if platform == 'nt' else 'bin') / f'{name}{suffix}',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(f'No se encontró {name}. Consulta las instrucciones de instalación del README.')


_downloader_thread = None
_downloader_state_lock = threading.Lock()
_downloader_requested = False


def _run_embedded_downloader():
    global _downloader_thread, _downloader_requested
    while True:
        with _downloader_state_lock:
            _downloader_requested = False
        try:
            import downloader
            downloader.main()
        except Exception as exc:
            message = f'Error del descargador: {exc}'
            try:
                downloader.log(message)
            except Exception:
                print(message, flush=True)
        with _downloader_state_lock:
            if _downloader_requested:
                continue
            _downloader_thread = None
            return


def start_downloader():
    global _downloader_thread, _downloader_requested
    service = os.environ.get('VIDEOTECA_DOWNLOADER_SERVICE', '').strip()
    if service and os.name != 'nt':
        proc = subprocess.run(
            ['systemctl', '--user', 'start', service],
            text=True, capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or 'No se pudo iniciar el descargador').strip())
        return
    with _downloader_state_lock:
        _downloader_requested = True
        if _downloader_thread and _downloader_thread.is_alive():
            return
        _downloader_thread = threading.Thread(
            target=_run_embedded_downloader, name='descargador', daemon=True
        )
        _downloader_thread.start()


def resume_pending_downloads():
    with con() as connection:
        connection.execute(
            "UPDATE videos SET status='pending', updated_at=CURRENT_TIMESTAMP WHERE status='running'"
        )
        queued = connection.execute(
            "SELECT 1 FROM videos WHERE status='pending' LIMIT 1"
        ).fetchone()
    if not queued:
        return False
    start_downloader()
    return True


def esc(value):
    return html.escape(str(value or ''), quote=True)


def folded(value):
    """Texto comparable sin tildes para reconocer colecciones por el título."""
    normalized = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ''.join(char for char in normalized if not unicodedata.combining(char))


def display_group(r):
    if 'category_name' in r.keys() and r['category_name']:
        return r['category_name']
    label = r['category'] or ('Prioridad' if (r['priority'] or 0) > 0 else r['source'] or 'Sin grupo')
    if label == 'Clases de árabe':
        date = (r['upload_date'] or '').replace('-', '')
        if date:
            return 'Árabe 25/26' if date >= '20250901' else 'Árabe 24/25'
        return 'Árabe · sin fecha'

    title = folded(r['title'])
    source = folded(r['source'])
    if label == 'Bermejo' or 'bermejo' in source:
        return 'Bermejo'
    if 'tazkiyah' in title or 'tazkiya' in title:
        return 'Tazkiya'
    if 'taywid' in title or 'tajwid' in title or 'tajweed' in title:
        return 'Tajweed'
    if re.search(r'(^|\W)sirah(\W|$)', title):
        return 'Sirah'
    if 'ulum al-hadiz' in title or 'ulum al hadiz' in title:
        return 'Ulum al-Hadiz'
    if 'ulum al quran' in title or 'ulum al-quran' in title:
        return 'Ulum al-Quran'
    if title.startswith('fiqh y aqidah'):
        return 'Fiqh y Aqidah'
    if title.startswith('hadiz'):
        return 'Hadiz'
    if title.startswith('tafsir'):
        return 'Tafsir'
    if title.startswith('ramadan') or 'manazil al-sa' in title:
        return 'Ramadán · Manazil al-Sairin'
    if 'ghazali' in title or label == 'Retiro Al-Ghazali':
        return 'Retiro Al-Ghazali'
    return 'Otros' if label == 'Links' else label


def group_order(label):
    preferred = [
        'Árabe 25/26', 'Árabe 24/25', 'Árabe · sin fecha', 'Tazkiya',
        'Tajweed', 'Sirah', 'Bermejo', 'Fiqh y Aqidah', 'Hadiz', 'Tafsir',
        'Ulum al-Quran', 'Ulum al-Hadiz', 'Ramadán · Manazil al-Sairin',
        'Retiro Al-Ghazali', 'Otros',
    ]
    fixed = {name: position for position, name in enumerate(preferred)}
    return fixed.get(label, len(preferred)), natural_key(label)


def rows():
    with con() as c:
        result = c.execute('''SELECT v.*, vc.category_id AS view_category_id,
                                     c.name AS category_name
                              FROM videos v
                              LEFT JOIN video_categories vc ON vc.video_id=v.video_id
                              LEFT JOIN categories c ON c.id=vc.category_id''').fetchall()
    return sorted(result, key=lambda r: (
        group_order(display_group(r)),
        natural_key(r['title'] or r['video_id']),
        r['video_id'],
    ))


def api_video_rows():
    with con() as c:
        videos = [dict(row) for row in c.execute('SELECT * FROM videos ORDER BY rowid').fetchall()]
        memberships = c.execute('''SELECT vc.video_id, vc.category_id, c.name
                                    FROM video_categories vc
                                    JOIN categories c ON c.id=vc.category_id
                                    ORDER BY vc.video_id, vc.category_id''').fetchall()
    categories_by_video = {}
    for membership in memberships:
        categories_by_video.setdefault(membership['video_id'], []).append({
            'id': membership['category_id'], 'name': membership['name'],
        })
    for video in videos:
        categories = categories_by_video.get(video['video_id'], [])
        video['category_ids'] = [category['id'] for category in categories]
        video['categories'] = categories
    return videos


def get_video(video_id):
    with con() as c:
        row = c.execute('SELECT * FROM videos WHERE video_id=?', (video_id,)).fetchone()
        if not row:
            return None
        video = dict(row)
        categories = [dict(category) for category in c.execute(
            '''SELECT c.id,c.name FROM video_categories vc
               JOIN categories c ON c.id=vc.category_id
               WHERE vc.video_id=? ORDER BY c.name COLLATE NOCASE''',
            (video_id,),
        ).fetchall()]
    video['category_ids'] = [category['id'] for category in categories]
    video['categories'] = categories
    video['category_name'] = categories[0]['name'] if categories else None
    return video


def format_duration(seconds):
    if not seconds:
        return ''
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def base_css():
    return """
:root{color-scheme:dark;--bg:#0a0a0a;--surface:#121212;--panel:#181818;--panel2:#222;--raised:#292929;--line:#353535;--text:#fff;--muted:#b3b3b3;--accent:#1ed760;--accent-hover:#3be477;--bad:#f3727f;--warn:#ffa42b;--info:#539df5;--shadow:0 18px 48px rgba(0,0,0,.42)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;min-height:100vh;background:linear-gradient(180deg,#1b1b1b 0,#0a0a0a 360px);color:var(--text);font-family:"DM Sans",Inter,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:inherit}button,input,select,textarea{font:inherit}button,a,input,select,textarea{outline-offset:3px}:focus-visible{outline:3px solid var(--accent)}
header{position:sticky;top:0;z-index:10;background:rgba(10,10,10,.9);backdrop-filter:blur(18px);border-bottom:1px solid rgba(255,255,255,.08)}.head{max-width:1480px;margin:auto;padding:18px 24px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px}.brand{display:flex;align-items:center;gap:10px;font-size:1.45rem;font-weight:850;letter-spacing:-.04em;text-decoration:none}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;background:var(--accent);color:#07150b;font-size:.8rem;padding-left:2px;box-shadow:0 0 0 5px rgba(30,215,96,.12)}.tagline{margin:5px 0 0 44px;color:var(--muted);font-size:.78rem}.language-switch{display:flex;gap:4px;padding:4px;background:#1f1f1f;border-radius:999px}.language-switch a{padding:7px 11px;border-radius:999px;color:var(--muted);font-size:.72rem;font-weight:750;text-decoration:none;white-space:nowrap}.language-switch a.active{background:#fff;color:#111}.language-switch a:hover{color:#fff}
.summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin-top:18px}.stat{display:flex;flex-direction:column;gap:2px;min-width:0;padding:11px 13px;border-radius:10px;background:rgba(255,255,255,.055);color:var(--muted);font-size:.7rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.stat b{color:#fff;font-size:1.15rem;line-height:1}.controls{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:10px;margin-top:14px}.search-wrap{position:relative;display:block}.search-wrap>span{position:absolute;left:16px;top:50%;transform:translateY(-50%);z-index:1;color:var(--muted);font-size:1.35rem}.controls input,.controls select{width:100%;min-height:48px;border:1px solid transparent;border-radius:999px;background:#242424;color:var(--text);padding:12px 18px;font-size:.95rem;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}.controls input{padding-left:48px}.controls input:hover,.controls select:hover{background:#2b2b2b}.controls input:focus,.controls select:focus{border-color:#fff;box-shadow:none}.controls select{cursor:pointer}
main{max-width:1480px;margin:auto;padding:22px 24px 48px}.group{margin:0 0 14px;border-radius:12px;background:var(--surface);overflow:hidden}.group>summary{cursor:pointer;list-style:none;padding:17px 18px;font-size:1.05rem;font-weight:800;display:flex;gap:10px;align-items:center;user-select:none}.group>summary::-webkit-details-marker{display:none}.group>summary::before{content:'›';font-size:1.55rem;line-height:.7;color:var(--accent);transition:transform .18s}.group[open]>summary::before{transform:rotate(90deg)}.group>summary:hover{background:#1b1b1b}.group-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.count{display:grid;place-items:center;min-width:27px;height:22px;padding:0 7px;border-radius:999px;background:#2b2b2b;color:var(--muted);font-size:.7rem;font-weight:700}.group .grid{padding:0 14px 16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{min-width:0;display:flex;flex-direction:column;background:var(--panel);border-radius:10px;overflow:hidden;transition:background .18s,transform .18s,box-shadow .18s}.card:hover{background:var(--raised);transform:translateY(-2px);box-shadow:0 12px 28px rgba(0,0,0,.35)}.card[hidden],.group[hidden]{display:none!important}.card-main{display:block;flex:1;text-decoration:none;color:inherit}.thumb{position:relative;aspect-ratio:16/9;background:#282828;overflow:hidden}.thumb img{display:block;width:100%;height:100%;object-fit:cover;transition:transform .25s}.card:hover .thumb img{transform:scale(1.025)}.thumb.placeholder{display:grid;place-items:center;background:linear-gradient(135deg,#262626,#151515);color:#777}.pending-icon{display:grid;place-items:center;width:48px;height:48px;border:2px solid #555;border-radius:50%;font-size:1.4rem}.play{position:absolute;right:12px;bottom:12px;display:grid;place-items:center;width:48px;height:48px;padding-left:3px;border-radius:50%;background:var(--accent);color:#061109;box-shadow:0 8px 20px #0008;opacity:0;transform:translateY(8px);transition:.18s}.card:hover .play,.card:focus-within .play{opacity:1;transform:none}.duration{position:absolute;right:8px;top:8px;border-radius:5px;background:#000c;padding:3px 6px;font-size:.68rem;font-weight:750}.body{padding:13px 13px 11px}.meta{display:flex;justify-content:space-between;align-items:center;gap:8px;color:var(--muted);font-size:.67rem}.badge{display:inline-flex;align-items:center;border-radius:999px;background:#303030;padding:4px 8px;color:#ddd;font-size:.64rem;font-weight:800;text-transform:uppercase;letter-spacing:.05em}.status-done{background:rgba(30,215,96,.14);color:#5eef91}.status-running,.status-pending{background:rgba(83,157,245,.15);color:#8fc1ff}.status-error{background:rgba(243,114,127,.15);color:#ff9da7}.status-remote{background:#303030;color:#ccc}.title{margin:9px 0 0;font-size:.92rem;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.card-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;padding:0 10px 10px}.card-actions a,.card-actions button,.group-actions button,.manager button,.dialog-actions button,.button{min-width:0;border:0;border-radius:999px;background:#2b2b2b;color:#fff;padding:8px 10px;font-size:.7rem;font-weight:800;text-align:center;text-decoration:none;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:transform .12s,background .12s}.card-actions a:hover,.card-actions button:hover,.group-actions button:hover,.manager button:hover,.dialog-actions button:hover,.button:hover{background:#3a3a3a;transform:scale(1.02)}.card-actions .youtube{background:#342326;color:#ffb1b8}.card-actions .download,.card-actions .manage-video{grid-column:1/-1}.card-actions .download,.group-actions button,.manager button,.dialog-actions button{background:var(--accent);color:#07150b}.card-actions .download:hover,.group-actions button:hover,.manager button:hover,.dialog-actions button:hover{background:var(--accent-hover)}.group-actions{padding:0 14px 12px}.group-actions button{padding:9px 15px}
.manager{max-width:1432px;margin:18px auto 0;border-radius:12px;background:var(--surface);overflow:hidden}.manager>summary{cursor:pointer;display:flex;align-items:center;gap:10px;padding:16px 18px;font-weight:850;list-style:none}.manager>summary span{color:var(--accent);font-size:1.25rem}.manager-grid{display:grid;grid-template-columns:minmax(0,.7fr) minmax(0,1.3fr);gap:12px;padding:0 14px 14px}.manager-card{min-width:0;background:#1b1b1b;border-radius:10px;padding:16px}.manager-card h2{font-size:1rem;margin:0 0 14px}.manager-card label{display:grid;gap:7px;margin-bottom:12px;color:var(--muted);font-size:.78rem;font-weight:650}.manager-card input,.manager-card select,.manager-card textarea{width:100%;min-width:0;border:1px solid #404040;border-radius:8px;background:#101010;color:var(--text);padding:11px 12px}.manager-card textarea{resize:vertical}.manager-card button{padding:10px 16px}.manager-message{display:none;margin:0 14px 14px;padding:11px 13px;border-radius:8px;background:#123e29;color:#bff8d2}.manager-message.error{background:#4b2025;color:#ffd4d8}.manager-message.show{display:block}
.video-dialog{width:min(580px,calc(100vw - 24px));max-height:90vh;overflow:auto;border:1px solid #454545;border-radius:14px;background:#1b1b1b;color:var(--text);padding:0;box-shadow:var(--shadow)}.video-dialog::backdrop{background:#000c;backdrop-filter:blur(4px)}.dialog-body{padding:22px}.dialog-body h2{margin:0 0 5px;font-size:1.25rem}.dialog-title{color:var(--muted);margin:0 0 18px;word-break:break-word}.category-checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:11px 0 18px}.category-check{display:flex;align-items:center;gap:8px;border:1px solid #3b3b3b;border-radius:8px;background:#121212;padding:10px;min-width:0}.category-check:has(input:checked){border-color:var(--accent);background:rgba(30,215,96,.08)}.category-check input{width:18px;height:18px;accent-color:var(--accent);flex:0 0 auto}.category-check span{overflow:hidden;text-overflow:ellipsis}.dialog-actions{display:flex;gap:8px;flex-wrap:wrap}.dialog-actions button{padding:10px 15px}.dialog-actions .secondary{background:#333;color:#fff}.delete-zone{margin-top:22px;padding-top:18px;border-top:1px solid #3b3b3b}.delete-option{display:flex;align-items:flex-start;gap:9px;color:#ffc5cb;font-size:.85rem;margin:13px 0}.delete-option input{width:18px;height:18px;accent-color:var(--bad);flex:0 0 auto}.dialog-actions .danger{background:#c13e4c;color:#fff}.dialog-actions .danger:hover{background:#dc5260}.dialog-error{display:none;color:#ffd6da;background:#4c2027;border-radius:8px;padding:10px;margin:10px 0}.dialog-error.show{display:block}.empty{display:none;padding:60px 20px;text-align:center;color:var(--muted)}
.watch{max-width:1100px}.back{display:inline-block;margin-bottom:18px;color:var(--muted);font-weight:700;text-decoration:none}.back:hover{color:#fff}.player{display:block;width:100%;max-height:72vh;border-radius:12px;background:#000;box-shadow:var(--shadow)}.watch h1{font-size:clamp(1.5rem,4vw,2.6rem);letter-spacing:-.035em}.watch-meta{display:flex;gap:12px;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:.82rem}.actions{margin-top:20px}.button{display:inline-block;background:var(--accent);color:#07150b;padding:11px 18px}.notice{padding:28px;border-radius:12px;background:#1b1b1b;color:var(--muted);text-align:center}.error-notice{margin-top:12px;color:#ffc2c8;background:#451d22}
@media(max-width:850px){.summary{grid-template-columns:repeat(3,1fr)}.manager-grid{grid-template-columns:1fr}.grid{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}}
@media(max-width:650px){.head{padding:13px 12px}.top{align-items:flex-start}.brand{font-size:1.18rem}.brand-mark{width:30px;height:30px}.tagline{margin-left:40px}.language-switch a{padding:6px 8px;font-size:.64rem}.summary{grid-template-columns:repeat(3,1fr);gap:6px;margin-top:13px}.stat{padding:8px 9px;font-size:.62rem}.stat b{font-size:1rem}.controls{grid-template-columns:1fr}.controls input,.controls select{min-height:44px}.manager{margin:12px 10px 0}.manager-grid{padding:0 10px 10px}main{padding:14px 10px 36px}.group{margin-bottom:10px}.group>summary{padding:14px 12px}.group .grid{padding:0 8px 10px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.body{padding:9px}.title{font-size:.8rem}.meta{font-size:.6rem}.play{width:40px;height:40px;opacity:1;transform:none}.card-actions{gap:5px;padding:0 7px 7px}.card-actions a,.card-actions button{font-size:.63rem;padding:7px 4px}.category-checks{grid-template-columns:1fr}.watch{padding:14px}.player{border-radius:8px}}
@media(max-width:390px){.tagline{display:none}.language-switch a{font-size:0}.language-switch a::after{content:attr(data-short);font-size:.68rem}.grid{grid-template-columns:1fr 1fr}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
"""


UI_TEXT = {
    'es': {
        'html_lang': 'es', 'library': 'Videoteca', 'tagline': 'Tu videoteca privada',
        'sequential': 'Descarga secuencial', 'total': 'Total', 'available': 'Disponibles',
        'not_downloaded': 'Sin descargar', 'downloading': 'Descargando', 'pending': 'Pendientes',
        'failed': 'Fallidos', 'search': 'Buscar vídeos',
        'search_placeholder': 'Buscar clase, tema o identificador…', 'filter_status': 'Filtrar por estado',
        'all_statuses': 'Todos los estados', 'only_available': 'Solo disponibles',
        'status_done': 'Disponible', 'status_remote': 'Solo en YouTube', 'status_pending': 'En cola',
        'status_running': 'Descargando', 'status_error': 'Falló', 'view_local': 'Ver local',
        'download': 'Descargar', 'manage_delete': 'Organizar / borrar',
        'download_category': 'Descargar toda la categoría', 'empty': 'No hay vídeos que coincidan con la búsqueda.',
        'manage_library': 'Gestionar videoteca', 'create_category': 'Crear categoría', 'name': 'Nombre',
        'category_example': 'Ej. Tafsir 26/27', 'add_videos': 'Añadir vídeos', 'category': 'Categoría',
        'choose_category': 'Selecciona una categoría', 'videos_playlists': 'Vídeos o playlists',
        'urls_placeholder': 'Pega un vídeo, una playlist o varias URLs (una por línea)',
        'add_to_library': 'Añadir a la videoteca', 'organize_video': 'Organizar o borrar vídeo',
        'categories': 'Categorías', 'save_categories': 'Guardar categorías', 'cancel': 'Cancelar',
        'remove_library': 'Eliminar de la videoteca',
        'delete_local': 'Eliminar también el archivo de vídeo y su miniatura del disco',
        'delete_video': 'Borrar vídeo', 'operation_failed': 'No se pudo completar la operación.',
        'reading_links': 'Leyendo enlaces y playlists…', 'added': 'Añadidos', 'existing': 'Ya existentes',
        'select_category_error': 'Selecciona al menos una categoría.',
        'confirm_delete_local': 'Se borrará el vídeo de la videoteca Y TAMBIÉN su archivo local. ¿Continuar?',
        'confirm_delete_record': 'Se borrará el vídeo de la videoteca, pero se conservará el archivo local. ¿Continuar?',
        'cleanup_warning': 'El registro se borró, pero algunos archivos no pudieron eliminarse:',
        'queueing': 'Encolando…', 'queued': 'vídeo(s) enviados a la cola.',
        'back': 'Volver a la videoteca', 'open_youtube': 'Abrir en YouTube',
        'watch_running': 'Este vídeo se está descargando ahora.',
        'watch_pending': 'Este vídeo todavía está pendiente.',
        'watch_error': 'La descarga falló. Corrige la causa y pulsa Descargar para reintentarlo.',
        'watch_unavailable': 'El vídeo aún no está disponible.', 'language': 'Idioma',
    },
    'en': {
        'html_lang': 'en', 'library': 'Video library', 'tagline': 'Your private video vault',
        'sequential': 'Sequential downloads', 'total': 'Total', 'available': 'Available',
        'not_downloaded': 'Not downloaded', 'downloading': 'Downloading', 'pending': 'Pending',
        'failed': 'Failed', 'search': 'Search videos',
        'search_placeholder': 'Search lessons, topics, or IDs…', 'filter_status': 'Filter by status',
        'all_statuses': 'All statuses', 'only_available': 'Available only',
        'status_done': 'Available', 'status_remote': 'YouTube only', 'status_pending': 'Queued',
        'status_running': 'Downloading', 'status_error': 'Failed', 'view_local': 'Watch locally',
        'download': 'Download', 'manage_delete': 'Manage / delete',
        'download_category': 'Download entire category', 'empty': 'No videos match your search.',
        'manage_library': 'Manage library', 'create_category': 'Create category', 'name': 'Name',
        'category_example': 'E.g. Tafsir 26/27', 'add_videos': 'Add videos', 'category': 'Category',
        'choose_category': 'Choose a category', 'videos_playlists': 'Videos or playlists',
        'urls_placeholder': 'Paste one video, playlist, or multiple URLs (one per line)',
        'add_to_library': 'Add to library', 'organize_video': 'Manage or delete video',
        'categories': 'Categories', 'save_categories': 'Save categories', 'cancel': 'Cancel',
        'remove_library': 'Remove from library',
        'delete_local': 'Also delete the video file and thumbnail from disk',
        'delete_video': 'Delete video', 'operation_failed': 'The operation could not be completed.',
        'reading_links': 'Reading links and playlists…', 'added': 'Added', 'existing': 'Already present',
        'select_category_error': 'Select at least one category.',
        'confirm_delete_local': 'This video AND its local file will be deleted. Continue?',
        'confirm_delete_record': 'The library record will be deleted, but the local file will be kept. Continue?',
        'cleanup_warning': 'The record was deleted, but some files could not be removed:',
        'queueing': 'Queueing…', 'queued': 'video(s) sent to the queue.',
        'back': 'Back to the library', 'open_youtube': 'Open on YouTube',
        'watch_running': 'This video is downloading now.',
        'watch_pending': 'This video is still pending.',
        'watch_error': 'The download failed. Fix the cause and press Download to retry.',
        'watch_unavailable': 'This video is not available yet.', 'language': 'Language',
    },
}


def normalize_lang(lang):
    return 'en' if str(lang or '').casefold().startswith('en') else 'es'


def language_nav(lang):
    active_es = ' active' if lang == 'es' else ''
    active_en = ' active' if lang == 'en' else ''
    current_es = ' aria-current="page"' if lang == 'es' else ''
    current_en = ' aria-current="page"' if lang == 'en' else ''
    return (
        f'<nav class="language-switch" aria-label="{esc(UI_TEXT[lang]["language"])}">'
        f'<a class="{active_es}" href="?lang=es" lang="es" data-short="ES"{current_es}>ES · Español</a>'
        f'<a class="{active_en}" href="?lang=en" lang="en" data-short="EN"{current_en}>EN · English</a></nav>'
    )


def landing_page(lang='es'):
    lang = normalize_lang(lang)
    text = UI_TEXT[lang]
    all_rows = rows()
    categories = list_categories()
    counts = {}
    seen_videos = set()
    membership_map = {}
    groups = {category['name']: [] for category in categories}
    category_ids = {category['name']: category['id'] for category in categories}
    for row in all_rows:
        membership_map.setdefault(row['video_id'], []).append(row['view_category_id'])
        if row['video_id'] not in seen_videos:
            seen_videos.add(row['video_id'])
            counts[row['status']] = counts.get(row['status'], 0) + 1
        groups.setdefault(display_group(row), []).append(row)

    cards_by_group = []
    status_labels = {key.removeprefix('status_'): value for key, value in text.items() if key.startswith('status_')}
    for group, items in groups.items():
        cards = []
        for row in items:
            title = row['title'] or row['video_id']
            status = row['status']
            duration = format_duration(row['duration'])
            if status == 'done' and row['filename']:
                visual = (
                    f'<div class="thumb"><img loading="lazy" decoding="async" width="640" height="360" '
                    f'src="/thumb/{esc(row["video_id"])}.jpg" alt="" '
                    f'onerror="this.closest(\'.thumb\').classList.add(\'placeholder\');this.remove()">'
                    f'<span class="play" aria-hidden="true">▶</span>'
                    + (f'<span class="duration">{esc(duration)}</span>' if duration else '') + '</div>'
                )
            else:
                visual = '<div class="thumb placeholder" aria-hidden="true"><span class="pending-icon">↓</span></div>'
            search_text = re.sub(r'\s+', ' ', folded(f'{group} {title} {row["video_id"]}')).strip()
            watch_url = f'/watch/{esc(row["video_id"])}?lang={lang}'
            cards.append(
                f'<article class="card {esc(status)}" data-status="{esc(status)}" data-search="{esc(search_text)}">'
                f'<a class="card-main" href="{watch_url}">{visual}'
                f'<div class="body"><div class="meta"><span class="badge status-{esc(status)}">'
                f'{esc(status_labels.get(status, status))}</span><span>{esc(row["video_id"])}</span></div>'
                f'<h3 class="title">{esc(title)}</h3></div></a>'
                f'<div class="card-actions"><a href="{watch_url}">{esc(text["view_local"])}</a>'
                f'<a class="youtube" href="{esc(row["url"])}" target="_blank" rel="noreferrer">YouTube ↗</a>'
                + (f'<button class="download" type="button" data-download-video="{esc(row["video_id"])}">{esc(text["download"])}</button>' if status in ('remote', 'error') else '')
                + f'<button class="manage-video" type="button" data-manage-video="{esc(row["video_id"])}" '
                  f'data-video-title="{esc(title)}" data-category-ids="{",".join(str(value) for value in membership_map[row["video_id"]] if value is not None)}">{esc(text["manage_delete"])}</button>'
                + '</div></article>'
            )
        category_id = items[0]['view_category_id'] if items else category_ids.get(group)
        cards_by_group.append(
            f'<details class="group" data-group="{esc(group.casefold())}"><summary><span class="group-name">{esc(group)}</span> '
            f'<span class="count">{len(items)}</span></summary>'
            + (f'<div class="group-actions"><button type="button" data-download-category="{category_id}">{esc(text["download_category"])}</button></div>' if category_id else '')
            + f'<div class="grid">{"".join(cards)}</div></details>'
        )

    stats = (
        f'<span class="stat"><b>{len(seen_videos)}</b>{esc(text["total"])}</span>'
        f'<span class="stat"><b>{counts.get("done", 0)}</b>{esc(text["available"])}</span>'
        f'<span class="stat"><b>{counts.get("remote", 0)}</b>{esc(text["not_downloaded"])}</span>'
        f'<span class="stat"><b>{counts.get("running", 0)}</b>{esc(text["downloading"])}</span>'
        f'<span class="stat"><b>{counts.get("pending", 0)}</b>{esc(text["pending"])}</span>'
        f'<span class="stat"><b>{counts.get("error", 0)}</b>{esc(text["failed"])}</span>'
    )
    category_options = ''.join(
        f'<option value="{category["id"]}">{esc(category["name"])}</option>' for category in categories
    )
    category_checkboxes = ''.join(
        f'<label class="category-check"><input type="checkbox" name="managed_categories" '
        f'value="{category["id"]}"><span>{esc(category["name"])}</span></label>' for category in categories
    )
    manager = f'''<details class="manager"><summary><span>＋</span>{esc(text['manage_library'])}</summary><div class="manager-grid">
<form id="categoryForm" class="manager-card"><h2>{esc(text['create_category'])}</h2><label>{esc(text['name'])}<input name="name" required maxlength="80" placeholder="{esc(text['category_example'])}"></label><button type="submit">{esc(text['create_category'])}</button></form>
<form id="videoForm" class="manager-card"><h2>{esc(text['add_videos'])}</h2><label>{esc(text['category'])}<select name="category_id" required><option value="" selected disabled>{esc(text['choose_category'])}</option>{category_options}</select></label><label>{esc(text['videos_playlists'])}<textarea name="urls" required rows="5" placeholder="{esc(text['urls_placeholder'])}"></textarea></label><button type="submit">{esc(text['add_to_library'])}</button></form>
</div><p id="managerMessage" class="manager-message" role="status"></p></details>'''
    video_dialog = f'''<dialog id="videoManageDialog" class="video-dialog"><form id="videoManageForm" class="dialog-body">
<input type="hidden" name="video_id"><h2>{esc(text['organize_video'])}</h2><p id="managedVideoTitle" class="dialog-title"></p>
<strong>{esc(text['categories'])}</strong><div class="category-checks">{category_checkboxes}</div>
<p id="videoDialogError" class="dialog-error" role="alert"></p>
<div class="dialog-actions"><button type="submit">{esc(text['save_categories'])}</button><button type="button" class="secondary" data-close-video-dialog>{esc(text['cancel'])}</button></div>
<div class="delete-zone"><strong>{esc(text['remove_library'])}</strong><label class="delete-option"><input id="deleteLocal" type="checkbox">{esc(text['delete_local'])}</label>
<div class="dialog-actions"><button id="deleteVideoButton" type="button" class="danger">{esc(text['delete_video'])}</button></div></div>
</form></dialog>'''
    js_text = json.dumps({key: text[key] for key in (
        'operation_failed', 'reading_links', 'added', 'existing', 'select_category_error',
        'confirm_delete_local', 'confirm_delete_record', 'cleanup_warning', 'queueing', 'queued',
        'download', 'download_category',
    )}, ensure_ascii=False)
    script = f"""
const lang={json.dumps(lang)}, T={js_text};
const params=new URLSearchParams(location.search);let savedLang=null;
try{{savedLang=localStorage.getItem('ytvault-lang');}}catch(error){{}}
if(!params.has('lang')&&savedLang&&savedLang!==lang) location.replace(location.pathname+'?lang='+savedLang);
if(params.has('lang')){{try{{localStorage.setItem('ytvault-lang',lang);}}catch(error){{}}}}
const q=document.querySelector('#q'),status=document.querySelector('#status'),empty=document.querySelector('#empty');
const normSearch=value=>value.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLocaleLowerCase(lang).replace(/\\s+/g,' ').trim();
function filterCards(){{
  const needle=normSearch(q.value), wanted=status.value; let visible=0;
  document.querySelectorAll('.group').forEach(group=>{{let inGroup=0;
    group.querySelectorAll('.card').forEach(card=>{{const show=(!needle||card.dataset.search.includes(needle))&&(!wanted||card.dataset.status===wanted);card.hidden=!show;if(show){{visible++;inGroup++;}}}});
    group.hidden=inGroup===0;if((needle||wanted)&&inGroup)group.open=true;
  }});empty.style.display=visible?'none':'block';
}}
q.addEventListener('input',filterCards);status.addEventListener('change',filterCards);
document.addEventListener('keydown',e=>{{if(e.key==='/'&&document.activeElement!==q){{e.preventDefault();q.focus();}}}});
const managerMessage=document.querySelector('#managerMessage');
function showMessage(text,isError=false){{managerMessage.textContent=text;managerMessage.className='manager-message show'+(isError?' error':'');}}
async function postJson(path,payload){{const response=await fetch(path,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});const data=await response.json();if(!response.ok)throw new Error(data.error||T.operation_failed);return data;}}
document.querySelector('#categoryForm').addEventListener('submit',async event=>{{event.preventDefault();const button=event.submitter;button.disabled=true;try{{await postJson('/api/categories',{{name:new FormData(event.currentTarget).get('name')}});location.reload();}}catch(error){{showMessage(error.message,true);button.disabled=false;}}}});
document.querySelector('#videoForm').addEventListener('submit',async event=>{{event.preventDefault();const button=event.submitter;button.disabled=true;showMessage(T.reading_links);const form=new FormData(event.currentTarget);try{{const result=await postJson('/api/videos/add',{{category_id:form.get('category_id'),urls:form.get('urls')}});showMessage(`${{T.added}}: ${{result.added}}. ${{T.existing}}: ${{result.existing}}.`);setTimeout(()=>location.reload(),700);}}catch(error){{showMessage(error.message,true);button.disabled=false;}}}});
const videoDialog=document.querySelector('#videoManageDialog'),videoManageForm=document.querySelector('#videoManageForm'),videoDialogError=document.querySelector('#videoDialogError');
function showDialogError(text=''){{videoDialogError.textContent=text;videoDialogError.classList.toggle('show',Boolean(text));}}
document.addEventListener('click',event=>{{const button=event.target.closest('[data-manage-video]');if(!button)return;const selected=new Set(button.dataset.categoryIds.split(',').filter(Boolean));videoManageForm.elements.video_id.value=button.dataset.manageVideo;document.querySelector('#managedVideoTitle').textContent=button.dataset.videoTitle;videoManageForm.querySelectorAll('[name="managed_categories"]').forEach(input=>{{input.checked=selected.has(input.value);}});document.querySelector('#deleteLocal').checked=false;showDialogError();videoDialog.showModal();}});
document.querySelector('[data-close-video-dialog]').addEventListener('click',()=>videoDialog.close());
videoManageForm.addEventListener('submit',async event=>{{event.preventDefault();const button=event.submitter;const categoryIds=[...videoManageForm.querySelectorAll('[name="managed_categories"]:checked')].map(input=>Number(input.value));if(!categoryIds.length){{showDialogError(T.select_category_error);return;}}button.disabled=true;try{{await postJson('/api/videos/categories',{{video_id:videoManageForm.elements.video_id.value,category_ids:categoryIds}});location.reload();}}catch(error){{showDialogError(error.message);button.disabled=false;}}}});
document.querySelector('#deleteVideoButton').addEventListener('click',async event=>{{const deleteLocal=document.querySelector('#deleteLocal').checked;if(!confirm(deleteLocal?T.confirm_delete_local:T.confirm_delete_record))return;const button=event.currentTarget;button.disabled=true;try{{const result=await postJson('/api/videos/delete',{{video_id:videoManageForm.elements.video_id.value,delete_local:deleteLocal}});if(result.file_errors?.length)alert(`${{T.cleanup_warning}}\\n${{result.file_errors.join('\\n')}}`);location.reload();}}catch(error){{showDialogError(error.message);button.disabled=false;}}}});
document.addEventListener('click',async event=>{{const videoButton=event.target.closest('[data-download-video]'),categoryButton=event.target.closest('[data-download-category]');if(!videoButton&&!categoryButton)return;const button=videoButton||categoryButton;button.disabled=true;button.textContent=T.queueing;try{{const result=await postJson(videoButton?'/api/download/video':'/api/download/category',videoButton?{{video_id:videoButton.dataset.downloadVideo}}:{{category_id:categoryButton.dataset.downloadCategory}});showMessage(`${{result.queued}} ${{T.queued}}`);setTimeout(()=>location.reload(),700);}}catch(error){{showMessage(error.message,true);button.disabled=false;button.textContent=videoButton?T.download:T.download_category;}}}});
"""
    return f'''<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#121212"><title>YT Vault · {esc(text['library'])}</title><style>{base_css()}</style></head><body>
<header><div class="head"><div class="top"><div><a class="brand" href="/?lang={lang}"><span class="brand-mark">▶</span>YT Vault</a><p class="tagline">{esc(text['tagline'])}</p></div>{language_nav(lang)}</div><div class="summary">{stats}</div><div class="controls"><label class="search-wrap"><span aria-hidden="true">⌕</span><input id="q" type="search" placeholder="{esc(text['search_placeholder'])}" autocomplete="off" aria-label="{esc(text['search'])}"></label><select id="status" aria-label="{esc(text['filter_status'])}"><option value="">{esc(text['all_statuses'])}</option><option value="done">{esc(text['only_available'])}</option><option value="remote">{esc(text['not_downloaded'])}</option><option value="running">{esc(text['downloading'])}</option><option value="pending">{esc(text['pending'])}</option><option value="error">{esc(text['failed'])}</option></select></div></div></header>
{manager}{video_dialog}<main>{''.join(cards_by_group)}<div id="empty" class="empty">{esc(text['empty'])}</div></main><script>{script}</script></body></html>'''


def watch_page(video_id, lang='es'):
    lang = normalize_lang(lang)
    text = UI_TEXT[lang]
    row = get_video(video_id)
    if not row:
        return None
    title = row['title'] or row['video_id']
    status = row['status']
    if status == 'done' and row['filename']:
        media_url = '/media/' + urllib.parse.quote(row['filename'])
        content = f'<video class="player" controls playsinline preload="metadata" poster="/thumb/{esc(video_id)}.jpg" src="{media_url}"></video>'
    else:
        labels = {'running': text['watch_running'], 'pending': text['watch_pending'], 'error': text['watch_error']}
        content = f'<div class="notice">{esc(labels.get(status, text["watch_unavailable"]))}</div>'
    error = ''
    if status == 'error' and (row['error'] or row['last_metadata_error']):
        error = f'<div class="notice error-notice">{esc(row["error"] or row["last_metadata_error"])}</div>'
    category_names = ', '.join(category['name'] for category in row['categories']) or display_group(row)
    status_label = text.get(f'status_{status}', status)
    persistence = f"""<script>const p=new URLSearchParams(location.search);let s=null;try{{s=localStorage.getItem('ytvault-lang');}}catch(e){{}}if(!p.has('lang')&&s&&s!=='{lang}')location.replace(location.pathname+'?lang='+s);if(p.has('lang')){{try{{localStorage.setItem('ytvault-lang','{lang}');}}catch(e){{}}}}</script>"""
    return f'''<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#121212"><title>{esc(title)} · YT Vault</title><style>{base_css()}</style></head><body><header><div class="head"><div class="top"><a class="brand" href="/?lang={lang}"><span class="brand-mark">▶</span>YT Vault</a>{language_nav(lang)}</div></div></header><main class="watch"><a class="back" href="/?lang={lang}">← {esc(text['back'])}</a>{content}<h1>{esc(title)}</h1><div class="watch-meta"><span class="badge status-{esc(status)}">{esc(status_label)}</span><span>{esc(category_names)}</span><span>{esc(format_duration(row['duration']))}</span><span>{esc(video_id)}</span></div><div class="actions"><a class="button" href="{esc(row['url'])}" target="_blank" rel="noreferrer">{esc(text['open_youtube'])}</a></div>{error}</main>{persistence}</body></html>'''


class WindowsTray:
    """Icono nativo de Windows sin dependencias de terceros."""

    def __init__(self, url, stop):
        self.url = url
        self.stop = stop
        self._wndproc = None
        self._hwnd = None
        self._nid = None
        self._icon = None
        self._owned_icons = []

    def run(self):
        import ctypes
        from ctypes import wintypes

        WM_DESTROY = 0x0002
        WM_COMMAND = 0x0111
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        WM_APP = 0x8000
        WM_TRAY = WM_APP + 1
        NIM_ADD, NIM_DELETE = 0x0, 0x2
        NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
        MF_STRING, MF_SEPARATOR = 0x0, 0x800
        TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
        IMAGE_ICON, LR_DEFAULTSIZE = 1, 0x40
        ID_OPEN, ID_EXIT = 1001, 1002

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ('style', wintypes.UINT), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT),
                ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT),
                ('hIcon', wintypes.HICON), ('szTip', wintypes.WCHAR * 128),
                ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD),
                ('szInfo', wintypes.WCHAR * 256), ('uTimeoutOrVersion', wintypes.UINT),
                ('szInfoTitle', wintypes.WCHAR * 64), ('dwInfoFlags', wintypes.DWORD),
                ('guidItem', ctypes.c_byte * 16), ('hBalloonIcon', wintypes.HICON),
            ]

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON), wintypes.UINT,
        ]
        shell32.ExtractIconExW.restype = wintypes.UINT
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        cleanup_errors = []

        def cleanup_bool(function, *args):
            try:
                if not function(*args):
                    raise ctypes.WinError()
            except Exception as exc:
                cleanup_errors.append(exc)

        def remove_tray_icon():
            nid = self._nid
            self._nid = None
            if nid is not None:
                cleanup_bool(shell32.Shell_NotifyIconW, NIM_DELETE, ctypes.byref(nid))

        def destroy_window(hwnd=None):
            current = self._hwnd
            if current is None or (hwnd is not None and current != hwnd):
                return
            self._hwnd = None
            cleanup_bool(user32.DestroyWindow, current)

        def release_owned_icons():
            owned_icons = self._owned_icons
            self._owned_icons = []
            self._icon = None
            for owned_icon in owned_icons:
                cleanup_bool(user32.DestroyIcon, owned_icon)

        def open_vault():
            webbrowser.open(self.url)

        def show_menu(hwnd):
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            menu = user32.CreatePopupMenu()
            if not menu:
                raise ctypes.WinError()
            try:
                user32.AppendMenuW(menu, MF_STRING, ID_OPEN, 'Open YT Vault')
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                user32.AppendMenuW(menu, MF_STRING, ID_EXIT, 'Exit YT Vault')
                user32.SetForegroundWindow(hwnd)
                command = user32.TrackPopupMenu(
                    menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                    point.x, point.y, 0, hwnd, None,
                )
                if command == ID_OPEN:
                    open_vault()
                elif command == ID_EXIT:
                    # Let run_application stop the server after the message loop exits.
                    # Calling server.shutdown() from this callback can block the UI
                    # thread and leaves callback failures invisible to Python.
                    user32.PostQuitMessage(0)
            finally:
                if not user32.DestroyMenu(menu):
                    raise ctypes.WinError()

        callback_errors = []

        def fail_callback(exc):
            # ctypes ignores exceptions escaping a callback.  Keep the first one for
            # the Python frame outside WNDPROC and wake GetMessageW best-effort.
            try:
                if not callback_errors:
                    callback_errors.append(exc)
            except BaseException:
                pass
            try:
                user32.PostQuitMessage(1)
            except BaseException:
                pass

        @WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            try:
                if message == WM_TRAY:
                    if lparam == WM_LBUTTONDBLCLK:
                        open_vault()
                    elif lparam == WM_RBUTTONUP:
                        show_menu(hwnd)
                    return 0
                if message == WM_COMMAND:
                    return 0
                if message == WM_DESTROY:
                    self._hwnd = None
                    remove_tray_icon()
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)
            except BaseException as exc:
                fail_callback(exc)
                return 0

        self._wndproc = wndproc
        instance = kernel32.GetModuleHandleW(None)
        class_name = f'YTVaultTrayWindow-{os.getpid()}'
        class_registered = False
        try:
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = wndproc
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            class_registered = True

            hwnd = user32.CreateWindowExW(
                0, class_name, 'YT Vault', 0, 0, 0, 0, 0, None, None, instance, None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self._hwnd = hwnd

            large = wintypes.HICON()
            small = wintypes.HICON()
            shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1)
            self._owned_icons = [handle for handle in (large.value, small.value) if handle]
            icon = small.value or large.value
            if not icon:
                icon = user32.LoadImageW(
                    None, ctypes.cast(32512, wintypes.LPCWSTR), IMAGE_ICON,
                    0, 0, LR_DEFAULTSIZE | 0x8000,
                )
            self._icon = icon
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = icon
            nid.szTip = 'YT Vault — double-click to open'
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError('Windows could not create the YT Vault tray icon.')
            self._nid = nid

            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError()
                if result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            if callback_errors:
                raise callback_errors[0]
        finally:
            already_failing = sys.exc_info()[0] is not None
            remove_tray_icon()
            destroy_window()
            release_owned_icons()
            if class_registered:
                class_registered = False
                cleanup_bool(user32.UnregisterClassW, class_name, instance)
            self._wndproc = None
            if cleanup_errors and not already_failing:
                raise cleanup_errors[0]


def run_application(server, url, use_tray=False, tray_factory=None):
    if not use_tray:
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return
    worker = threading.Thread(target=server.serve_forever, name='yt-vault-web', daemon=True)
    worker.start()
    try:
        tray = (tray_factory or WindowsTray)(url, server.shutdown)
        tray.run()
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors = []
        for operation in (
            server.shutdown,
            lambda: worker.join(timeout=5),
            server.server_close,
        ):
            try:
                operation()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0]


class H(BaseHTTPRequestHandler):
    def send_bytes(self, body, content_type, cache='no-store', status=200):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode(),
            'application/json; charset=utf-8', status=status,
        )

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise ValueError('Longitud de petición no válida.') from exc
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ValueError('La petición está vacía o es demasiado grande.')
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('El cuerpo JSON no es válido.') from exc
        if not isinstance(payload, dict):
            raise ValueError('El cuerpo JSON debe ser un objeto.')
        return payload

    def validate_mutating_request(self):
        content_type = self.headers.get('Content-Type', '').partition(';')[0].strip().casefold()
        if content_type != 'application/json':
            self.send_json({'error': 'Se requiere Content-Type application/json.'}, 415)
            return False
        source = self.headers.get('Origin') or self.headers.get('Referer')

        def origin_tuple(value):
            try:
                parsed = urllib.parse.urlparse(value)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                    return None
                if parsed.username is not None or parsed.password is not None:
                    return None
                default_port = 443 if parsed.scheme == 'https' else 80
                return parsed.scheme, parsed.hostname.casefold(), parsed.port or default_port
            except ValueError:
                return None

        configured = os.environ.get('VIDEOTECA_ALLOWED_ORIGINS') or os.environ.get('VIDEOTECA_ORIGIN')
        if configured:
            allowed = {origin_tuple(value.strip()) for value in configured.split(',') if value.strip()}
        else:
            port = self.server.server_address[1]
            allowed = {
                ('http', '127.0.0.1', port),
                ('http', 'localhost', port),
                ('http', '::1', port),
            }
        allowed.discard(None)
        if not source or origin_tuple(source) not in allowed:
            self.send_json({'error': 'Origen de petición no permitido.'}, 403)
            return False
        return True

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.validate_mutating_request():
            return
        init()
        try:
            payload = self.read_json()
            if path == '/api/categories':
                self.send_json({'category': create_category(payload.get('name'))}, 201)
                return
            if path == '/api/videos/add':
                self.send_json(add_video_urls(payload.get('category_id'), payload.get('urls')), 201)
                return
            if path == '/api/videos/categories':
                category_ids = set_video_categories(payload.get('video_id'), payload.get('category_ids'))
                self.send_json({'category_ids': category_ids})
                return
            if path == '/api/videos/delete':
                self.send_json(delete_video(
                    payload.get('video_id'), delete_local=payload.get('delete_local') is True
                ))
                return
            if path == '/api/download/video':
                queued = queue_video(payload.get('video_id'))
                if queued:
                    start_downloader()
                self.send_json({'queued': queued})
                return
            if path == '/api/download/category':
                queued = queue_category(payload.get('category_id'))
                if queued:
                    start_downloader()
                self.send_json({'queued': queued})
                return
            self.send_json({'error': 'Ruta no encontrada.'}, 404)
        except ValueError as exc:
            self.send_json({'error': str(exc)}, 400)
        except Exception as exc:
            print(f'Error en API {path}: {exc}', flush=True)
            self.send_json({'error': 'Error interno al procesar la operación.'}, 500)

    def serve_media(self, path, head_only=False):
        name = urllib.parse.unquote(path.removeprefix('/media/'))
        file_path = (VIDEOS / name).resolve()
        if not file_path.is_relative_to(VIDEOS.resolve()) or not file_path.is_file():
            self.send_error(404)
            return
        size = file_path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get('Range')
        status = 200
        if range_header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
            if not match:
                self.send_error(416)
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            elif match.group(2):
                length = int(match.group(2))
                start = max(0, size - length)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', mimetypes.guess_type(str(file_path))[0] or 'video/mp4')
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'private, max-age=3600')
        self.end_headers()
        if head_only:
            return
        try:
            with file_path.open('rb') as fh:
                fh.seek(start)
                remaining = length
                while remaining:
                    chunk = fh.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/media/'):
            self.serve_media(path, head_only=True)
        else:
            self.send_error(404)

    def do_GET(self):
        init()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        lang = normalize_lang(urllib.parse.parse_qs(parsed.query).get('lang', ['es'])[0])
        if path in ('/', '/index.html'):
            self.send_bytes(landing_page(lang).encode(), 'text/html; charset=utf-8')
            return
        if path.startswith('/watch/'):
            video_id = urllib.parse.unquote(path.removeprefix('/watch/'))
            page = watch_page(video_id, lang)
            if page is None:
                self.send_error(404)
            else:
                self.send_bytes(page.encode(), 'text/html; charset=utf-8')
            return
        if path == '/api/videos':
            body = json.dumps({'videos': api_video_rows()}, ensure_ascii=False).encode()
            self.send_bytes(body, 'application/json; charset=utf-8')
            return
        if path.startswith('/thumb/') and path.endswith('.jpg'):
            video_id = urllib.parse.unquote(path.removeprefix('/thumb/').removesuffix('.jpg'))
            if not _video_id_re.fullmatch(video_id):
                self.send_error(404)
                return
            thumb = THUMBS / f'{video_id}.jpg'
            if not thumb.is_file():
                self.send_error(404)
                return
            self.send_bytes(thumb.read_bytes(), 'image/jpeg', 'public, max-age=604800')
            return
        if path.startswith('/media/'):
            self.serve_media(path)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Videoteca local de YouTube')
    parser.add_argument('--host', default=os.environ.get('VIDEOTECA_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('VIDEOTECA_PORT', '8802')))
    parser.add_argument('--open-browser', action='store_true', help='Abre la portada en el navegador')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), H)
    try:
        init()
        resume_pending_downloads()
    except BaseException:
        server.server_close()
        raise
    browser_host = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
    url = f'http://{browser_host}:{args.port}'
    if sys.stdout is not None:
        print(url, flush=True)
    if args.open_browser or FROZEN:
        threading.Timer(.6, webbrowser.open, args=(url,)).start()
    run_application(server, url, use_tray=(os.name == 'nt' and FROZEN))
