# YT Vault

**English** · [Español](README.es.md)

A lightweight, local-first web application for organizing YouTube links into categories and downloading only the videos you choose.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![Windows](https://img.shields.io/badge/Windows-EXE-0078D4?logo=windows)

## Features

- Create collapsible categories directly from the interface.
- Assign one video to multiple categories and display it in each of them.
- Reorganize categories from every video card using multi-select.
- Remove a library record while either keeping or explicitly deleting its local files.
- Add individual videos, YouTube IDs, or complete playlists.
- Expand playlists automatically with `yt-dlp`.
- Track separate states: YouTube only, pending, downloading, available, and failed.
- Download one video or every pending video in a category.
- Strictly sequential queue: downloads never run concurrently.
- Play local files with HTTP range-request support.
- Generate local thumbnails with FFmpeg.
- Keep a direct link to the original YouTube video.
- Accent-insensitive search and natural numeric ordering.
- Modern, responsive dark interface for desktop and mobile.
- Switch between English and Spanish; the browser remembers the preference.
- Native Windows system tray icon for opening or shutting down YT Vault.
- SQLite storage: no database server required.
- No telemetry, accounts, or external services apart from YouTube.

> The repository is distributed empty. It contains no personal videos, thumbnails, or databases.

## Quick start on Linux

### Requirements

- Python 3.10 or newer.
- FFmpeg.
- `systemd --user` is optional, but allows the application to run as a persistent service.

On Debian or Ubuntu:

```bash
sudo apt install python3 python3-venv ffmpeg
```

### Automatic installation

```bash
git clone https://github.com/lobuhi/yt-vault.git
cd yt-vault
chmod +x setup.sh
./setup.sh
```

The installer:

1. creates `.venv`;
2. installs `yt-dlp`;
3. creates an empty SQLite database;
4. installs and starts `yt-vault.service` when `systemd --user` is available;
5. uses port **8802** by default.

Open:

```text
http://127.0.0.1:8802/
```

From another device on the same network:

```text
http://SERVER-IP:8802/
```

### Choose another port

```bash
PORT=9000 ./setup.sh
```

To restrict access to the local machine:

```bash
HOST=127.0.0.1 PORT=9000 ./setup.sh
```

## Manual execution

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --host 127.0.0.1 --port 8802 --open-browser
```

Without `systemd`, the downloader runs in a background thread inside the web process. The automatic Linux installation uses a separate service so downloads can survive a portal restart.

## Usage

1. Open **Manage library**.
2. Create a category.
3. Select it in the add-videos form.
4. Paste one or more of the following:
   - `https://www.youtube.com/watch?v=...`
   - `https://youtu.be/...`
   - `https://www.youtube.com/shorts/...`
   - a playlist URL;
   - a video ID.
5. Select **Add to library**.
6. Download an individual video or select **Download entire category**.

Each card includes **Manage / delete**. The dialog lets you assign several categories, move the video, or remove its record. **Also delete the video file and thumbnail from disk** is disabled by default and requires an additional confirmation. Pending or active downloads cannot be deleted. If the operating system prevents file removal, cleanup is recorded and retried safely.

Adding a video to the catalog **does not download it automatically**. Only explicitly queued rows are downloaded.

## Data and backups

Source installations store their data inside the project:

| Path | Contents |
|---|---|
| `data/portal.sqlite3` | Catalog and status data |
| `videos/` | Downloaded MP4 files |
| `thumbs/` | JPEG thumbnails |
| `tmp/` | Temporary downloads |
| `logs/downloader.log` | Downloader log |

For a complete backup, temporarily stop the services and copy those paths:

```bash
systemctl --user stop yt-vault.service yt-vault-downloader.service
cp -a data videos thumbs /path/to/backup/
systemctl --user start yt-vault.service
```

These paths are excluded from Git through `.gitignore`.

## Linux services

```bash
# Status
systemctl --user status yt-vault.service
systemctl --user status yt-vault-downloader.service

# Restart the portal
systemctl --user restart yt-vault.service

# Portal log
journalctl --user -u yt-vault.service -f

# Downloader log
journalctl --user -u yt-vault-downloader.service -f
```

The downloader service normally appears as `inactive` while the queue is empty. It starts when a download button is selected and exits after finishing the queue.

## Windows executable

The repository includes `.github/workflows/build-windows.yml`.

### Download or build

Download the latest `YTVault.exe` or `YTVault-Windows.zip` from [GitHub Releases](https://github.com/lobuhi/yt-vault/releases). To create a fresh build manually:

1. Open the repository's **Actions** tab.
2. Select **Build for Windows** / **Compilar para Windows**.
3. Select **Run workflow**.
4. Download the `YTVault-Windows` artifact.

The portable package contains:

- `YTVault.exe`;
- `yt-dlp.exe`;
- `ffmpeg.exe` and `ffprobe.exe`;
- `deno.exe`, the JavaScript runtime recommended by yt-dlp for full YouTube support.

The portable files can remain together and work without a separate installation. The standalone `YTVault.exe` also works by itself: on the first playlist import or queued download, it automatically downloads missing tools into `%LOCALAPPDATA%\YTVault\tools`. Every download uses HTTPS, is checked against the publisher's SHA-256 checksum, and is moved into place only after verification. If installation fails, queued items change to **failed** instead of remaining stuck as pending; details are written to `%LOCALAPPDATA%\YTVault\logs\downloader.log`.

Starting `YTVault.exe` uses port 8802 and opens the browser automatically.

### System tray controls

While YT Vault is running, its green icon remains in the **Windows system tray**, next to the clock (it may be inside the hidden-icons menu).

- Double-click the icon to open YT Vault.
- Right-click and choose **Open YT Vault** to open the interface.
- Right-click and choose **Exit YT Vault** to stop the local server and fully close the application.

Windows data is stored outside the executable in:

```text
%LOCALAPPDATA%\YTVault\
```

### Publish a release automatically

Create and push a `v*` tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions builds Windows and publishes a release containing `YTVault.exe` and `YTVault-Windows.zip`.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `VIDEOTECA_HOME` | Data directory | Project directory; `%LOCALAPPDATA%\YTVault` in the EXE |
| `VIDEOTECA_HOST` | Listen address | `127.0.0.1` |
| `VIDEOTECA_PORT` | Port | `8802` |
| `VIDEOTECA_ALLOWED_ORIGINS` | Comma-separated origins allowed for `POST` operations | `http://127.0.0.1:<port>`, `http://localhost:<port>`, and `http://[::1]:<port>` |
| `VIDEOTECA_ORIGIN` | Backward-compatible single-origin alias | Empty |
| `VIDEOTECA_DOWNLOADER_SERVICE` | Linux downloader service | Empty; integrated thread |
| `YOUTUBE_SUCCESS_DELAY` | Delay after a successful download | `30` seconds |

## Local API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/videos` | List the catalog |
| `POST` | `/api/categories` | Create a category |
| `POST` | `/api/videos/add` | Add videos or a playlist |
| `POST` | `/api/videos/categories` | Replace a video's category selection |
| `POST` | `/api/videos/delete` | Remove a record and optionally its local files |
| `POST` | `/api/download/video` | Queue one video |
| `POST` | `/api/download/category` | Queue a category |

The API has no authentication and is intended for local use or a trusted LAN. Do not expose the port directly to the Internet. `POST` requests require `Content-Type: application/json` and an `Origin` or `Referer` listed in `VIDEOTECA_ALLOWED_ORIGINS`. Configure this variable when using a LAN IP, domain name, or HTTPS proxy.

## Development and tests

```bash
python3 -W error -m unittest -v
python3 -m py_compile app.py downloader.py
```

The web server uses only Python's standard library. `yt-dlp` and FFmpeg are invoked as external tools.

## Legal notice

Only download content when you are authorized to do so. Users are responsible for complying with YouTube's terms of service, content licenses, and applicable law.

## License

Released under the [MIT License](LICENSE).
