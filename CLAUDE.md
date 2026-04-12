# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Frameo Email Bridge is a Python service that monitors a Gmail inbox via IMAP and automatically pushes photo/video attachments to a Frameo digital photo frame over WiFi using ADB. Optionally syncs all frame photos to a shared Google Photos album for family viewing. The pipeline: Email → Gmail IMAP poll → filter by subject/sender → download → resize/convert/optimize → `adb push` to frame → appears on frame (→ upload to Google Photos album).

## Commands

```bash
# Setup (interactive - creates venv, installs deps, runs config wizard)
bash setup.sh

# Run the service
source .venv/bin/activate
python main.py                    # uses config.yaml by default
python main.py --config other.yaml

# Reconfigure (re-run setup wizard)
.venv/bin/python configure.py

# Manual network scan for frames
.venv/bin/python discover_frame.py

# Google Photos one-time auth (opens browser)
.venv/bin/python google_photos.py --auth
.venv/bin/python google_photos.py --auth --headless  # no browser (Raspberry Pi)
```

There are no tests, linting, or build steps. The project uses a `.venv/` virtualenv with dependencies: Pillow, pillow-heif, PyYAML, google-auth-httplib2, google-auth-oauthlib.

## Architecture

Nine Python modules:

```
main.py (orchestrator, polling loop, GP sync scheduling)
  ├── email_monitor.py    IMAP client, downloads attachments to inbox/
  ├── image_processor.py  PIL: resize, rotate, sharpen, optimize → processed/
  ├── video_processor.py  ffmpeg subprocess: trim, scale, re-encode → processed/
  ├── frame_pusher.py     ADB subprocess: push to frame, archive on success
  │     └── discover_frame.py  Network subnet scan for Frameo frames
  └── google_photos.py    OAuth2 auth, two-step upload, album management, SQLite tracking

configure.py   Interactive config wizard (standalone entry point)
adb_setup.py   USB→WiFi ADB one-time setup helpers (used by configure.py)
```

### Polling Cycle (main.py → run_pipeline)

1. `EmailMonitor.check_for_new_photos()` — IMAP SINCE search, filter against SQLite DB of processed UIDs, download to `inbox/`
2. For each attachment: determine image vs video, process via `image_processor` or `video_processor`, output to `processed/`
3. `FramePusher.push_photo()` — ADB push to frame, trigger media scan, move to `archive/`
4. If Google Photos enabled: immediately upload archived file to album via `GooglePhotosSync.upload_file()`
5. Retry any files left in `processed/` from prior failed pushes
6. Retry any Google Photos uploads that previously failed (scan `archive/` against SQLite)
7. If daily full sync time reached: `_run_full_sync()` — compare frame contents (DCIM + frameo_files/media/) with album, upload missing, remove deleted, clean up old archives
8. Sleep `poll_interval_seconds` (in 1-second increments for responsive shutdown)

### Key Design Decisions

- **IMAP strategy**: Does NOT rely on `\Seen` flag. Fetches with `BODY.PEEK[]` and tracks processed UIDs in SQLite. This avoids issues when other mail clients mark emails as read.
- **Baseline initialization**: First run marks all existing emails as processed to avoid flooding the frame with old photos.
- **Self-healing IP**: After 3 consecutive ADB push failures, automatically runs `discover_frame.py` to find the frame at its new IP and updates `config.yaml` in place. Throttled to once per minute.
- **Crash recovery**: On startup, processes orphan files left in `inbox/` from prior crashes (their UIDs are already in SQLite so IMAP won't re-deliver them).
- **Atomic writes**: Video processing writes to a temp file (mkstemp), only moves to output on success. Files are archived only after a successful `adb push`.
- **Graceful shutdown**: SIGTERM/SIGINT set `running=False`; main loop exits cleanly; SQLite connections closed in `finally` block.
- **Google Photos isolation**: GP upload failures never block the email→frame pipeline. All GP calls wrapped in try/except. Failed uploads retried next cycle.
- **Google Photos two-step upload**: Raw bytes POST to `/v1/uploads` returns upload token, then `mediaItems:batchCreate` with token + album ID. Uses `AuthorizedSession` from google-auth.
- **Full sync dual-path**: Scans both `/sdcard/DCIM/` (service-pushed) and `/sdcard/frameo_files/media/` (Frameo app). Skips album removal if any path scan fails (prevents data loss on partial ADB failure).
- **Archive cleanup**: After confirmed GP upload, files older than `archive_retention_days` are deleted. Only files tracked in SQLite are eligible.
- **SQLite WAL mode**: Both `email_monitor` and `google_photos` open separate connections to the same DB file; WAL mode prevents contention.

### Runtime Directories

- `inbox/` — downloaded attachments (transient, retry queue for orphan recovery)
- `processed/` — processed files awaiting push (persists across restarts)
- `archive/` — successfully pushed files
- `archive/failed/` — files that failed processing (bad format, etc.)
- `logs/` — `frameo_bridge.log` (RotatingFileHandler, 5MB, 3 backups)
- `data/` — `processed_emails.db` (SQLite: email UIDs + GP upload tracking + GP removed tracking), `token.json` (Google OAuth2 refresh token)

## Configuration

All settings in `config.yaml` (not committed — contains Gmail credentials). See `config.yaml.example` for the template with all fields and comments.

Four sections: `email` (IMAP server, credentials, poll interval, subject/sender filters), `frame` (ADB IP/port, photo path, resolution, push timeout), `processing` (image quality/size limits, video duration/size limits, HEIC conversion toggle), `google_photos` (enabled, credentials file, album name, daily sync time, archive retention days).

## Conventions

- Per-module logger: `logger = logging.getLogger(__name__)` (or `"frameo"` in main)
- External tools called via `subprocess.run()`: `adb` for frame communication, `ffmpeg` for video processing
- Email addresses masked in logs via `mask_email()`
- Config loaded once at startup; only `frame_pusher.py` writes back to `config.yaml` (on IP rediscovery)
- SQLite uses WAL journal mode (shared DB between email_monitor and google_photos)
- System deps: Python 3.10+, `adb`, `ffmpeg` (video only)
