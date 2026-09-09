#!/usr/bin/env python3
from __future__ import annotations
import contextlib, json, os, re, shutil, sqlite3, subprocess, sys, time, datetime
from pathlib import Path
SOURCE_DIR=Path(__file__).resolve().parent; FROZEN=bool(getattr(sys,'frozen',False)); BINARY_DIR=Path(sys.executable).resolve().parent if FROZEN else SOURCE_DIR
DEFAULT_ROOT=(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'VideotecaYouTube') if FROZEN else SOURCE_DIR
ROOT=Path(os.environ.get('VIDEOTECA_HOME',DEFAULT_ROOT)).expanduser().resolve(); DB=ROOT/'data'/'portal.sqlite3'; VIDEOS=ROOT/'videos'; THUMBS=ROOT/'thumbs'; TMP=ROOT/'tmp'; LOG=ROOT/'logs'/'downloader.log'; SCHEMA="\nCREATE TABLE IF NOT EXISTS videos(\n  video_id TEXT PRIMARY KEY,\n  url TEXT NOT NULL,\n  source TEXT,\n  title TEXT,\n  filename TEXT,\n  status TEXT NOT NULL DEFAULT 'pending',\n  error TEXT,\n  attempts INTEGER NOT NULL DEFAULT 0,\n  duration REAL,\n  filesize INTEGER,\n  priority INTEGER NOT NULL DEFAULT 0,\n  category TEXT,\n  upload_date TEXT,\n  metadata_attempts INTEGER NOT NULL DEFAULT 0,\n  last_metadata_error TEXT,\n  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,\n  created_at TEXT DEFAULT CURRENT_TIMESTAMP\n);\nCREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);\nCREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source);\nCREATE INDEX IF NOT EXISTS idx_videos_priority ON videos(priority DESC, title COLLATE NOCASE);\n"
# La cola es siempre secuencial. Entre descargas correctas basta una pausa
# corta; los fallos conservan un backoff mayor para no insistir contra YouTube.
SUCCESS_DELAY=int(os.environ.get('YOUTUBE_SUCCESS_DELAY','30'))
ERROR_DELAY=int(os.environ.get('YOUTUBE_ERROR_DELAY','900'))
def log(msg):
    LOG.parent.mkdir(exist_ok=True); line=f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n"; print(line,end='',flush=True); LOG.open('a',encoding='utf-8').write(line)
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
def run(cmd, timeout=None): return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
def tool(name):
    suffix='.exe' if os.name=='nt' else ''
    candidates=[shutil.which(name),BINARY_DIR/f'{name}{suffix}',SOURCE_DIR/'.venv'/('Scripts' if os.name=='nt' else 'bin')/f'{name}{suffix}']
    for candidate in candidates:
        if candidate and Path(candidate).is_file(): return str(candidate)
    raise RuntimeError(f'No se encontró {name}. Consulta el README.')
def ytdlp(): return tool('yt-dlp')
def ffmpeg(): return tool('ffmpeg')
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
    # YouTube empezó a devolver 403 con el cliente por defecto en descargas
    # directas. El cliente Android sigue entregando URLs descargables en los
    # vídeos probados; mantenerlo como extractor por defecto evita que la cola
    # se quede bloqueada reintentando el mismo 403 cada hora.
    return [
        ytdlp(),
        '--js-runtimes','node:/usr/bin/node',
        '--remote-components','ejs:github',
        '--extractor-args','youtube:player_client=android',
        '--no-playlist',
    ]
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
        fetch_metadata(r)
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
    init(); log(f'Downloader/priorizador iniciado: secuencial, pausa OK={SUCCESS_DELAY}s, pausa error={ERROR_DELAY}s')
    metadata_phase()
    while True:
        items=pending()
        if not items: log('Cola completa; saliendo'); return
        r=items[0]
        try:
            log(f"Descargando {r['video_id']} prioridad={r['priority']} categoría={r['category']} título={r['title'] or ''}")
            download_one(r); log(f"OK {r['video_id']}; esperando {SUCCESS_DELAY}s antes del siguiente vídeo")
            delay=SUCCESS_DELAY
        except Exception as e:
            err=str(e)[-4000:]; log(f"ERROR {r['video_id']}: {err[:300].replace(chr(10),' ')}"); update(r['video_id'],status='error',error=err)
            log(f'Esperando {ERROR_DELAY}s antes del siguiente intento/vídeo')
            delay=ERROR_DELAY
        time.sleep(delay)
if __name__=='__main__': main()
