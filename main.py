#!/usr/bin/env python3
"""Frameo Email Bridge - Main orchestrator.

Continuously polls Gmail for photo attachments and pushes them
to a Frameo digital photo frame via ADB over WiFi.
"""

import argparse
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from email_monitor import EmailMonitor
from frame_pusher import FramePusher
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
    pusher = FramePusher(config, dirs["archive"])

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
                count = run_pipeline(monitor, pusher, dirs, processing_config)
                if count > 0:
                    logger.info("Cycle complete: %d photo(s) pushed", count)
                else:
                    logger.debug("Cycle complete: no new photos")
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Pipeline error: %s", e, exc_info=True)

            # Sleep in 1-second increments for responsive shutdown
            for _ in range(poll_interval):
                if not running:
                    break
                time.sleep(1)
    finally:
        # Always close the SQLite connection cleanly so pending transactions
        # are flushed and the DB file is left in a consistent state, even
        # if the polling loop crashed.
        monitor.close()

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

    return pushed


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
