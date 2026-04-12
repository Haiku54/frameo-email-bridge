#!/usr/bin/env python3
"""Frameo Email Bridge - Main orchestrator.

Continuously polls Gmail for photo attachments and pushes them
to a Frameo digital photo frame via ADB over WiFi.
"""

import argparse
import logging
import shutil
import signal
import sys
import time
from datetime import datetime, date
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from email_monitor import EmailMonitor
from frame_pusher import FramePusher
from google_photos import GooglePhotosSync
from image_processor import ImageProcessingError, HeicNotSupportedError, process_image
from video_processor import (
    VIDEO_EXTENSIONS,
    FfmpegNotInstalledError,
    VideoProcessingError,
    process_video,
)

running = True
logger = logging.getLogger("frameo")


def main():
    parser = argparse.ArgumentParser(description="Frameo Email Bridge")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    config_path = Path(args.config)
    base_dir = Path(__file__).parent

    # Setup
    setup_logging(base_dir / "logs")
    setup_signal_handlers()
    dirs = create_directories(base_dir)

    logger.info("=" * 50)
    logger.info("Frameo Email Bridge starting")
    logger.info("Email: %s", mask_email(config["email"]["username"]))
    logger.info("Frame: %s:%s", config["frame"]["adb_ip"], config["frame"]["adb_port"])
    logger.info("Photo path: %s", config["frame"]["photo_path"])
    logger.info("Poll interval: %ds", config["email"]["poll_interval_seconds"])
    logger.info("=" * 50)

    # Initialize modules
    monitor = EmailMonitor(config, dirs["inbox"], dirs["data"] / "processed_emails.db")
    pusher = FramePusher(config, dirs["archive"], config_path=config_path)

    # Initialize Google Photos sync (optional)
    gp_sync = None
    gp_config = config.get("google_photos", {})
    if gp_config.get("enabled", False):
        gp_sync = GooglePhotosSync(config, dirs["data"])
        if gp_sync.authenticate():
            logger.info(
                "Google Photos sync enabled, album: %s",
                gp_config.get("album_name", "Frameo Photos"),
            )
        else:
            logger.warning("Google Photos auth failed — sync disabled")
            gp_sync = None

    # Health check
    if pusher.health_check():
        logger.info("Frame health check passed")
    else:
        logger.warning("Frame health check failed - will retry during polling")

    # Main loop
    poll_interval = config["email"]["poll_interval_seconds"]
    processing_config = config.get("processing", {})
    # Pass frame resolution into processing config for image_processor
    processing_config["resolution_width"] = config["frame"].get("resolution_width", 800)
    processing_config["resolution_height"] = config["frame"].get("resolution_height", 480)

    # Daily full sync tracking — initialize to today so we don't trigger
    # on every restart. The sync will fire tomorrow at full_sync_time.
    full_sync_time = gp_config.get("full_sync_time", "03:00") if gp_sync else None
    last_full_sync_date: date | None = date.today() if gp_sync else None

    try:
        # Crash recovery: if a previous run was killed between downloading
        # an attachment and processing it, the file is sitting in inbox/
        # but its UID is already in the SQLite DB (so IMAP won't re-deliver
        # it). Process those orphans now before entering the normal poll
        # loop. Must be inside the try/finally so monitor.close() still
        # runs if orphan recovery fails (e.g. SD card error).
        try:
            orphan_count = _process_inbox_orphans(pusher, dirs, processing_config)
            if orphan_count > 0:
                logger.info(
                    "Recovered %d orphaned file(s) from inbox/ on startup",
                    orphan_count,
                )
        except OSError as e:
            logger.error("Orphan recovery failed (continuing anyway): %s", e)

        while running:
            try:
                count = run_pipeline(
                    monitor, pusher, dirs, processing_config, gp_sync,
                )
                if count > 0:
                    logger.info("Cycle complete: %d photo(s) pushed", count)
                else:
                    logger.debug("Cycle complete: no new photos")
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Pipeline error: %s", e, exc_info=True)

            # Daily Google Photos full sync
            if gp_sync and full_sync_time:
                now = datetime.now()
                if (
                    now.strftime("%H:%M") >= full_sync_time
                    and last_full_sync_date != now.date()
                ):
                    try:
                        _run_full_sync(gp_sync, pusher, dirs)
                    except Exception as e:
                        logger.error("Full sync failed: %s", e, exc_info=True)
                    # Clean up old archives confirmed in Google Photos
                    retention = gp_config.get("archive_retention_days", 7)
                    if retention > 0:
                        try:
                            _cleanup_old_archives(gp_sync, dirs["archive"], retention)
                        except Exception as e:
                            logger.error("Archive cleanup failed: %s", e)
                    # Mark today as done even on failure — individual files
                    # that failed will be retried via _retry_gp_uploads each
                    # cycle, and a full re-sync will run tomorrow.
                    last_full_sync_date = now.date()

            # Sleep in 1-second increments for responsive shutdown
            for _ in range(poll_interval):
                if not running:
                    break
                time.sleep(1)
    finally:
        # Always close connections cleanly so pending transactions are
        # flushed and DB files are left in a consistent state.
        monitor.close()
        if gp_sync:
            gp_sync.close()

    logger.info("Frameo Email Bridge stopped")


def _process_one_file(
    input_path: Path,
    output_stem: str,
    is_video: bool,
    pusher: FramePusher,
    dirs: dict,
    processing_config: dict,
) -> tuple[bool, str | None]:
    """Process and push a single file from inbox/.

    Returns (pushed, output_name). output_name is the name of the file that
    was produced in processed/ (None if processing failed).
    """
    if is_video:
        output_name = f"{output_stem}.mp4"
        output_path = dirs["processed"] / output_name
        try:
            process_video(input_path, output_path, processing_config)
        except FfmpegNotInstalledError as e:
            logger.error("%s — moving to failed/", e)
            _move_to_failed(input_path, dirs["failed"])
            return False, None
        except VideoProcessingError as e:
            logger.error("Video processing failed for %s: %s", input_path.name, e)
            _move_to_failed(input_path, dirs["failed"])
            return False, None
    else:
        output_name = f"{output_stem}.jpg"
        output_path = dirs["processed"] / output_name
        try:
            process_image(input_path, output_path, processing_config)
        except HeicNotSupportedError as e:
            logger.warning("%s — moving to failed/", e)
            _move_to_failed(input_path, dirs["failed"])
            return False, None
        except ImageProcessingError as e:
            logger.error("Image processing failed for %s: %s", input_path.name, e)
            _move_to_failed(input_path, dirs["failed"])
            return False, None

    # Remove original from inbox
    try:
        input_path.unlink()
    except OSError:
        pass

    pushed = pusher.push_photo(output_path)
    return pushed, output_name


def _process_inbox_orphans(
    pusher: FramePusher,
    dirs: dict,
    processing_config: dict,
) -> int:
    """Process any files left in inbox/ from a previous crashed run.

    Without this, a file saved to inbox/ whose UID was already committed to
    the SQLite DB would be orphaned forever — IMAP won't re-deliver it and
    the normal polling loop only sees new UNSEEN emails.
    """
    processed = 0
    for orphan in sorted(dirs["inbox"].iterdir()):
        if not orphan.is_file():
            continue
        logger.warning("Recovering orphaned file from prior crash: %s", orphan.name)
        is_video = orphan.suffix.lower() in VIDEO_EXTENSIONS
        stem = orphan.stem
        pushed, _ = _process_one_file(
            orphan, stem, is_video, pusher, dirs, processing_config
        )
        if pushed:
            processed += 1
    return processed


def run_pipeline(
    monitor: EmailMonitor,
    pusher: FramePusher,
    dirs: dict,
    processing_config: dict,
    gp_sync: GooglePhotosSync | None = None,
) -> int:
    """Run one polling cycle: email -> download -> process -> push."""
    attachments = monitor.check_for_new_photos()
    pushed = 0
    current_outputs: set[str] = set()

    for att in attachments:
        stem = f"{att.uid}_{Path(att.original_filename).stem}"
        was_pushed, output_name = _process_one_file(
            att.file_path, stem, att.is_video, pusher, dirs, processing_config
        )
        if output_name:
            current_outputs.add(output_name)
        if was_pushed:
            pushed += 1
            # Immediate upload to Google Photos
            if gp_sync and output_name:
                _gp_upload_safe(gp_sync, dirs["archive"] / output_name)

    # Retry any previously failed pushes sitting in processed/
    for leftover in list(dirs["processed"].glob("*.jpg")) + list(dirs["processed"].glob("*.mp4")):
        if leftover.name in current_outputs:
            continue
        # Skip tempfile.mkstemp leftovers (should never happen after round 2 fix,
        # but defensive — they start with "tmp" and are short names)
        if leftover.name.startswith("tmp") and len(leftover.stem) < 12:
            continue
        logger.info("Retrying previously failed push: %s", leftover.name)
        if pusher.push_photo(leftover):
            pushed += 1
            if gp_sync:
                _gp_upload_safe(gp_sync, pusher.archive_dir / leftover.name)

    # Retry any Google Photos uploads that previously failed
    if gp_sync:
        _retry_gp_uploads(gp_sync, dirs["archive"])

    return pushed


def _gp_upload_safe(gp_sync: GooglePhotosSync, archive_path: Path) -> None:
    """Upload a file to Google Photos, logging but never raising on failure."""
    try:
        gp_sync.upload_file(archive_path)
    except Exception as e:
        logger.warning(
            "Google Photos upload failed for %s (will retry): %s",
            archive_path.name, e,
        )


def _retry_gp_uploads(gp_sync: GooglePhotosSync, archive_dir: Path) -> None:
    """Upload any archived files that are not yet in Google Photos."""
    uploaded = gp_sync.get_uploaded_files()
    for f in sorted(archive_dir.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".jpg", ".mp4"):
            continue
        if f.name in uploaded:
            continue
        if f.name.startswith("_configure_test"):
            continue
        # Skip files that were removed from the album (deleted from frame)
        if gp_sync.is_removed(f.name):
            continue
        if not gp_sync.upload_file(f):
            break  # Don't hammer the API if it's down


def _run_full_sync(
    gp_sync: GooglePhotosSync,
    pusher: FramePusher,
    dirs: dict,
) -> None:
    """Daily sync: reconcile frame contents with Google Photos album."""
    logger.info("Starting daily Google Photos full sync")

    # 1. List files on frame — scan both the push directory and the
    #    Frameo app's own media directory (photos sent via the phone app).
    FRAME_MEDIA_PATHS = [
        None,                           # pusher.photo_path (DCIM)
        "/sdcard/frameo_files/media/",   # Frameo app's media
    ]
    frame_files: dict[str, str | None] = {}  # {filename: remote_path}
    any_path_failed = False
    for rpath in FRAME_MEDIA_PATHS:
        files = pusher.list_remote_files(remote_path=rpath)
        if files is None:
            # ADB failure on this path — mark as incomplete
            any_path_failed = True
            continue
        for f in files:
            # Prefix with path to avoid collisions between directories
            key = f"{rpath or 'dcim'}:{f}"
            frame_files[key] = rpath

    if not frame_files and any_path_failed:
        logger.warning("Could not list frame files, skipping full sync")
        return

    # 2. Get what we've uploaded (from SQLite)
    uploaded = gp_sync.get_uploaded_files()

    # Only consider photo/video files (ls may include subdirs and other files)
    photo_extensions = {".jpg", ".jpeg", ".mp4"}
    # frame_files is {key: remote_path} where key = "path:filename"
    # Extract just the filename part for comparison with uploaded DB
    frame_by_name: dict[str, str | None] = {}  # {filename: remote_path}
    for key, rpath in frame_files.items():
        # key is "path:filename" — extract filename
        filename = key.split(":", 1)[1] if ":" in key else key
        if Path(filename).suffix.lower() in photo_extensions:
            # If collision, keep the first one seen (DCIM takes priority)
            if filename not in frame_by_name:
                frame_by_name[filename] = rpath

    frame_set = set(frame_by_name.keys())
    uploaded_set = set(uploaded.keys())

    # 3. Files on frame but NOT in album → upload
    missing_from_album = frame_set - uploaded_set
    if missing_from_album:
        logger.info(
            "Full sync: %d file(s) on frame not in album", len(missing_from_album),
        )
        for filename in sorted(missing_from_album):
            # Check archive first (common: pushed by us but GP upload failed)
            archive_path = dirs["archive"] / filename
            if archive_path.exists():
                try:
                    gp_sync.upload_file(archive_path)
                except Exception as e:
                    logger.warning("Full sync upload from archive failed for %s: %s", filename, e)
                continue

            # Not in archive → pull from frame (using correct remote path)
            local_path = dirs["processed"] / filename
            if pusher.pull_file(filename, local_path, remote_path=frame_by_name[filename]):
                try:
                    gp_sync.upload_file(local_path)
                except Exception as e:
                    logger.warning("Full sync GP upload failed for %s: %s", filename, e)
                    local_path.unlink(missing_ok=True)
                else:
                    try:
                        shutil.move(str(local_path), str(dirs["archive"] / filename))
                    except OSError as e:
                        logger.warning("Full sync move to archive failed for %s: %s", filename, e)
                        local_path.unlink(missing_ok=True)
            else:
                logger.warning("Full sync: could not pull %s from frame", filename)

    # 4. Files in album but NOT on frame → remove from album
    #    Skip if any path scan failed — we might have an incomplete view
    #    and would wrongly remove photos that are still on the frame.
    if any_path_failed:
        logger.warning("Full sync: skipping album removal (incomplete frame scan)")
        logger.info("Daily Google Photos full sync complete (partial)")
        return
    missing_from_frame = uploaded_set - frame_set
    if missing_from_frame:
        logger.info(
            "Full sync: %d file(s) in album but not on frame, removing",
            len(missing_from_frame),
        )
        filenames_to_remove = sorted(missing_from_frame)
        media_ids = [uploaded[f] for f in filenames_to_remove]
        # batchRemoveMediaItems supports max 50 per call
        for i in range(0, len(media_ids), 50):
            batch_ids = media_ids[i : i + 50]
            batch_names = filenames_to_remove[i : i + 50]
            try:
                if gp_sync.remove_from_album(batch_ids):
                    for fname in batch_names:
                        gp_sync.mark_removed(fname)
            except Exception as e:
                logger.warning("Full sync remove-from-album failed: %s", e)

    logger.info("Daily Google Photos full sync complete")


def _cleanup_old_archives(
    gp_sync: GooglePhotosSync,
    archive_dir: Path,
    retention_days: int,
) -> None:
    """Delete archived files older than retention_days that are confirmed in GP."""
    uploaded = gp_sync.get_uploaded_files()
    if not uploaded:
        return

    now = time.time()
    cutoff = now - (retention_days * 86400)
    deleted = 0

    for f in sorted(archive_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name not in uploaded:
            continue
        if f.stat().st_mtime > cutoff:
            continue
        f.unlink()
        deleted += 1

    if deleted:
        logger.info("Archive cleanup: deleted %d file(s) older than %d days", deleted, retention_days)


def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {path}", file=sys.stderr)
        print("Run 'bash setup.sh' first, then edit config.yaml", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f)

    # Validate required fields
    required = [
        ("email", "imap_server"),
        ("email", "username"),
        ("email", "password"),
        ("frame", "adb_ip"),
        ("frame", "photo_path"),
    ]
    for section, key in required:
        val = config.get(section, {}).get(key, "")
        if not val:
            print(
                f"Error: config.yaml is missing required field: {section}.{key}",
                file=sys.stderr,
            )
            sys.exit(1)

    return config


def setup_logging(logs_dir: Path):
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (rotating)
    file_handler = RotatingFileHandler(
        logs_dir / "frameo_bridge.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def setup_signal_handlers():
    def handler(signum, _frame):
        global running
        logger.info("Received signal %s, shutting down...", signal.Signals(signum).name)
        running = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def create_directories(base_dir: Path) -> dict:
    dirs = {
        "inbox": base_dir / "inbox",
        "processed": base_dir / "processed",
        "archive": base_dir / "archive",
        "failed": base_dir / "archive" / "failed",
        "logs": base_dir / "logs",
        "data": base_dir / "data",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def mask_email(email_addr: str) -> str:
    if "@" not in email_addr:
        return "***"
    user, domain = email_addr.split("@", 1)
    if len(user) <= 2:
        return f"**@{domain}"
    return f"{user[0]}{'*' * (len(user) - 2)}{user[-1]}@{domain}"


def _move_to_failed(file_path: Path, failed_dir: Path):
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        file_path.rename(failed_dir / file_path.name)
    except OSError as e:
        logger.error("Failed to move %s to failed dir: %s", file_path.name, e)


if __name__ == "__main__":
    main()
