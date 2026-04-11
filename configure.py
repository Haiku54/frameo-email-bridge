#!/usr/bin/env python3
"""Interactive configuration for Frameo Email Bridge.

Prompts the user for Gmail credentials, subject/sender filters, video
settings, and discovers the Frameo frame via USB or local network scan.
Writes the result to config.yaml.
"""

import getpass
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

import adb_setup
import discover_frame

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXAMPLE_PATH = Path(__file__).parent / "config.yaml.example"


def main() -> None:
    print("=" * 60)
    print("  Frameo Email Bridge - Interactive Setup")
    print("=" * 60)
    print()

    # Start from example or existing config
    config = _load_base_config()

    # Gmail section
    print("--- Gmail Settings ---")
    config["email"]["username"] = _prompt(
        "Gmail address", default=config["email"].get("username", "")
    )
    config["email"]["password"] = _prompt_password(
        current=config["email"].get("password", "")
    )
    config["email"]["poll_interval_seconds"] = int(_prompt(
        "Poll interval in seconds",
        default=str(config["email"].get("poll_interval_seconds", 120)),
    ))
    print()

    # Email filter section
    print("--- Email Filters ---")
    print("Subject filter: only emails with this subject will be processed.")
    print("Leave blank to accept all subjects, or enter a keyword like 'Frameo'.")
    config["email"]["subject_filter"] = _prompt(
        "Subject filter", default=config["email"].get("subject_filter", ""),
        allow_blank=True,
    )

    senders_str = _prompt(
        "Allowed senders (comma-separated, blank for all)",
        default=",".join(config["email"].get("allowed_senders") or []),
        allow_blank=True,
    )
    config["email"]["allowed_senders"] = [
        s.strip() for s in senders_str.split(",") if s.strip()
    ]
    print()

    # Video section
    print("--- Video Support ---")
    print()
    print("WARNING: Your Frameo frame has a maximum video duration that varies")
    print("by model. Frameo typically supports videos up to 15 seconds.")
    print("  - Older models: 10 seconds")
    print("  - Newer models: 15 seconds")
    print("If unsure, start with 10 seconds. Longer videos are trimmed automatically.")
    print()
    accept_videos = _prompt_yesno(
        "Accept video attachments",
        default=config["processing"].get("accept_videos", True),
    )
    config["processing"]["accept_videos"] = accept_videos
    if accept_videos:
        config["processing"]["max_video_duration_seconds"] = int(_prompt(
            "Maximum video duration (seconds)",
            default=str(config["processing"].get("max_video_duration_seconds", 10)),
        ))
        if not shutil.which("ffmpeg"):
            print()
            print("  WARNING: ffmpeg is not installed. Video processing will fail.")
            print("  Install with: sudo apt install ffmpeg")
    print()

    # Frame discovery section
    print("--- Frame Discovery ---")
    frame_info = _discover_frame()
    if not frame_info:
        print()
        print("  Could not find a Frameo frame automatically.")
        ip = _prompt("Enter frame IP address manually", default="")
        if not ip:
            print("  ERROR: No frame configured. Aborting.")
            sys.exit(1)
        frame_info = {"ip": ip, "resolution": (1280, 800), "photo_path": "/sdcard/DCIM/"}

    config["frame"]["adb_ip"] = frame_info["ip"]
    config["frame"]["adb_port"] = 5555
    config["frame"]["photo_path"] = frame_info.get("photo_path") or "/sdcard/DCIM/"
    w, h = frame_info.get("resolution") or (1280, 800)
    if w and h:
        config["frame"]["resolution_width"] = w
        config["frame"]["resolution_height"] = h
    print(f"  Frame IP:        {frame_info['ip']}")
    print(f"  Resolution:      {config['frame']['resolution_width']}x{config['frame']['resolution_height']}")
    print(f"  Photo path:      {config['frame']['photo_path']}")
    print()

    # Save config
    _save_config(config)
    print(f"  Config saved to {CONFIG_PATH}")
    print()

    # Offer test push
    if _prompt_yesno("Send a test image to the frame now", default=True):
        _test_push(config)

    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("  Next steps:")
    print("    - Run manually:  python main.py")
    print("    - Run as service: see README.md for systemd/Docker setup")
    print("    - IMPORTANT: set a static IP / DHCP reservation on your router")
    print(f"      for the frame ({frame_info['ip']}) so it doesn't change.")
    print()


def _load_base_config() -> dict:
    """Load the existing config.yaml if present, otherwise the example."""
    if CONFIG_PATH.exists():
        print(f"  Existing config.yaml found at {CONFIG_PATH}")
        if _prompt_yesno("Start from existing config (keep current values as defaults)", default=True):
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or _load_example()
    return _load_example()


def _load_example() -> dict:
    if not EXAMPLE_PATH.exists():
        print(f"  ERROR: {EXAMPLE_PATH} not found. Cannot continue.")
        sys.exit(1)
    with open(EXAMPLE_PATH) as f:
        return yaml.safe_load(f)


def _save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _prompt(label: str, default: str = "", allow_blank: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        value = input(f"  {label}{hint}: ").strip()
        if not value:
            value = default
        if value or allow_blank:
            return value
        print("  (required — please enter a value)")


def _prompt_password(current: str) -> str:
    hint = " [keep current]" if current else ""
    print("  Gmail App Password (input hidden, 16 chars with or without spaces)")
    value = getpass.getpass(f"  Password{hint}: ").strip()
    if not value and current:
        return current
    if not value:
        print("  (required — please enter a value)")
        return _prompt_password(current)
    return value


def _prompt_yesno(label: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {label}? {hint}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _discover_frame() -> dict | None:
    """Try USB first, then network scan. Returns frame info dict or None."""
    if not shutil.which("adb"):
        print("  ERROR: adb is not installed on this system.")
        print("  Install it first:")
        print("    Linux:   sudo apt install adb")
        print("    macOS:   brew install android-platform-tools")
        print("    Windows: https://developer.android.com/tools/releases/platform-tools")
        sys.exit(1)

    # Check for USB device
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10,
        )
        usb_serial = None
        for line in result.stdout.splitlines()[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1] == "device" and ":" not in parts[0]:
                usb_serial = parts[0]
                break

        if usb_serial:
            print(f"  Found USB-connected device: {usb_serial}")
            return _setup_via_usb(usb_serial)
    except subprocess.TimeoutExpired:
        print("  WARNING: `adb devices` timed out")

    # No USB device - try network scan
    print("  No USB device found. Scanning local network...")
    try:
        frames = discover_frame.find_frames()
    except discover_frame.AdbNotInstalledError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    if len(frames) == 1:
        return frames[0]
    if len(frames) > 1:
        print(f"  Found {len(frames)} frames:")
        for i, f in enumerate(frames, 1):
            print(f"    {i}. {f['ip']} ({f.get('model', 'unknown')})")
        choice = _prompt("Select frame number", default="1")
        try:
            return frames[int(choice) - 1]
        except (ValueError, IndexError):
            return frames[0]
    return None


def _setup_via_usb(serial: str) -> dict | None:
    """Run the USB flow from adb_setup.py: enable tcpip, connect via WiFi, detect props."""
    ip = adb_setup.get_device_ip(serial)
    if not ip:
        ip = _prompt("Could not auto-detect IP. Enter frame WiFi IP manually", default="")
        if not ip:
            return None

    print(f"  Frame WiFi IP: {ip}")
    print("  Enabling ADB over WiFi...")
    adb_setup.enable_tcpip(serial)
    time.sleep(1.5)

    if not adb_setup.connect_wifi(ip, 5555):
        print("  ERROR: Could not connect over WiFi. Is the frame on the same network?")
        return None
    print("  WiFi ADB connection: OK")
    print("  You can now disconnect the USB cable.")

    device = f"{ip}:5555"
    resolution = adb_setup.detect_resolution(device) or (1280, 800)
    photo_path = adb_setup.discover_photo_path(device) or "/sdcard/DCIM/"

    return {
        "ip": ip,
        "resolution": resolution,
        "photo_path": photo_path,
    }


def _test_push(config: dict) -> None:
    """Generate a small test image and push it to the frame."""
    from datetime import datetime

    from PIL import Image, ImageDraw

    from frame_pusher import FramePusher

    w = config["frame"]["resolution_width"]
    h = config["frame"]["resolution_height"]
    test_path = Path(__file__).parent / "processed" / "_configure_test.jpg"
    test_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (w, h), (30, 80, 140))
    draw = ImageDraw.Draw(img)
    draw.text((w // 2 - 80, h // 2 - 20), "Frameo Test OK", fill="white")
    draw.text((w // 2 - 80, h // 2 + 10), datetime.now().strftime("%Y-%m-%d %H:%M"), fill="yellow")
    img.save(test_path, quality=90)

    archive_dir = Path(__file__).parent / "archive"
    pusher = FramePusher(config, archive_dir)
    if pusher.push_photo(test_path):
        print("  Test image pushed successfully. Check the frame.")
    else:
        print("  ERROR: Test push failed. Check logs and frame connectivity.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled.")
        sys.exit(1)
