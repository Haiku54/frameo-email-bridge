#!/usr/bin/env python3
"""Google Photos sync module for Frameo Email Bridge.

Uploads archived photos/videos to a shared Google Photos album.
Handles OAuth2 authentication, two-step upload, album management,
and bidirectional sync tracking via SQLite.

Standalone usage:
    python google_photos.py --auth          # One-time OAuth consent flow
    python google_photos.py --auth --headless  # Headless (print URL, no browser)
"""

import argparse
import logging
import mimetypes
import os
import sqlite3
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

import yaml

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]

API_BASE = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = f"{API_BASE}/uploads"


class GooglePhotosSync:
    """Manages uploads and sync to a shared Google Photos album."""

    def __init__(self, config: dict, data_dir: Path):
        gp_cfg = config.get("google_photos", {})
        self.credentials_file = Path(gp_cfg.get("credentials_file", "credentials.json"))
        # Resolve relative paths against the project root (where config.yaml lives)
        if not self.credentials_file.is_absolute():
            self.credentials_file = Path(__file__).parent / self.credentials_file
        self.album_name = gp_cfg.get("album_name", "Frameo Photos")
        self.data_dir = Path(data_dir)
        self.token_path = self.data_dir / "token.json"
        self.session: AuthorizedSession | None = None
        self._album_id: str | None = None
        self._db = self._init_db()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, headless: bool = False) -> bool:
        """Load or refresh credentials and create an authorized session.

        Returns True on success.
        """
        creds = self._load_or_refresh_credentials()
        if creds is None:
            if not self.credentials_file.exists():
                logger.error(
                    "credentials.json not found at %s — download it from "
                    "Google Cloud Console (see setup instructions)",
                    self.credentials_file,
                )
                return False
            creds = self._run_oauth_flow(headless=headless)
            if creds is None:
                return False

        self.session = AuthorizedSession(creds)
        logger.info("Google Photos authenticated")
        return True

    def _load_or_refresh_credentials(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        try:
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        except Exception as e:
            logger.warning("Could not load token.json: %s", e)
            return None

        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
                return creds
            except Exception as e:
                logger.warning("Token refresh failed: %s", e)
                return None
        return None

    def _run_oauth_flow(self, headless: bool = False) -> Credentials | None:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_file), SCOPES,
            )
            if headless:
                # Print URL for manual browser visit — works on headless/Pi
                creds = flow.run_console()
            else:
                creds = flow.run_local_server(port=0)
            self._save_token(creds)
            return creds
        except Exception as e:
            logger.error("OAuth flow failed: %s", e)
            return None

    def _save_token(self, creds: Credentials) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Write with restricted permissions (owner-only) — token contains
        # a long-lived refresh token granting Google Photos access.
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(creds.to_json())
        logger.debug("Token saved to %s", self.token_path)

    # ------------------------------------------------------------------
    # Album management
    # ------------------------------------------------------------------

    def _ensure_album(self) -> str:
        """Return the album ID, creating the album if needed."""
        if self._album_id:
            return self._album_id

        # Check SQLite for a stored album_id
        stored_id = self._get_meta("album_id")
        stored_name = self._get_meta("album_name")
        if stored_id and stored_name == self.album_name:
            # Verify it still exists
            resp = self.session.get(f"{API_BASE}/albums/{stored_id}")
            if resp.status_code == 200:
                self._album_id = stored_id
                return stored_id
            logger.info("Stored album no longer exists, creating new one")

        # Create album
        resp = self.session.post(
            f"{API_BASE}/albums",
            json={"album": {"title": self.album_name}},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to create album: {resp.status_code} {resp.text}")

        album_id = resp.json()["id"]
        self._album_id = album_id
        self._set_meta("album_id", album_id)
        self._set_meta("album_name", self.album_name)

        logger.info("Created Google Photos album: %s (id=%s)", self.album_name, album_id)
        logger.info("Share the album manually: open Google Photos → Albums → '%s' → Share", self.album_name)
        return album_id

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(self, file_path: Path) -> bool:
        """Upload a single file to the Google Photos album.

        Returns True on success.
        """
        if not self.session:
            logger.warning("Google Photos not authenticated, skipping upload")
            return False

        file_path = Path(file_path)
        if not file_path.exists():
            logger.warning("File not found for GP upload: %s", file_path)
            return False

        # Skip if already uploaded
        if self._is_uploaded(file_path.name):
            logger.debug("Already in Google Photos: %s", file_path.name)
            return True

        # Step 1: upload raw bytes
        upload_token = self._upload_bytes(file_path)
        if not upload_token:
            return False

        # Step 2: create media item in album
        album_id = self._ensure_album()
        media_item_id = self._create_media_item(upload_token, file_path.name, album_id)
        if not media_item_id:
            return False

        self.mark_uploaded(file_path.name, media_item_id)
        logger.info("Uploaded to Google Photos: %s", file_path.name)
        return True

    def _upload_bytes(self, file_path: Path) -> str | None:
        """Upload raw bytes to Google Photos. Returns upload token or None."""
        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                resp = self.session.post(
                    UPLOAD_URL,
                    data=f,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Goog-Upload-Content-Type": mime_type,
                        "X-Goog-Upload-Protocol": "raw",
                    },
                )
            if resp.status_code == 200:
                return resp.text
            logger.error(
                "Upload bytes failed for %s: %d %s",
                file_path.name, resp.status_code, resp.text,
            )
        except Exception as e:
            logger.error("Upload bytes exception for %s: %s", file_path.name, e)
        return None

    def _create_media_item(
        self, upload_token: str, filename: str, album_id: str,
    ) -> str | None:
        """Create a media item in the album. Returns media_item_id or None."""
        try:
            resp = self.session.post(
                f"{API_BASE}/mediaItems:batchCreate",
                json={
                    "albumId": album_id,
                    "newMediaItems": [
                        {
                            "simpleMediaItem": {
                                "fileName": filename,
                                "uploadToken": upload_token,
                            }
                        }
                    ],
                },
            )
            if resp.status_code != 200:
                logger.error("batchCreate failed: %d %s", resp.status_code, resp.text)
                return None

            result = resp.json().get("newMediaItemResults", [{}])[0]
            status = result.get("status", {})
            if status.get("message") != "Success":
                logger.error("batchCreate item failed: %s", status)
                return None

            return result["mediaItem"]["id"]
        except Exception as e:
            logger.error("batchCreate exception: %s", e)
            return None

    # ------------------------------------------------------------------
    # Album listing & removal (for full sync)
    # ------------------------------------------------------------------

    def list_album_items(self) -> dict[str, str]:
        """Return {filename: media_item_id} for all items in the album."""
        if not self.session:
            return {}
        album_id = self._get_meta("album_id")
        if not album_id:
            return {}

        items: dict[str, str] = {}
        page_token = None
        while True:
            body: dict = {"albumId": album_id, "pageSize": 100}
            if page_token:
                body["pageToken"] = page_token
            resp = self.session.post(f"{API_BASE}/mediaItems:search", json=body)
            if resp.status_code != 200:
                logger.warning("mediaItems:search failed: %d", resp.status_code)
                break
            data = resp.json()
            for item in data.get("mediaItems", []):
                items[item.get("filename", "")] = item["id"]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    def remove_from_album(self, media_item_ids: list[str]) -> bool:
        """Remove media items from the album (max 50 per call)."""
        if not self.session or not media_item_ids:
            return False
        album_id = self._get_meta("album_id")
        if not album_id:
            return False

        try:
            resp = self.session.post(
                f"{API_BASE}/albums/{album_id}:batchRemoveMediaItems",
                json={"mediaItemIds": media_item_ids},
            )
            if resp.status_code != 200:
                logger.error(
                    "batchRemoveMediaItems failed: %d %s",
                    resp.status_code, resp.text,
                )
                return False
            return True
        except Exception as e:
            logger.error("batchRemoveMediaItems exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # SQLite tracking
    # ------------------------------------------------------------------

    def _init_db(self) -> sqlite3.Connection:
        db_path = self.data_dir / "processed_emails.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS google_photos_uploads ("
            "  filename TEXT PRIMARY KEY,"
            "  media_item_id TEXT NOT NULL,"
            "  album_id TEXT NOT NULL,"
            "  uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS google_photos_meta ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS google_photos_removed ("
            "  filename TEXT PRIMARY KEY,"
            "  removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()
        return conn

    def get_uploaded_files(self) -> dict[str, str]:
        """Return {filename: media_item_id} from the local DB."""
        rows = self._db.execute(
            "SELECT filename, media_item_id FROM google_photos_uploads"
        ).fetchall()
        return dict(rows)

    def _is_uploaded(self, filename: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM google_photos_uploads WHERE filename = ?", (filename,)
        ).fetchone()
        return row is not None

    def mark_uploaded(self, filename: str, media_item_id: str) -> None:
        album_id = self._album_id or self._get_meta("album_id") or ""
        self._db.execute(
            "INSERT OR REPLACE INTO google_photos_uploads "
            "(filename, media_item_id, album_id) VALUES (?, ?, ?)",
            (filename, media_item_id, album_id),
        )
        # Clear from removed list if it was re-uploaded
        self._db.execute(
            "DELETE FROM google_photos_removed WHERE filename = ?", (filename,)
        )
        self._db.commit()

    def mark_removed(self, filename: str) -> None:
        """Mark a file as removed from the album. Tracks it so retry won't re-upload."""
        self._db.execute(
            "DELETE FROM google_photos_uploads WHERE filename = ?", (filename,)
        )
        self._db.execute(
            "INSERT OR IGNORE INTO google_photos_removed (filename) VALUES (?)",
            (filename,),
        )
        self._db.commit()

    def is_removed(self, filename: str) -> bool:
        """Check if a file was previously removed (deleted from frame)."""
        row = self._db.execute(
            "SELECT 1 FROM google_photos_removed WHERE filename = ?", (filename,)
        ).fetchone()
        return row is not None

    def _get_meta(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM google_photos_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO google_photos_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._db.commit()

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._db:
            self._db.close()


# ======================================================================
# Standalone auth CLI
# ======================================================================

def _cli_auth():
    parser = argparse.ArgumentParser(description="Google Photos auth helper")
    parser.add_argument("--auth", action="store_true", help="Run OAuth consent flow")
    parser.add_argument(
        "--headless", action="store_true",
        help="Headless mode: print URL instead of opening browser",
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file",
    )
    args = parser.parse_args()

    if not args.auth:
        parser.print_help()
        return

    # Minimal logging to console
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run setup.sh first.")
        return

    config = yaml.safe_load(config_path.read_text())
    data_dir = Path(__file__).parent / "data"

    gp = GooglePhotosSync(config, data_dir)
    if gp.authenticate(headless=args.headless):
        print()
        print("Authentication successful!")
        print(f"Token saved to {gp.token_path}")
        # Also verify album creation
        try:
            album_id = gp._ensure_album()
            print(f"Album '{gp.album_name}' ready (id={album_id})")
            print()
            print("Next: share the album with family manually:")
            print("  Open Google Photos -> Albums -> '%s' -> Share" % gp.album_name)
        except Exception as e:
            print(f"Album creation will happen on first upload: {e}")
        gp.close()
    else:
        print("Authentication failed. Check the error messages above.")


if __name__ == "__main__":
    _cli_auth()
