"""ADB frame pusher module for Frameo email bridge.

Manages ADB connection to a Frameo frame over WiFi and pushes processed
images to the frame's photo directory.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class AdbError(Exception):
    pass


class FrameConnectionError(AdbError):
    pass


class PushError(AdbError):
    pass


class FramePusher:
    def __init__(self, config: dict, archive_dir: Path, config_path: Path | None = None):
        frame_cfg = config["frame"]
        self.ip = frame_cfg["adb_ip"]
        self.port = frame_cfg.get("adb_port", 5555)
        self.photo_path = frame_cfg["photo_path"].rstrip("/") + "/"
        self.device = f"{self.ip}:{self.port}"
        self.push_timeout = frame_cfg.get("push_timeout", 60)
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        # Full config + path, used to persist updated IP back to disk
        # after auto-rediscovery.
        self._config = config
        self._config_path = Path(config_path) if config_path else None
        # Throttle rediscovery attempts so we don't scan the subnet on
        # every failed push.
        self._last_rediscovery = 0.0
        self._rediscovery_cooldown = 60.0  # seconds

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
        last_error: Exception | None = None

        # Up to 3 attempts at the current IP, then one rediscovery sweep,
        # then 2 more attempts at the new IP if found.
        for attempt in range(1, 4):
            try:
                self._try_push(local_path)
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

        # All attempts at current IP failed — try rediscovering the frame
        # on the network in case its IP has changed. This handles the
        # common case where the router reassigns a different IP after a
        # reboot or DHCP lease renewal.
        if self._try_rediscover():
            logger.info("Frame rediscovered at %s, retrying push", self.device)
            for attempt in range(1, 3):
                try:
                    self._try_push(local_path)
                    return True
                except AdbError as e:
                    last_error = e
                    logger.warning(
                        "Post-rediscovery push attempt %d/2 failed: %s",
                        attempt, e,
                    )
                    if attempt < 2:
                        self._disconnect()
                        time.sleep(3)

        logger.error(
            "All push attempts failed for %s: %s", local_path.name, last_error
        )
        return False

    def _try_push(self, local_path: Path) -> None:
        """Single push attempt. Raises AdbError on any failure."""
        remote_path = self.photo_path + local_path.name
        if not self._ensure_connected():
            raise FrameConnectionError(f"Cannot connect to {self.device}")

        self._run_adb(
            "push", str(local_path), remote_path,
            timeout=self.push_timeout,
        )
        self._trigger_media_scan(remote_path)

        # Move to archive only after a fully successful push
        archive_path = self.archive_dir / local_path.name
        shutil.move(str(local_path), str(archive_path))
        logger.info("Pushed to frame: %s", local_path.name)

    def _try_rediscover(self) -> bool:
        """Scan the local network for a Frameo frame and switch to it if found.

        Throttled to once per `_rediscovery_cooldown` seconds so we don't
        spam network scans on every failed push in a long offline period.
        Returns True if we switched to a new, reachable IP.
        """
        now = time.monotonic()
        if now - self._last_rediscovery < self._rediscovery_cooldown:
            logger.debug("Rediscovery skipped (on cooldown)")
            return False
        self._last_rediscovery = now

        logger.info("Attempting to rediscover Frameo frame on the network...")
        try:
            # Lazy import to avoid a hard dependency cycle at module load time
            import discover_frame
            frames = discover_frame.find_frames()
        except Exception as e:
            logger.warning("Rediscovery failed: %s", e)
            return False

        if not frames:
            logger.warning("Rediscovery did not find any Frameo frames on the network")
            return False

        new_ip = frames[0]["ip"]
        if new_ip == self.ip:
            # Same IP — not a useful rediscovery
            logger.debug("Rediscovery found same IP %s", new_ip)
            return False

        logger.info("Frame IP changed: %s -> %s. Updating config.", self.ip, new_ip)
        self.ip = new_ip
        self.device = f"{self.ip}:{self.port}"
        self._config["frame"]["adb_ip"] = new_ip
        self._persist_config()
        return True

    def _persist_config(self) -> None:
        """Write the updated config back to disk so the new IP survives restart."""
        if not self._config_path:
            return
        try:
            with open(self._config_path, "w") as f:
                yaml.dump(self._config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            logger.info("Saved updated frame IP to %s", self._config_path)
        except OSError as e:
            logger.error("Could not persist config to %s: %s", self._config_path, e)

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
        except FileNotFoundError:
            # adb disappeared from PATH (uninstalled) after startup — the
            # health check at startup may have passed, so we still need to
            # handle this at every push.
            raise AdbError("adb is not installed or not on PATH")

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
