# Videoteca YouTube

Videoteca web local, ligera y sin dependencias de frontend, para organizar enlaces de YouTube por categorías y descargar únicamente los vídeos que el usuario elija.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Licencia](https://img.shields.io/badge/licencia-MIT-green)
![Windows](https://img.shields.io/badge/Windows-EXE-0078D4?logo=windows)

## Características

- Categorías desplegables creadas desde la propia interfaz.
- Alta de vídeos individuales, identificadores de YouTube o playlists completas.
- Expansión automática de playlists mediante `yt-dlp`.
- Estados separados: sin descargar, pendiente, descargando, disponible y fallido.
- Descarga individual o de todos los vídeos pendientes de una categoría.
- Cola estrictamente secuencial: nunca inicia varias descargas simultáneas.
- Reproducción local con soporte de peticiones HTTP por rangos.
- Miniaturas locales generadas con FFmpeg.
- Enlace directo al vídeo original de YouTube.
- Búsqueda que ignora tildes y espacios repetidos.
- Orden numérico natural (`1, 2, 3… 10`, no `1, 10, 2`).
- Interfaz adaptable a móvil.
- SQLite: no requiere servidor de base de datos.
- Sin telemetría, cuentas ni servicios externos aparte de YouTube.

> El repositorio se distribuye vacío: no contiene vídeos, miniaturas ni bases de datos personales.

## Inicio rápido en Linux

### Requisitos

- Python 3.10 o posterior.
- FFmpeg.
- `systemd --user` es opcional, pero permite dejar la aplicación como servicio persistente.

En Debian o Ubuntu:

```bash
sudo apt install python3 python3-venv ffmpeg
```

### Instalación automática

```bash
git clone https://github.com/lobuhi/videoteca-youtube.git
cd videoteca-youtube
chmod +x setup.sh
./setup.sh
```

El instalador:

1. crea `.venv`;
2. instala `yt-dlp`;
3. crea una base SQLite vacía;
4. instala e inicia `videoteca-youtube.service` cuando `systemd --user` está disponible;
5. utiliza el puerto **8802** de forma predeterminada.

Abre:

```text
http://127.0.0.1:8802/
```

Desde otro dispositivo de la misma red:

```text
http://IP-DEL-SERVIDOR:8802/
```

### Elegir otro puerto

```bash
PORT=9000 ./setup.sh
```

También puede limitarse al equipo local:

```bash
HOST=127.0.0.1 PORT=9000 ./setup.sh
```

## Ejecución manual

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --host 127.0.0.1 --port 8802 --open-browser
```

Sin `systemd`, el descargador se ejecuta en un hilo de fondo dentro del proceso web. Con la instalación automática de Linux, se usa un servicio separado para que las descargas sobrevivan a un reinicio del portal.

## Uso

1. Abre **Gestionar videoteca**.
2. Crea una categoría.
3. Selecciónala en el formulario de incorporación.
4. Pega uno o varios elementos:
   - `https://www.youtube.com/watch?v=...`
   - `https://youtu.be/...`
   - `https://www.youtube.com/shorts/...`
   - una URL de playlist;
   - un identificador de vídeo.
5. Pulsa **Añadir a la videoteca**.
6. Descarga un vídeo concreto o pulsa **Descargar toda la categoría**.

Añadir un vídeo al catálogo **no lo descarga automáticamente**. Solo se descargan filas encoladas explícitamente.

## Datos y copias de seguridad

En una instalación desde código, los datos se guardan dentro del proyecto:

| Ruta | Contenido |
|---|---|
| `data/portal.sqlite3` | Catálogo y estados |
| `videos/` | MP4 descargados |
| `thumbs/` | Miniaturas JPEG |
| `tmp/` | Descargas temporales |
| `logs/downloader.log` | Registro del descargador |

Para hacer una copia completa, detén temporalmente el servicio y copia esas rutas:

```bash
systemctl --user stop videoteca-youtube.service videoteca-youtube-downloader.service
cp -a data videos thumbs /ruta/de/copia/
systemctl --user start videoteca-youtube.service
```

Estos contenidos están excluidos de Git mediante `.gitignore`.

## Servicios Linux

```bash
# Estado
systemctl --user status videoteca-youtube.service
systemctl --user status videoteca-youtube-downloader.service

# Reiniciar portal
systemctl --user restart videoteca-youtube.service

# Registro del portal
journalctl --user -u videoteca-youtube.service -f

# Registro de descargas
journalctl --user -u videoteca-youtube-downloader.service -f
```

El servicio del descargador aparece normalmente como `inactive` cuando la cola está vacía. Se inicia al pulsar un botón de descarga y termina al completar la cola.

## Windows y versión `.exe`

El proyecto incluye el flujo de GitHub Actions `.github/workflows/build-windows.yml`.

### Generar un ejecutable desde GitHub

1. Entra en la pestaña **Actions** del repositorio.
2. Selecciona **Compilar para Windows**.
3. Pulsa **Run workflow**.
4. Descarga el artefacto `VideotecaYouTube-Windows`.

El paquete contiene:

- `VideotecaYouTube.exe`;
- `yt-dlp.exe`;
- `ffmpeg.exe`.

Los tres archivos deben permanecer juntos para poder descargar vídeos. Al abrir `VideotecaYouTube.exe`, la aplicación usa el puerto 8802 y abre el navegador automáticamente.

Los datos de Windows se guardan fuera del ejecutable, en:

```text
%LOCALAPPDATA%\VideotecaYouTube\
```

### Publicar una release automáticamente

Crea y sube una etiqueta con formato `v*`:

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions compilará Windows y publicará automáticamente una release con:

- `VideotecaYouTube.exe`;
- `VideotecaYouTube-Windows.zip`, que incluye las herramientas necesarias.

## Variables de entorno

| Variable | Uso | Valor predeterminado |
|---|---|---|
| `VIDEOTECA_HOME` | Directorio de datos | Proyecto; `%LOCALAPPDATA%\VideotecaYouTube` en EXE |
| `VIDEOTECA_HOST` | Dirección de escucha | `127.0.0.1` |
| `VIDEOTECA_PORT` | Puerto | `8802` |
| `VIDEOTECA_DOWNLOADER_SERVICE` | Servicio Linux del descargador | Vacío; hilo integrado |
| `YOUTUBE_SUCCESS_DELAY` | Pausa tras una descarga correcta | `30` segundos |
| `YOUTUBE_ERROR_DELAY` | Pausa tras un error | `900` segundos |

## API local

| Método | Ruta | Función |
|---|---|---|
| `GET` | `/api/videos` | Lista del catálogo |
| `POST` | `/api/categories` | Crear categoría |
| `POST` | `/api/videos/add` | Añadir vídeos o playlist |
| `POST` | `/api/download/video` | Encolar un vídeo |
| `POST` | `/api/download/category` | Encolar una categoría |

La API no incorpora autenticación. Está pensada para uso local o en una LAN de confianza. No expongas el puerto directamente a Internet.

## Desarrollo y pruebas

```bash
python3 -W error -m unittest -v
python3 -m py_compile app.py downloader.py
```

La aplicación utiliza únicamente la biblioteca estándar de Python para el servidor web. `yt-dlp` y FFmpeg se invocan como herramientas externas.

## Consideraciones legales

Descarga únicamente contenido cuando tengas autorización para hacerlo. El usuario es responsable de cumplir las condiciones del servicio de YouTube, las licencias del contenido y la legislación aplicable.

## Licencia

Publicado bajo la licencia [MIT](LICENSE).
