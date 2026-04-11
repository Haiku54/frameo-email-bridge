"""ADB frame pusher module for Frameo email bridge.

Manages ADB connection to a Frameo frame over WiFi and pushes processed
images to the frame's photo directory.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class AdbError(Exception):
    pass


class FrameConnectionError(AdbError):
    pass


class PushError(AdbError):
    pass


class FramePusher:
    def __init__(self, config: dict, archive_dir: Path):
        frame_cfg = config["frame"]
        self.ip = frame_cfg["adb_ip"]
        self.port = frame_cfg.get("adb_port", 5555)
        self.photo_path = frame_cfg["photo_path"].rstrip("/") + "/"
        self.device = f"{self.ip}:{self.port}"
        self.push_timeout = frame_cfg.get("push_timeout", 60)
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def health_check(self) -> bool:
        """Verify ADB is available and frame is reachable."""
        if not shutil.which("adb"):
            logger.critical("adb not found in PATH. Install with: sudo apt install adb")
            return False

        if not self._ensure_connected():
            logger.warning("Cannot connect to frame at %s", self.device)
            return False

        # Verify photo path exists
        try:
            result = self._run_adb("shell", "ls", self.photo_path)
            logger.info("Frame connected. Photo path %s is accessible.", self.photo_path)
            return True
        except AdbError as e:
            logger.warning("Photo path %s not accessible: %s", self.photo_path, e)
            return False

    def push_photo(self, local_path: Path) -> bool:
        """Push a processed image to the frame.

        On success, moves the local file to archive_dir.
        On failure, leaves the file in place for retry next cycle.
        Returns True on success.
        """
        local_path = Path(local_path)
        remote_path = self.photo_path + local_path.name
        last_error = None

        for attempt in range(1, 4):
            try:
                if not self._ensure_connected():
                    raise FrameConnectionError(f"Cannot connect to {self.device}")

                # Push the file
                self._run_adb(
                    "push", str(local_path), remote_path,
                    timeout=self.push_timeout,
                )

                # Trigger media scanner so Frameo picks up the new file
                self._trigger_media_scan(remote_path)

                # Move to archive
                archive_path = self.archive_dir / local_path.name
                shutil.move(str(local_path), str(archive_path))

                logger.info("Pushed to frame: %s", local_path.name)
                return True

            except AdbError as e:
                last_error = e
                logger.warning(
                    "Push attempt %d/3 failed for %s: %s",
                    attempt, local_path.name, e,
                )
                if attempt < 3:
                    self._disconnect()
                    time.sleep(5)

        logger.error(
            "All push attempts failed for %s: %s", local_path.name, last_error
        )
        return False

    def _run_adb(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = ["adb", "-s", self.device] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise AdbError(f"ADB command timed out ({timeout}s): {' '.join(cmd)}")

        if result.returncode != 0:
            raise AdbError(
                f"ADB command failed (rc={result.returncode}): {' '.join(cmd)}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result

    def _is_connected(self) -> bool:
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if self.device in line and "device" in line and "offline" not in line:
                    return True
            return False
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _connect(self) -> bool:
        try:
            result = subprocess.run(
                ["adb", "connect", self.device],
                capture_output=True, text=True, timeout=15,
            )
            output = result.stdout.strip()
            return "connected" in output.lower()
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("ADB connect failed: %s", e)
            return False

    def _ensure_connected(self) -> bool:
        if self._is_connected():
            return True
        logger.info("Reconnecting to frame at %s...", self.device)
        for attempt in range(3):
            if self._connect():
                logger.info("Connected to frame")
                return True
            time.sleep(2)
        return False

    def _disconnect(self) -> None:
        try:
            subprocess.run(
                ["adb", "disconnect", self.device],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass

    def _trigger_media_scan(self, remote_path: str) -> None:
        """Tell Android's MediaStore to scan the new file so Frameo sees it."""
        try:
            self._run_adb(
                "shell", "am", "broadcast",
                "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d", f"file://{remote_path}",
            )
        except AdbError as e:
            # Non-fatal: frame may still detect the file on its own
            logger.debug("Media scan broadcast failed (non-fatal): %s", e)
