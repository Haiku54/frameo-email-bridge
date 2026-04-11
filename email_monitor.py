"""Email monitoring module for Frameo email bridge.

Connects to Gmail via IMAP, polls for new emails with image attachments,
downloads them, and tracks processed UIDs in SQLite to avoid reprocessing.
"""

import dataclasses
import email
import email.policy
import imaplib
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/heic", "image/heif",
                       "image/bmp", "image/gif", "image/tiff", "image/webp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".bmp",
                    ".gif", ".tiff", ".tif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
SAFE_FILENAME_RE = re.compile(r"[^\w\-.]")


@dataclasses.dataclass
class DownloadedAttachment:
    file_path: Path
    original_filename: str
    sender: str
    uid: str
    download_time: datetime
    is_video: bool = False


class EmailMonitor:
    def __init__(self, config: dict, inbox_dir: Path, db_path: Path):
        self.imap_server = config["email"]["imap_server"]
        self.imap_port = config["email"].get("imap_port", 993)
        self.username = config["email"]["username"]
        self.password = config["email"]["password"]
        self.allowed_senders = [
            s.lower().strip()
            for s in config["email"].get("allowed_senders", [])
            if s
        ]
        self.subject_filter = (config["email"].get("subject_filter") or "").strip().lower()
        self.accept_videos = config.get("processing", {}).get("accept_videos", True)
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.db = self._init_db(db_path)
        self._baseline_initialized = self._check_baseline()

    def _check_baseline(self) -> bool:
        """Has the DB been initialized with the baseline of existing UIDs?

        We set a meta row the first time we sync the full inbox history.
        Until then, the first poll will mark everything currently in the
        inbox as already-processed so we don't flood the frame with the
        user's entire email history on first run.
        """
        row = self.db.execute(
            "SELECT value FROM meta WHERE key = 'baseline_synced'"
        ).fetchone()
        return row is not None and row[0] == "1"

    def _set_baseline_synced(self) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('baseline_synced', '1')"
        )
        self.db.commit()
        self._baseline_initialized = True

    def _init_db(self, db_path: Path) -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                uid TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                attachment_count INTEGER,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        return conn

    def close(self) -> None:
        """Close the SQLite connection. Call on shutdown."""
        try:
            self.db.close()
        except Exception as e:
            logger.debug("Error closing SQLite connection: %s", e)

    def check_for_new_photos(self) -> list[DownloadedAttachment]:
        """Poll Gmail for new emails with image attachments.

        Strategy: do NOT rely on the IMAP \\Seen flag. Other Gmail clients
        (phone app, another browser tab, filter rules, notification preview)
        can mark emails as read before we get to them. Instead, ask Gmail for
        all recent emails (last N days) and filter out UIDs we have already
        processed based on our own SQLite database. This makes the service
        independent of whatever else is touching the inbox.

        Returns a list of downloaded attachments saved to inbox_dir.
        """
        conn = None
        attachments = []
        try:
            conn = self._connect()

            # First-run baseline: mark every existing inbox email as already
            # processed so we don't dump the user's entire history onto the
            # frame. Only new emails from this moment forward will be sent.
            if not self._baseline_initialized:
                self._initialize_baseline(conn)

            uids = self._search_recent(conn)
            # Filter out UIDs we already handled. This is the single source
            # of truth for "already processed".
            new_uids = [u for u in uids if not self._is_uid_processed(u)]
            if not new_uids:
                logger.debug("No new unprocessed emails (checked %d recent)", len(uids))
                return []

            logger.info(
                "Found %d new email(s) to process (out of %d recent)",
                len(new_uids), len(uids),
            )

            for uid in new_uids:
                try:
                    msg_attachments = self._process_email(conn, uid)
                    attachments.extend(msg_attachments)
                except (ConnectionResetError, TimeoutError, OSError, imaplib.IMAP4.error) as e:
                    # Transient network/protocol errors: leave UID unmarked so
                    # the next polling cycle will retry.
                    logger.warning("Transient error on UID %s, will retry next cycle: %s", uid, e)
                    raise
                except Exception as e:
                    # Structural/parse errors (malformed email etc.) — mark as
                    # processed so we don't infinite-loop on a broken email.
                    logger.error("Permanent error on UID %s, skipping: %s", uid, e)
                    self._mark_uid_processed(uid, "", "", 0)

        except imaplib.IMAP4.error as e:
            logger.error("IMAP error: %s", e)
        except (ConnectionResetError, TimeoutError, OSError) as e:
            logger.warning("Network error connecting to email: %s", e)
        finally:
            if conn:
                self._disconnect(conn)

        return attachments

    def _connect(self) -> imaplib.IMAP4_SSL:
        logger.debug("Connecting to %s:%d", self.imap_server, self.imap_port)
        conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port, timeout=30)
        conn.login(self.username, self.password)
        conn.select("INBOX")
        return conn

    def _disconnect(self, conn: imaplib.IMAP4_SSL) -> None:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass

    def _initialize_baseline(self, conn: imaplib.IMAP4_SSL) -> None:
        """On first run, mark every current inbox email as already processed.

        Without this, a fresh install would immediately push every photo in
        the user's entire email history to the frame.
        """
        logger.info("First run: marking existing inbox as baseline (will not be processed)")
        status, data = conn.uid("search", None, "ALL")
        if status != "OK":
            logger.warning("Baseline SEARCH ALL failed, baseline skipped")
            return
        existing = data[0].decode().split() if data[0] else []
        for uid in existing:
            self._mark_uid_processed(uid, "", "", 0)
        self._set_baseline_synced()
        logger.info(
            "Baseline marked %d existing email(s) as processed", len(existing)
        )

    def _search_recent(self, conn: imaplib.IMAP4_SSL, days: int = 2) -> list[str]:
        """Return UIDs of all emails received in the last `days` days.

        Uses IMAP SEARCH SINCE which is supported by all servers. The service
        then filters these UIDs against its own SQLite database of already-
        processed emails, so it does NOT depend on the \\Seen flag.
        """
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = conn.uid("search", None, "SINCE", since)
        if status != "OK" or not data[0]:
            return []
        return data[0].decode().split()

    def _process_email(
        self, conn: imaplib.IMAP4_SSL, uid: str
    ) -> list[DownloadedAttachment]:
        # BODY.PEEK[] fetches the message without setting the \Seen flag,
        # so if another client later reads the email it'll look unread as
        # the user expects. Processing tracking happens entirely in our
        # SQLite database.
        status, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            logger.warning("Failed to fetch UID %s", uid)
            return []

        # data structure: [(header_info_bytes, body_bytes), b')']
        # find the tuple with the payload
        raw = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
                break
        if raw is None:
            logger.warning("Could not locate message body for UID %s", uid)
            return []

        msg = email.message_from_bytes(raw, policy=email.policy.default)

        sender = msg.get("From", "")
        subject = msg.get("Subject", "")

        # Extract just the email address from "Name <email>" format
        sender_addr = email.utils.parseaddr(sender)[1].lower()

        if not self._is_sender_allowed(sender_addr):
            logger.info("Sender %s not in whitelist, skipping", sender_addr)
            self._mark_uid_processed(uid, sender_addr, subject, 0)
            return []

        if not self._matches_subject_filter(subject):
            logger.info(
                "Subject %r does not match filter %r, skipping",
                subject, self.subject_filter,
            )
            self._mark_uid_processed(uid, sender_addr, subject, 0)
            return []

        attachments = self._extract_attachments(msg, uid, sender_addr)

        # Record UID in our DB — this is the only dedup mechanism now.
        self._mark_uid_processed(uid, sender_addr, subject, len(attachments))

        if attachments:
            logger.info(
                "Downloaded %d attachment(s) from %s (UID %s)",
                len(attachments), sender_addr, uid,
            )

        return attachments

    def _extract_attachments(
        self, msg: email.message.Message, uid: str, sender: str
    ) -> list[DownloadedAttachment]:
        attachments = []

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            content_id = part.get("Content-ID")
            filename = part.get_filename()

            # Skip non-attachment parts
            if part.get_content_maintype() == "multipart":
                continue

            ext = Path(filename).suffix.lower() if filename else ""

            # Determine if this is an image or video attachment
            is_image_type = content_type in IMAGE_CONTENT_TYPES
            is_image_ext = ext in IMAGE_EXTENSIONS
            is_video_type = content_type.startswith("video/")
            is_video_ext = ext in VIDEO_EXTENSIONS

            # Also accept application/octet-stream with image/video extension
            if content_type == "application/octet-stream":
                if is_image_ext:
                    is_image_type = True
                if is_video_ext:
                    is_video_type = True

            is_image = is_image_type or is_image_ext
            is_video = is_video_type or is_video_ext

            if not is_image and not is_video:
                continue

            if is_video and not self.accept_videos:
                logger.debug("Skipping video %s (accept_videos is false)", filename)
                continue

            # Decode the payload ONCE — calling get_payload(decode=True)
            # a second time can return None for some encodings under
            # email.policy.default, silently dropping attachments.
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            # Skip inline images (logos, signatures) unless large
            if is_image and content_id and "inline" in disposition.lower():
                if len(payload) < 100_000:  # < 100KB
                    logger.debug("Skipping small inline image: %s", filename)
                    continue

            # Sanitize filename
            if not filename:
                if is_video:
                    filename = "unnamed.mp4"
                else:
                    default_ext = ".jpg" if "jpeg" in content_type else ".png"
                    filename = f"unnamed{default_ext}"
            safe_name = SAFE_FILENAME_RE.sub("_", filename)
            save_path = self.inbox_dir / f"{uid}_{safe_name}"

            # Disambiguate if two attachments in the same email share
            # the same sanitized filename. Without this the second write
            # would silently overwrite the first.
            if save_path.exists():
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                counter = 1
                while save_path.exists():
                    save_path = self.inbox_dir / f"{uid}_{counter}_{stem}{suffix}"
                    counter += 1

            save_path.write_bytes(payload)
            attachments.append(DownloadedAttachment(
                file_path=save_path,
                original_filename=filename,
                sender=sender,
                uid=uid,
                download_time=datetime.now(),
                is_video=is_video,
            ))
            kind = "video" if is_video else "image"
            logger.debug("Saved %s: %s (%d KB)", kind, save_path.name, len(payload) // 1024)

        return attachments

    def _matches_subject_filter(self, subject: str) -> bool:
        """Case-insensitive substring match against configured subject filter."""
        if not self.subject_filter:
            return True  # No filter = accept all
        return self.subject_filter in (subject or "").lower()

    def _is_sender_allowed(self, sender_addr: str) -> bool:
        if not self.allowed_senders:
            return True
        return sender_addr in self.allowed_senders

    def _is_uid_processed(self, uid: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM processed_emails WHERE uid = ?", (uid,)
        ).fetchone()
        return row is not None

    def _mark_uid_processed(
        self, uid: str, sender: str, subject: str, attachment_count: int
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO processed_emails (uid, sender, subject, attachment_count) VALUES (?, ?, ?, ?)",
            (uid, sender, subject, attachment_count),
        )
        self.db.commit()
