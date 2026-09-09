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


def load_playlist(url):
    executable = find_tool('yt-dlp')
    proc = subprocess.run(
        [executable, '--flat-playlist', '--dump-single-json', '--no-warnings', url],
        text=True, capture_output=True, timeout=300,
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
        candidates = playlist_loader(value) if is_playlist else [
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


def find_tool(name):
    suffix = '.exe' if os.name == 'nt' else ''
    candidates = [
        shutil.which(name),
        Path.home() / '.local' / 'bin' / f'{name}{suffix}',
        BINARY_DIR / f'{name}{suffix}',
        SOURCE_DIR / '.venv' / ('Scripts' if os.name == 'nt' else 'bin') / f'{name}{suffix}',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(f'No se encontró {name}. Consulta las instrucciones de instalación del README.')


_downloader_thread = None
_downloader_lock = threading.Lock()


def _run_embedded_downloader():
    global _downloader_thread
    if not _downloader_lock.acquire(blocking=False):
        return
    try:
        import downloader
        downloader.main()
    except Exception as exc:
        print(f'Error del descargador: {exc}', flush=True)
    finally:
        _downloader_lock.release()
        _downloader_thread = None


def start_downloader():
    global _downloader_thread
    service = os.environ.get('VIDEOTECA_DOWNLOADER_SERVICE', '').strip()
    if service and os.name != 'nt':
        proc = subprocess.run(
            ['systemctl', '--user', 'start', service],
            text=True, capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or 'No se pudo iniciar el descargador').strip())
        return
    if _downloader_thread and _downloader_thread.is_alive():
        return
    _downloader_thread = threading.Thread(target=_run_embedded_downloader, name='descargador', daemon=True)
    _downloader_thread.start()


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
:root{color-scheme:dark;--bg:#080d1a;--panel:#111a31;--panel2:#17213d;--line:#2b3a68;--text:#eef3ff;--muted:#aab6d5;--accent:#74b9ff;--ok:#38d996;--warn:#ffd166;--bad:#ff7583}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#17213d 0,#080d1a 32rem);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}a{color:inherit}header{position:sticky;top:0;z-index:10;background:#0d1428ee;backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}.head{max-width:1500px;margin:auto;padding:14px 18px}.top{display:flex;align-items:center;justify-content:space-between;gap:12px}.brand{font-size:1.35rem;font-weight:800;text-decoration:none}.summary{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.pill,.badge{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;background:#202d52;padding:4px 9px;color:#dce7ff;font-size:.78rem}.controls{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:10px;margin-top:12px}.controls input,.controls select{width:100%;border:1px solid #415488;border-radius:12px;background:#111a31;color:var(--text);padding:12px 14px;font-size:1rem;outline:none}.controls input:focus,.controls select:focus{border-color:var(--accent);box-shadow:0 0 0 3px #74b9ff22}main{max-width:1500px;margin:auto;padding:18px}.group{margin:0 0 12px;border:1px solid var(--line);border-radius:15px;background:#0d1428aa;overflow:hidden}.group summary{cursor:pointer;list-style:none;padding:16px 18px;font-size:1.08rem;font-weight:800;display:flex;gap:8px;align-items:center;user-select:none}.group summary::-webkit-details-marker{display:none}.group summary::before{content:'›';font-size:1.55rem;line-height:.7;color:var(--accent);transition:transform .16s}.group[open] summary::before{transform:rotate(90deg)}.group summary:hover{background:#17213d}.group .grid{padding:0 14px 16px}.count{color:var(--muted);font-weight:500}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.card{min-width:0;display:block;background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:15px;overflow:hidden;text-decoration:none;box-shadow:0 8px 25px #0003;transition:transform .16s,border-color .16s}.card:hover{transform:translateY(-2px);border-color:#6487ca}.card.done{border-color:#2f8e6b88}.card.running{border-color:#c99b36}.card.error{border-color:#b54e5a}.thumb{position:relative;aspect-ratio:16/9;background:linear-gradient(135deg,#1c294a,#0c1326);overflow:hidden}.thumb img{width:100%;height:100%;object-fit:cover;display:block}.thumb.placeholder{display:grid;place-items:center;color:#8291b7;font-size:2.2rem}.play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:#050814cc;border:1px solid #ffffffaa;font-size:1.15rem;padding-left:3px}.duration{position:absolute;right:7px;bottom:7px;background:#050814df;border-radius:6px;padding:3px 6px;font-size:.75rem}.body{padding:12px}.meta{display:flex;gap:6px;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:.75rem}.status-done{background:#123e31;border-color:#237b59}.status-running{background:#4a3914;border-color:#a37a21}.status-error{background:#4c2027;border-color:#9e4550}.title{font-size:.95rem;line-height:1.35;margin:9px 0 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.empty{display:none;text-align:center;color:var(--muted);padding:55px 10px}.back{display:inline-flex;text-decoration:none;color:var(--accent);margin-bottom:16px}.watch{max-width:1100px}.player{width:100%;max-height:75vh;background:#000;border-radius:16px;box-shadow:0 15px 50px #0007}.watch h1{font-size:clamp(1.25rem,3vw,2rem);line-height:1.25}.watch-meta{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);margin:12px 0 20px}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.button{display:inline-flex;text-decoration:none;border:1px solid var(--line);background:#1b2a4c;padding:10px 14px;border-radius:11px;color:#dfe9ff}.notice{padding:18px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}
.card{display:flex;flex-direction:column}.card[hidden],.group[hidden]{display:none!important}.card-main{display:block;flex:1;text-decoration:none;color:inherit}.card-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;padding:0 10px 10px}.card-actions a{min-width:0;text-align:center;text-decoration:none;border:1px solid #415488;border-radius:9px;background:#1b2a4c;color:#dfe9ff;padding:7px 5px;font-size:.76rem;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.card-actions .youtube{border-color:#c94b55;background:#581e27;color:#fff}.card-actions a:hover{filter:brightness(1.16)}
.card-actions button,.group-actions button,.manager button{border:1px solid #3e74bd;border-radius:9px;background:#17477c;color:#fff;padding:7px 6px;font:inherit;font-size:.76rem;font-weight:800;cursor:pointer}.card-actions .download,.card-actions .manage-video{grid-column:1/-1}.group-actions{padding:0 14px 12px}.group-actions button{padding:9px 12px}.manager{max-width:1500px;margin:18px auto 0;border:1px solid #3b5388;border-radius:15px;background:#101a32;overflow:hidden}.manager>summary{cursor:pointer;padding:15px 18px;font-weight:850;color:#dfe9ff}.manager-grid{display:grid;grid-template-columns:minmax(0,.7fr) minmax(0,1.3fr);gap:14px;padding:0 16px 16px}.manager-card{min-width:0;background:#0b1226;border:1px solid var(--line);border-radius:13px;padding:14px}.manager-card h2{font-size:1rem;margin:0 0 12px}.manager-card label{display:grid;gap:6px;margin-bottom:11px;color:var(--muted);font-size:.82rem}.manager-card input,.manager-card select,.manager-card textarea{width:100%;min-width:0;border:1px solid #415488;border-radius:10px;background:#111a31;color:var(--text);padding:10px 11px;font:inherit}.manager-card textarea{resize:vertical}.manager-message{display:none;margin:0 16px 16px;padding:10px 12px;border-radius:10px;background:#123e31;color:#c9f7e2}.manager-message.error{background:#4c2027;color:#ffd6da}.manager-message.show{display:block}
.video-dialog{width:min(560px,calc(100vw - 24px));max-height:90vh;overflow:auto;border:1px solid #526da8;border-radius:16px;background:#101a32;color:var(--text);padding:0;box-shadow:0 24px 80px #000a}.video-dialog::backdrop{background:#030712cc;backdrop-filter:blur(3px)}.dialog-body{padding:18px}.dialog-body h2{margin:0 0 5px;font-size:1.2rem}.dialog-title{color:var(--muted);margin:0 0 16px;word-break:break-word}.category-checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0 18px}.category-check{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:10px;background:#0b1226;padding:9px;min-width:0}.category-check input{width:18px;height:18px;flex:0 0 auto}.category-check span{overflow:hidden;text-overflow:ellipsis}.dialog-actions{display:flex;gap:8px;flex-wrap:wrap}.dialog-actions button{border:1px solid #3e74bd;border-radius:10px;background:#17477c;color:#fff;padding:10px 12px;font:inherit;font-weight:800;cursor:pointer}.dialog-actions .secondary{background:#1b2a4c;border-color:#415488}.delete-zone{margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}.delete-option{display:flex;align-items:flex-start;gap:9px;color:#ffd9dd;font-size:.9rem;margin:12px 0}.delete-option input{width:18px;height:18px;flex:0 0 auto}.dialog-actions .danger{background:#7a2631;border-color:#dc6170}.dialog-error{display:none;color:#ffd6da;background:#4c2027;border-radius:9px;padding:9px;margin:10px 0}.dialog-error.show{display:block}
@media(max-width:650px){.head{padding:11px 12px}.top{align-items:flex-start}.brand{font-size:1.12rem}.summary .pill:nth-last-child(-n+2){display:none}.controls{grid-template-columns:1fr}.controls input,.controls select{padding:10px 12px}.manager{margin:12px 10px 0}.manager-grid{grid-template-columns:1fr;padding:0 10px 10px}main{padding:14px 10px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.body{padding:9px}.title{font-size:.84rem}.meta{font-size:.68rem}.play{width:40px;height:40px}.card-actions{gap:5px;padding:0 7px 7px}.card-actions a,.card-actions button{font-size:.68rem;padding:6px 3px}.category-checks{grid-template-columns:1fr}.watch{padding:14px}.player{border-radius:10px}}
"""


def landing_page():
    all_rows = rows()
    categories = list_categories()
    counts = {}
    seen_videos = set()
    membership_map = {}
    groups = {category['name']: [] for category in categories}
    category_ids = {category['name']: category['id'] for category in categories}
    for r in all_rows:
        membership_map.setdefault(r['video_id'], []).append(r['view_category_id'])
        if r['video_id'] not in seen_videos:
            seen_videos.add(r['video_id'])
            counts[r['status']] = counts.get(r['status'], 0) + 1
        groups.setdefault(display_group(r), []).append(r)

    cards_by_group = []
    status_labels = {'done': 'Disponible', 'remote': 'Solo en YouTube', 'pending': 'En cola', 'running': 'Descargando', 'error': 'Falló'}
    for group, items in groups.items():
        cards = []
        for r in items:
            title = r['title'] or r['video_id']
            status = r['status']
            duration = format_duration(r['duration'])
            if status == 'done' and r['filename']:
                visual = (
                    f'<div class="thumb"><img loading="lazy" decoding="async" width="640" height="360" '
                    f'src="/thumb/{esc(r["video_id"])}.jpg" alt="" '
                    f'onerror="this.closest(\'.thumb\').classList.add(\'placeholder\');this.remove()">'
                    f'<span class="play" aria-hidden="true">▶</span>'
                    + (f'<span class="duration">{esc(duration)}</span>' if duration else '') + '</div>'
                )
            else:
                visual = '<div class="thumb placeholder" aria-hidden="true">⌛</div>'
            search_text = re.sub(r'\s+', ' ', folded(f'{group} {title} {r["video_id"]}')).strip()
            cards.append(
                f'<article class="card {esc(status)}" data-status="{esc(status)}" '
                f'data-search="{esc(search_text)}"><a class="card-main" href="/watch/{esc(r["video_id"])}">{visual}'
                f'<div class="body"><div class="meta"><span class="badge status-{esc(status)}">'
                f'{esc(status_labels.get(status, status))}</span><span>{esc(r["video_id"])}</span></div>'
                f'<h3 class="title">{esc(title)}</h3></div></a>'
                f'<div class="card-actions"><a href="/watch/{esc(r["video_id"])}">Ver local</a>'
                f'<a class="youtube" href="{esc(r["url"])}" target="_blank" rel="noreferrer">YouTube ↗</a>'
                + (f'<button class="download" type="button" data-download-video="{esc(r["video_id"])}">Descargar</button>' if status in ('remote', 'error') else '')
                + f'<button class="manage-video" type="button" data-manage-video="{esc(r["video_id"])}" '
                  f'data-video-title="{esc(title)}" data-category-ids="{",".join(str(value) for value in membership_map[r["video_id"]] if value is not None)}">Organizar / borrar</button>'
                + '</div></article>'
            )
        category_id = items[0]['view_category_id'] if items else category_ids.get(group)
        cards_by_group.append(
            f'<details class="group" data-group="{esc(group.casefold())}"><summary>{esc(group)} '
            f'<span class="count">{len(items)}</span></summary>'
            + (f'<div class="group-actions"><button type="button" data-download-category="{category_id}">Descargar toda la categoría</button></div>' if category_id else '')
            + f'<div class="grid">{"".join(cards)}</div></details>'
        )

    summary = (
        f'<span class="pill">Total: {len(seen_videos)}</span>'
        f'<span class="pill">Disponibles: {counts.get("done", 0)}</span>'
        f'<span class="pill">Sin descargar: {counts.get("remote", 0)}</span>'
        f'<span class="pill">Descargando: {counts.get("running", 0)}</span>'
        f'<span class="pill">Pendientes: {counts.get("pending", 0)}</span>'
        f'<span class="pill">Fallidos: {counts.get("error", 0)}</span>'
    )
    category_options = ''.join(
        f'<option value="{category["id"]}">{esc(category["name"])}</option>' for category in categories
    )
    category_checkboxes = ''.join(
        f'<label class="category-check"><input type="checkbox" name="managed_categories" '
        f'value="{category["id"]}"><span>{esc(category["name"])}</span></label>'
        for category in categories
    )
    manager = f'''<details class="manager"><summary>Gestionar videoteca</summary><div class="manager-grid">
<form id="categoryForm" class="manager-card"><h2>Crear categoría</h2><label>Nombre<input name="name" required maxlength="80" placeholder="Ej. Tafsir 26/27"></label><button type="submit">Crear categoría</button></form>
<form id="videoForm" class="manager-card"><h2>Añadir vídeos</h2><label>Categoría<select name="category_id" required><option value="" selected disabled>Selecciona una categoría</option>{category_options}</select></label><label>Vídeos o playlists<textarea name="urls" required rows="5" placeholder="Pega un vídeo, una playlist o varias URLs (una por línea)"></textarea></label><button type="submit">Añadir a la videoteca</button></form>
</div><p id="managerMessage" class="manager-message" role="status"></p></details>'''
    video_dialog = f'''<dialog id="videoManageDialog" class="video-dialog"><form id="videoManageForm" class="dialog-body">
<input type="hidden" name="video_id"><h2>Organizar o borrar vídeo</h2><p id="managedVideoTitle" class="dialog-title"></p>
<strong>Categorías</strong><div class="category-checks">{category_checkboxes}</div>
<p id="videoDialogError" class="dialog-error" role="alert"></p>
<div class="dialog-actions"><button type="submit">Guardar categorías</button><button type="button" class="secondary" data-close-video-dialog>Cancelar</button></div>
<div class="delete-zone"><strong>Eliminar de la videoteca</strong><label class="delete-option"><input id="deleteLocal" type="checkbox">Eliminar también el archivo de vídeo y su miniatura del disco</label>
<div class="dialog-actions"><button id="deleteVideoButton" type="button" class="danger">Borrar vídeo</button></div></div>
</form></dialog>'''
    script = """
const q=document.querySelector('#q'),status=document.querySelector('#status'),empty=document.querySelector('#empty');
const normSearch=value=>value.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase('es').replace(/\\s+/g,' ').trim();
function filterCards(){
  const needle=normSearch(q.value), wanted=status.value; let visible=0;
  document.querySelectorAll('.group').forEach(group=>{let inGroup=0;
    group.querySelectorAll('.card').forEach(card=>{const show=(!needle||card.dataset.search.includes(needle))&&(!wanted||card.dataset.status===wanted);card.hidden=!show;if(show){visible++;inGroup++;}});
    group.hidden=inGroup===0;
    if((needle||wanted)&&inGroup) group.open=true;
  });
  empty.style.display=visible?'none':'block';
}
q.addEventListener('input',filterCards);status.addEventListener('change',filterCards);
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}});
const managerMessage=document.querySelector('#managerMessage');
function showMessage(text,isError=false){managerMessage.textContent=text;managerMessage.className='manager-message show'+(isError?' error':'');}
async function postJson(path,payload){
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||'No se pudo completar la operación.');
  return data;
}
document.querySelector('#categoryForm').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await postJson('/api/categories',{name:new FormData(event.currentTarget).get('name')});location.reload();}catch(error){showMessage(error.message,true);button.disabled=false;}});
document.querySelector('#videoForm').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;showMessage('Leyendo enlaces y playlists…');const form=new FormData(event.currentTarget);try{const result=await postJson('/api/videos/add',{category_id:form.get('category_id'),urls:form.get('urls')});showMessage(`Añadidos: ${result.added}. Ya existentes: ${result.existing}.`);setTimeout(()=>location.reload(),700);}catch(error){showMessage(error.message,true);button.disabled=false;}});
const videoDialog=document.querySelector('#videoManageDialog'),videoManageForm=document.querySelector('#videoManageForm'),videoDialogError=document.querySelector('#videoDialogError');
function showDialogError(text=''){videoDialogError.textContent=text;videoDialogError.classList.toggle('show',Boolean(text));}
document.addEventListener('click',event=>{const button=event.target.closest('[data-manage-video]');if(!button)return;const selected=new Set(button.dataset.categoryIds.split(',').filter(Boolean));videoManageForm.elements.video_id.value=button.dataset.manageVideo;document.querySelector('#managedVideoTitle').textContent=button.dataset.videoTitle;videoManageForm.querySelectorAll('[name="managed_categories"]').forEach(input=>{input.checked=selected.has(input.value);});document.querySelector('#deleteLocal').checked=false;showDialogError();videoDialog.showModal();});
document.querySelector('[data-close-video-dialog]').addEventListener('click',()=>videoDialog.close());
videoManageForm.addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;const categoryIds=[...videoManageForm.querySelectorAll('[name="managed_categories"]:checked')].map(input=>Number(input.value));if(!categoryIds.length){showDialogError('Selecciona al menos una categoría.');return;}button.disabled=true;try{await postJson('/api/videos/categories',{video_id:videoManageForm.elements.video_id.value,category_ids:categoryIds});location.reload();}catch(error){showDialogError(error.message);button.disabled=false;}});
document.querySelector('#deleteVideoButton').addEventListener('click',async event=>{const deleteLocal=document.querySelector('#deleteLocal').checked;const message=deleteLocal?'Se borrará el vídeo de la videoteca Y TAMBIÉN su archivo local. ¿Continuar?':'Se borrará el vídeo de la videoteca, pero se conservará el archivo local. ¿Continuar?';if(!confirm(message))return;const button=event.currentTarget;button.disabled=true;try{const result=await postJson('/api/videos/delete',{video_id:videoManageForm.elements.video_id.value,delete_local:deleteLocal});if(result.file_errors?.length)alert(`El registro se borró, pero algunos archivos no pudieron eliminarse:\\n${result.file_errors.join('\\n')}`);location.reload();}catch(error){showDialogError(error.message);button.disabled=false;}});
document.addEventListener('click',async event=>{
  const videoButton=event.target.closest('[data-download-video]'), categoryButton=event.target.closest('[data-download-category]');
  if(!videoButton&&!categoryButton)return;
  const button=videoButton||categoryButton;button.disabled=true;button.textContent='Encolando…';
  try{const result=await postJson(videoButton?'/api/download/video':'/api/download/category',videoButton?{video_id:videoButton.dataset.downloadVideo}:{category_id:categoryButton.dataset.downloadCategory});showMessage(`${result.queued} vídeo(s) enviados a la cola.`);setTimeout(()=>location.reload(),700);}catch(error){showMessage(error.message,true);button.disabled=false;button.textContent=videoButton?'Descargar':'Descargar toda la categoría';}
});
"""
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Videoteca</title><style>{base_css()}</style></head><body>
<header><div class="head"><div class="top"><a class="brand" href="/">Videoteca</a><span class="pill">Descarga secuencial</span></div><div class="summary">{summary}</div><div class="controls"><input id="q" type="search" placeholder="Buscar clase, tema o identificador…" autocomplete="off" aria-label="Buscar vídeos"><select id="status" aria-label="Filtrar por estado"><option value="">Todos los estados</option><option value="done">Solo disponibles</option><option value="remote">Sin descargar</option><option value="running">Descargando</option><option value="pending">Pendientes</option><option value="error">Fallidos</option></select></div></div></header>
{manager}{video_dialog}<main>{''.join(cards_by_group)}<div id="empty" class="empty">No hay vídeos que coincidan con la búsqueda.</div></main><script>{script}</script></body></html>'''


def watch_page(video_id):
    r = get_video(video_id)
    if not r:
        return None
    title = r['title'] or r['video_id']
    status = r['status']
    if status == 'done' and r['filename']:
        media_url = '/media/' + urllib.parse.quote(r['filename'])
        content = f'<video class="player" controls playsinline preload="metadata" poster="/thumb/{esc(video_id)}.jpg" src="{media_url}"></video>'
    else:
        labels = {'running': 'Este vídeo se está descargando ahora.', 'pending': 'Este vídeo todavía está pendiente.', 'error': 'La descarga falló y volverá a intentarse.'}
        content = f'<div class="notice">{esc(labels.get(status, "El vídeo aún no está disponible disponible."))}</div>'
    error = ''
    if status == 'error' and (r['error'] or r['last_metadata_error']):
        error = f'<div class="notice" style="margin-top:12px;color:#ffc2c8">{esc(r["error"] or r["last_metadata_error"])}</div>'
    category_names = ', '.join(category['name'] for category in r['categories']) or display_group(r)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Videoteca</title><style>{base_css()}</style></head><body><header><div class="head"><a class="brand" href="/">Videoteca</a></div></header><main class="watch"><a class="back" href="/">← Volver a la videoteca</a>{content}<h1>{esc(title)}</h1><div class="watch-meta"><span class="badge status-{esc(status)}">{esc(status)}</span><span>{esc(category_names)}</span><span>{esc(format_duration(r['duration']))}</span><span>{esc(video_id)}</span></div><div class="actions"><a class="button" href="{esc(r['url'])}" target="_blank" rel="noreferrer">Abrir en YouTube</a></div>{error}</main></body></html>'''


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
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            self.send_bytes(landing_page().encode(), 'text/html; charset=utf-8')
            return
        if path.startswith('/watch/'):
            video_id = urllib.parse.unquote(path.removeprefix('/watch/'))
            page = watch_page(video_id)
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
    init()
    server = ThreadingHTTPServer((args.host, args.port), H)
    browser_host = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
    url = f'http://{browser_host}:{args.port}'
    print(url, flush=True)
    if args.open_browser or FROZEN:
        threading.Timer(.6, webbrowser.open, args=(url,)).start()
    server.serve_forever()
