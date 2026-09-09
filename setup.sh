#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PORT="${PORT:-8802}"
HOST="${HOST:-0.0.0.0}"
PYTHON="${PYTHON:-python3}"
PORTAL_SERVICE="videoteca-youtube.service"
DOWNLOADER_SERVICE="videoteca-youtube-downloader.service"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || fail "No se encontró Python 3.10 o posterior."
command -v ffmpeg >/dev/null || fail "Falta ffmpeg. En Debian/Ubuntu: sudo apt install ffmpeg"

"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit('Se necesita Python 3.10 o posterior.')
PY

mkdir -p "$ROOT"/{data,videos,thumbs,tmp,logs}
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    if ! "$PYTHON" -m venv "$ROOT/.venv"; then
        command -v uv >/dev/null || fail "No se pudo crear el entorno virtual. Instala python3-venv o uv."
        uv venv --python "$PYTHON" "$ROOT/.venv"
    fi
fi

if [[ -x "$ROOT/.venv/bin/pip" ]]; then
    "$ROOT/.venv/bin/pip" install --upgrade -r "$ROOT/requirements.txt"
else
    command -v uv >/dev/null || fail "El entorno no incluye pip y no se encontró uv."
    uv pip install --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"
fi

VIDEOTECA_HOME="$ROOT" "$ROOT/.venv/bin/python" - <<'PY'
import app
app.init()
print('Base de datos inicializada.')
PY

if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$DOWNLOADER_SERVICE" <<EOF
[Unit]
Description=Descargador secuencial de Videoteca YouTube
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=VIDEOTECA_HOME=$ROOT
Environment=PATH=$ROOT/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=$ROOT/.venv/bin/python $ROOT/downloader.py
Restart=on-failure
RestartSec=300
EOF

    cat > "$UNIT_DIR/$PORTAL_SERVICE" <<EOF
[Unit]
Description=Videoteca local de YouTube
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=VIDEOTECA_HOME=$ROOT
Environment=VIDEOTECA_DOWNLOADER_SERVICE=$DOWNLOADER_SERVICE
Environment=PATH=$ROOT/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=$ROOT/.venv/bin/python $ROOT/app.py --host $HOST --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable --now "$PORTAL_SERVICE"
    printf '\nServicio instalado: %s\n' "$PORTAL_SERVICE"
    printf 'Estado: systemctl --user status %s\n' "$PORTAL_SERVICE"
    printf 'Logs:   journalctl --user -u %s -f\n' "$PORTAL_SERVICE"
else
    printf '\nsystemd de usuario no está disponible. Arranque manual:\n'
    printf '  %q %q --host %q --port %q\n' "$ROOT/.venv/bin/python" "$ROOT/app.py" "$HOST" "$PORT"
fi

printf '\nVideoteca lista en http://127.0.0.1:%s/\n' "$PORT"
if [[ "$HOST" == "0.0.0.0" ]]; then
    printf 'Desde otro equipo, usa http://IP-DE-ESTE-EQUIPO:%s/\n' "$PORT"
fi
