#!/usr/bin/env python3
from __future__ import annotations
import contextlib, datetime, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request, zipfile
from pathlib import Path
SOURCE_DIR=Path(__file__).resolve().parent; FROZEN=bool(getattr(sys,'frozen',False)); BINARY_DIR=Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR
DEFAULT_ROOT=(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'YTVault') if FROZEN else SOURCE_DIR
ROOT=Path(os.environ.get('VIDEOTECA_HOME',DEFAULT_ROOT)).expanduser().resolve(); DB=ROOT/'data'/'portal.sqlite3'; VIDEOS=ROOT/'videos'; THUMBS=ROOT/'thumbs'; TMP=ROOT/'tmp'; LOG=ROOT/'logs'/'downloader.log'; TOOLS=ROOT/'tools'
WINDOWS_TOOL_URLS={
    'yt_dlp': (
        'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe',
        'https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS',
    ),
    'ffmpeg': (
        'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
        'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256',
    ),
    'deno': (
        'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip',
        'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip.sha256sum',
    ),
}
MAX_TOOL_DOWNLOAD=300*1024*1024
_tool_install_lock=threading.Lock()
SCHEMA="\nCREATE TABLE IF NOT EXISTS videos(\n  video_id TEXT PRIMARY KEY,\n  url TEXT NOT NULL,\n  source TEXT,\n  title TEXT,\n  filename TEXT,\n  status TEXT NOT NULL DEFAULT 'pending',\n  error TEXT,\n  attempts INTEGER NOT NULL DEFAULT 0,\n  duration REAL,\n  filesize INTEGER,\n  priority INTEGER NOT NULL DEFAULT 0,\n  category TEXT,\n  upload_date TEXT,\n  metadata_attempts INTEGER NOT NULL DEFAULT 0,\n  last_metadata_error TEXT,\n  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,\n  created_at TEXT DEFAULT CURRENT_TIMESTAMP\n);\nCREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);\nCREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source);\nCREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(priority DESC, title COLLATE NOCASE);\n"
# La cola es siempre secuencial. Entre descargas correctas se aplica una pausa
# corta; los fallos definitivos pasan a error y no bloquean los vídeos siguientes.
SUCCESS_DELAY=int(os.environ.get('YOUTUBE_SUCCESS_DELAY','30'))
def log(msg):
    LOG.parent.mkdir(parents=True,exist_ok=True)
    line=f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n"
    print(line,end='',flush=True)
    with LOG.open('a',encoding='utf-8') as output:
        output.write(line)
@contextlib.contextmanager
def con():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000')
    try:
        with c: yield c
    finally:
        c.close()
def ensure_cols(c):
    cols={r[1] for r in c.execute('pragma table_info(videos)')}
    for name, typ, default in [('priority','INTEGER','0'),('category','TEXT','NULL'),('upload_date','TEXT','NULL'),('metadata_attempts','INTEGER','0'),('last_metadata_error','TEXT','NULL')]:
        if name not in cols: c.execute(f'ALTER TABLE videos ADD COLUMN {name} {typ} DEFAULT {default}')
def init():
    (ROOT/'data').mkdir(exist_ok=True); VIDEOS.mkdir(exist_ok=True); THUMBS.mkdir(exist_ok=True); TMP.mkdir(exist_ok=True)
    with con() as c:
        # La tabla ya existía antes de añadir priority/category; primero
        # aseguramos columnas y luego los índices que dependen de ellas.
        try:
            c.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            if 'priority' not in str(e):
                raise
        ensure_cols(c)
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(priority DESC, title COLLATE NOCASE)')
        seed=SOURCE_DIR/'seed_urls.json'
        for x in (json.loads(seed.read_text(encoding='utf-8')) if seed.is_file() else []):
            c.execute('insert or ignore into videos(video_id,url,source,status) values(?,?,?,?)',(x['video_id'],x['url'],x['source'],'pending'))
        c.execute("update videos set status='pending' where status='running'")
def update(vid, **kw):
    if not kw: return
    with con() as c: c.execute('update videos set '+', '.join(k+'=?' for k in kw)+', updated_at=CURRENT_TIMESTAMP where video_id=?', (*kw.values(),vid))
def rows(q):
    with con() as c: return c.execute(q).fetchall()
def existing_file(vid):
    hits=list(VIDEOS.glob(f'*--{vid}.mp4')); return hits[0] if hits else None
def transient(err):
    e=(err or '').lower(); return any(x in e for x in ['429','too many requests','rate','timeout','timed out','temporarily','try again','http error 403','forbidden','request limit','unavailable','reset by peer'])
def sanitize(s): return (re.sub(r'[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ._ -]+','',s or '').strip(' ._-')[:130] or 'video')
def run(cmd, timeout=None, platform=None):
    platform=platform or os.name
    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0x08000000) if platform=='nt' else 0
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding='utf-8',
        errors='replace',
        capture_output=True,
        timeout=timeout,
        creationflags=creationflags,
    )

def _expected_sha256(payload, filename):
    text=payload.decode('ascii','strict')
    candidates=[]
    for line in text.splitlines():
        match=re.search(r'(?i)\b([0-9a-f]{64})\b',line)
        if match:
            candidates.append((match.group(1).lower(),line.casefold()))
    wanted=filename.casefold()
    for digest,line in candidates:
        if wanted in line:
            return digest
    if len(candidates)==1:
        return candidates[0][0]
    raise RuntimeError(f'No se encontró el SHA-256 de {filename}')

class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.casefold() != 'https':
            raise RuntimeError(f'Redirección no HTTPS bloqueada: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https(opener, url, timeout=120):
    if urllib.parse.urlsplit(url).scheme.casefold() != 'https':
        raise RuntimeError(f'La descarga requiere HTTPS: {url}')
    request=urllib.request.Request(url,headers={'User-Agent':'YTVault/1.0'})
    response=opener(request,timeout=timeout)
    final_url=response.geturl()
    if urllib.parse.urlsplit(final_url).scheme.casefold()!='https':
        response.close()
        raise RuntimeError(f'Redirección no segura al descargar {url}')
    return response

def _read_limited(response, maximum):
    declared=response.headers.get('Content-Length')
    if declared:
        try:
            if int(declared)>maximum:
                raise RuntimeError('La descarga supera el tamaño permitido')
        except ValueError as exc:
            raise RuntimeError('Content-Length no válido') from exc
    chunks=[]; total=0
    while True:
        chunk=response.read(min(1024*1024,maximum-total+1))
        if not chunk:
            return b''.join(chunks)
        total+=len(chunk)
        if total>maximum:
            raise RuntimeError('La descarga supera el tamaño permitido')
        chunks.append(chunk)

def _download_verified(opener, url, checksum_url, target, checksum_name):
    with _open_https(opener,checksum_url,30) as response:
        expected=_expected_sha256(_read_limited(response,1024*1024),checksum_name)
    digest=hashlib.sha256(); total=0
    with _open_https(opener,url,300) as response, target.open('wb') as output:
        declared=response.headers.get('Content-Length')
        if declared:
            try:
                if int(declared)>MAX_TOOL_DOWNLOAD:
                    raise RuntimeError('La descarga supera el tamaño permitido')
            except ValueError as exc:
                raise RuntimeError('Content-Length no válido') from exc
        while True:
            chunk=response.read(1024*1024)
            if not chunk:
                break
            total+=len(chunk)
            if total>MAX_TOOL_DOWNLOAD:
                raise RuntimeError('La descarga supera el tamaño permitido')
            digest.update(chunk); output.write(chunk)
    if digest.hexdigest()!=expected:
        target.unlink(missing_ok=True)
        raise RuntimeError(f'Falló la verificación SHA-256 de {checksum_name}')

def _extract_executable(archive_path, executable, target):
    with zipfile.ZipFile(archive_path) as archive:
        members=[item for item in archive.infolist() if Path(item.filename).name.casefold()==executable.casefold()]
        if len(members)!=1:
            raise RuntimeError(f'No se encontró una única copia de {executable} en el ZIP')
        member=members[0]
        if member.is_dir() or member.file_size>MAX_TOOL_DOWNLOAD:
            raise RuntimeError(f'Entrada ZIP no válida para {executable}')
        with archive.open(member) as source, target.open('wb') as output:
            shutil.copyfileobj(source,output,1024*1024)

def install_windows_tools(tools_dir=TOOLS, opener=None, urls=None, required=None):
    opener=opener or urllib.request.build_opener(HTTPSOnlyRedirectHandler()).open
    urls=WINDOWS_TOOL_URLS if urls is None else urls
    required=set(urls) if required is None else set(required)
    unknown=required-set(urls)
    if unknown:
        raise ValueError(f'Herramientas desconocidas: {", ".join(sorted(unknown))}')
    tools_dir=Path(tools_dir); tools_dir.mkdir(parents=True,exist_ok=True)
    installed=[]
    with _tool_install_lock:
        with tempfile.TemporaryDirectory(prefix='.install-',dir=tools_dir) as temp_name:
            staging=Path(temp_name); prepared={}
            if 'yt_dlp' in required and not (tools_dir/'yt-dlp.exe').is_file():
                archive=staging/'yt-dlp.exe'
                _download_verified(opener,*urls['yt_dlp'],archive,'yt-dlp.exe')
                prepared['yt-dlp']=archive
            if 'ffmpeg' in required and (not (tools_dir/'ffmpeg.exe').is_file() or not (tools_dir/'ffprobe.exe').is_file()):
                archive=staging/'ffmpeg.zip'
                _download_verified(opener,*urls['ffmpeg'],archive,'ffmpeg.zip')
                for name in ('ffmpeg.exe','ffprobe.exe'):
                    extracted=staging/name
                    _extract_executable(archive,name,extracted)
                    prepared[name.removesuffix('.exe')]=extracted
            if 'deno' in required and not (tools_dir/'deno.exe').is_file():
                archive=staging/'deno.zip'
                _download_verified(opener,*urls['deno'],archive,'deno-x86_64-pc-windows-msvc.zip')
                extracted=staging/'deno.exe'
                _extract_executable(archive,'deno.exe',extracted)
                prepared['deno']=extracted
            for name,source in prepared.items():
                os.replace(source,tools_dir/f'{name}.exe')
                installed.append(name)
    return installed

def _tool_candidates(name, platform, frozen=False):
    suffix='.exe' if platform=='nt' else ''
    managed=[TOOLS/f'{name}{suffix}',BINARY_DIR/f'{name}{suffix}']
    external=[
        Path.home()/'.local'/'bin'/f'{name}{suffix}',
        SOURCE_DIR/'.venv'/('Scripts' if platform=='nt' else 'bin')/f'{name}{suffix}',
        shutil.which(name),
    ]
    return managed+external if platform=='nt' and frozen else [external[-1],*managed,*external[:-1]]

def tool(name, platform=None, frozen=None, installer=None):
    platform=platform or os.name; frozen=FROZEN if frozen is None else frozen
    for candidate in _tool_candidates(name,platform,frozen):
        if candidate and Path(candidate).is_file(): return str(candidate)
    if platform=='nt' and frozen:
        if installer:
            installer()
        else:
            component={'yt-dlp':'yt_dlp','ffmpeg':'ffmpeg','ffprobe':'ffmpeg','deno':'deno'}.get(name,name)
            install_windows_tools(required={component})
        for candidate in _tool_candidates(name,platform,frozen):
            if candidate and Path(candidate).is_file(): return str(candidate)
    raise RuntimeError(f'No se encontró {name}. Consulta el registro {LOG}.')

def ytdlp(): return tool('yt-dlp')
def ffmpeg(): return tool('ffmpeg')
def ffprobe(): return tool('ffprobe')
def javascript_runtime(required=True):
    errors=[]
    for name in ('deno','node','quickjs','bun'):
        try: return name,tool(name)
        except Exception as exc: errors.append(str(exc))
    if required:
        raise RuntimeError('No se encontró un runtime JavaScript compatible (Deno, Node, QuickJS o Bun)')
    return None
def ensure_runtime_tools(platform=None, frozen=None):
    platform=platform or os.name; frozen=FROZEN if frozen is None else frozen
    ytdlp(); ffmpeg(); ffprobe()
    if platform=='nt' and frozen:
        javascript_runtime()

def make_thumbnail(video, vid):
    """Crea un JPEG ligero para que la portada nunca tenga que abrir el MP4."""
    target=THUMBS/f'{vid}.jpg'
    if target.is_file() and target.stat().st_size>1000: return target
    tmp=THUMBS/f'.{vid}.tmp.jpg'
    p=run([
        ffmpeg(),'-hide_banner','-loglevel','error','-y','-ss','3',
        '-i',str(video),'-vf','thumbnail=30,scale=640:-2','-frames:v','1',
        '-q:v','4',str(tmp)
    ], timeout=180)
    if p.returncode!=0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError((p.stderr or p.stdout or 'No se pudo crear la miniatura')[-1000:])
    os.replace(tmp,target)
    return target
def common():
    command=[ytdlp()]
    runtime=javascript_runtime(required=False)
    if runtime:
        runtime_name,runtime_path=runtime
        command.extend([
            '--js-runtimes',f'{runtime_name}:{runtime_path}',
        ])
    command.append('--no-playlist')
    return command
def classify(title, source):
    t=(title or '').lower(); s=(source or '').lower()
    arabic_words=['árabe','arabe','arabic','العربية','اللغة العربية','nahw','نحو','sarf','صرف','gramática árabe','gramatica arabe','clase de árabe','curso de árabe','lección de árabe','leccion de arabe']
    if any(w in t for w in arabic_words): return 100, 'Clases de árabe'
    if 'ghazali' in t or 'ghazali' in s or 'ghazāl' in t: return 30, 'Retiro Al-Ghazali'
    if 'bermejo' in s: return 20, 'Bermejo'
    return 0, source or 'Otros'
def fetch_metadata(r):
    vid=r['video_id']; url=r['url']
    if r['title'] and r['category'] is not None:
        if r['category']!='Clases de árabe' or ('upload_date' in r.keys() and r['upload_date']):
            return
    update(vid, metadata_attempts=int(r['metadata_attempts'] or 0)+1, last_metadata_error=None)
    p=run(common()+['--dump-single-json',url], timeout=180)
    if p.returncode!=0:
        update(vid,last_metadata_error=(p.stderr or p.stdout)[-2000:]); return
    try: meta=json.loads(p.stdout)
    except Exception as e: update(vid,last_metadata_error=f'JSON metadata inválido: {e}'); return
    title=meta.get('title') or vid; duration=meta.get('duration'); upload_date=meta.get('upload_date')
    pr,cat=classify(title, r['source'])
    update(vid,title=title,duration=duration,upload_date=upload_date,priority=pr,category=cat,last_metadata_error=None)
def metadata_phase():
    log('Fase 1: obteniendo títulos de los vídeos en cola')
    for r in rows("select * from videos where status='pending' order by source, video_id"):
        try:
            fetch_metadata(r)
        except Exception as exc:
            error=f'{type(exc).__name__}: {exc}'[-2000:]
            update(r['video_id'],last_metadata_error=error)
            log(f"AVISO metadatos {r['video_id']}: {error}")
    # recalcular prioridad de todos los que ya tienen título
    with con() as c:
        for r in c.execute('select video_id,title,source from videos').fetchall():
            pr,cat=classify(r['title'], r['source'])
            c.execute('update videos set priority=?, category=?, updated_at=CURRENT_TIMESTAMP where video_id=?',(pr,cat,r['video_id']))
    counts=rows("select coalesce(category,'Sin categoría') cat, count(*) n from videos group by cat order by max(priority) desc, cat")
    log('Lista obtenida/priorizada: '+', '.join(f"{r['cat']}={r['n']}" for r in counts))
def pending():
    # Solo se descargan vídeos encolados explícitamente desde el frontal.
    # Los fallidos quedan visibles como error hasta que el usuario los reencole.
    return rows("select * from videos where status='pending' order by priority desc, category collate nocase, attempts asc, coalesce(title, video_id) collate nocase, video_id")
def download_one(r):
    vid=r['video_id']; url=r['url']; f=existing_file(vid)
    if f:
        try: make_thumbnail(f,vid)
        except Exception as e: log(f'AVISO miniatura {vid}: {e}')
        update(vid,status='done',filename=f.name,filesize=f.stat().st_size,error=None); return
    if not r['title']: fetch_metadata(r); r=rows(f"select * from videos where video_id='{vid}'")[0]
    update(vid,status='running',attempts=int(r['attempts'])+1,error=None)
    title=r['title'] or vid; outtmpl=str(TMP/f'{sanitize(title)}--{vid}.%(ext)s')
    base=common()+['--ffmpeg-location',str(Path(ffmpeg()).parent),'--merge-output-format','mp4','-o',outtmpl,url]
    formats=['bv*[ext=mp4]+bestaudio/bv*+ba/best[ext=mp4]/best','18/best[ext=mp4]/best']
    last=''
    for fmt in formats:
        p=run(base[:-3]+['-f',fmt]+base[-3:], timeout=7200)
        if p.returncode==0: break
        last=(p.stderr or p.stdout); log(f'Fallo formato {fmt} para {vid}: {last[:250].replace(chr(10)," ")}')
    else:
        raise RuntimeError(last)
    cand=sorted(TMP.glob(f'*--{vid}.mp4'), key=lambda x:x.stat().st_mtime, reverse=True)
    if not cand: raise RuntimeError('yt-dlp terminó pero no encontré el MP4')
    final=VIDEOS/cand[0].name; os.replace(cand[0], final)
    try: make_thumbnail(final,vid)
    except Exception as e: log(f'AVISO miniatura {vid}: {e}')
    update(vid,status='done',title=title,filename=final.name,filesize=final.stat().st_size,error=None)
def main():
    init(); log(f'Downloader/priorizador iniciado: secuencial, pausa OK={SUCCESS_DELAY}s')
    initial_items=pending()
    if not initial_items:
        log('Cola completa; saliendo'); return
    initial_ids=[item['video_id'] for item in initial_items]
    try:
        ensure_runtime_tools()
    except Exception as exc:
        message=f'No se pudieron instalar o localizar yt-dlp, FFmpeg y Deno: {exc}'
        placeholders=','.join('?' for _ in initial_ids)
        with con() as c:
            c.execute(
                f"UPDATE videos SET status='error', error=?, updated_at=CURRENT_TIMESTAMP WHERE status='pending' AND video_id IN ({placeholders})",
                (message[-4000:],*initial_ids),
            )
        log(f'ERROR dependencias: {message}')
        raise RuntimeError(message) from exc
    metadata_phase()
    while True:
        items=pending()
        if not items: log('Cola completa; saliendo'); return
        r=items[0]
        try:
            log(f"Descargando {r['video_id']} prioridad={r['priority']} categoría={r['category']} título={r['title'] or ''}")
            download_one(r); log(f"OK {r['video_id']}; esperando {SUCCESS_DELAY}s antes del siguiente vídeo")
        except Exception as e:
            err=str(e)[-4000:]; log(f"ERROR {r['video_id']}: {err[:300].replace(chr(10),' ')}"); update(r['video_id'],status='error',error=err)
            continue
        time.sleep(SUCCESS_DELAY)
if __name__=='__main__': main()
