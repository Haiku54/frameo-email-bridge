#!/usr/bin/env python3
"""Interactive ADB setup for Frameo Email Bridge.

One-time setup script that:
1. Checks ADB is installed
2. Finds USB-connected Frameo frame
3. Enables WiFi ADB (tcpip mode)
4. Discovers the frame's IP and photo directory
5. Saves settings to config.yaml
"""

import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

KNOWN_PHOTO_PATHS = [
    "/sdcard/DCIM/",       # Frameo reads from here
    "/sdcard/Pictures/",
    "/sdcard/Download/",
    "/sdcard/Frameo/",     # Config files only, NOT for photos
]


def main():
    print("=" * 50)
    print("  Frameo Email Bridge - ADB Setup")
    print("=" * 50)
    print()

    # Step 1: Check ADB
    if not check_adb_installed():
        sys.exit(1)

    # Step 2: Find USB device
    serial = wait_for_usb_device()
    if not serial:
        sys.exit(1)

    # Step 3: Get device info
    model = run_adb(serial, "shell", "getprop", "ro.product.model").strip()
    print(f"  Device model: {model}")

    # Check Frameo is installed
    packages = run_adb(serial, "shell", "pm", "list", "packages")
    if "net.frameo" not in packages:
        print("\n  WARNING: Frameo app not found on this device.")
        answer = input("  Continue anyway? [y/N] ").strip().lower()
        if answer != "y":
            sys.exit(1)
    else:
        print("  Frameo app: installed")

    # Step 4: Get WiFi IP
    ip = get_device_ip(serial)
    if not ip:
        ip = input("\n  Could not detect IP. Enter frame's WiFi IP manually: ").strip()
        if not ip:
            print("  ERROR: No IP address provided.")
            sys.exit(1)
    print(f"  Frame WiFi IP: {ip}")

    # Step 5: Enable TCP/IP mode
    print("\n  Enabling ADB over WiFi...")
    enable_tcpip(serial)

    # Step 6: Connect over WiFi
    print(f"  Connecting to {ip}:5555...")
    time.sleep(1.5)
    if not connect_wifi(ip, 5555):
        print("  ERROR: Could not connect over WiFi.")
        print("  Make sure the frame is on the same WiFi network as this computer.")
        sys.exit(1)
    print("  WiFi ADB connection: OK")

    # Step 7: Verify (optionally disconnect USB)
    print("\n  You can now disconnect the USB cable.")
    input("  Press Enter to continue after disconnecting USB (or just press Enter)...")

    device = f"{ip}:5555"
    try:
        result = subprocess.run(
            ["adb", "-s", device, "shell", "echo", "ok"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() != "ok":
            print("  WARNING: WiFi connection may not be stable.")
    except Exception:
        print("  WARNING: Could not verify WiFi connection.")

    # Step 8: Discover photo path
    photo_path = discover_photo_path(device)
    if not photo_path:
        print("\n  Could not auto-detect photo directory.")
        photo_path = input("  Enter the path manually (e.g., /sdcard/Frameo/): ").strip()
        if not photo_path:
            photo_path = "/sdcard/Frameo/"
    print(f"  Photo directory: {photo_path}")

    # Step 9: Save to config
    update_config(ip, 5555, photo_path)

    print("\n" + "=" * 50)
    print("  Setup complete!")
    print(f"  Frame IP: {ip}")
    print(f"  Photo path: {photo_path}")
    print()
    print("  Next steps:")
    print("  1. Edit config.yaml with your Gmail credentials")
    print("  2. Run: python main.py")
    print("=" * 50)


def check_adb_installed() -> bool:
    if shutil.which("adb"):
        version = subprocess.run(
            ["adb", "version"], capture_output=True, text=True
        ).stdout.splitlines()[0]
        print(f"  ADB: {version}")
        return True

    os_name = platform.system()
    print("  ERROR: adb is not installed.\n")
    if os_name == "Linux":
        print("  Install with: sudo apt install adb")
    elif os_name == "Darwin":
        print("  Install with: brew install android-platform-tools")
    elif os_name == "Windows":
        print("  Download from: https://developer.android.com/tools/releases/platform-tools")
    return False


def wait_for_usb_device() -> str | None:
    print("\n  Looking for USB-connected device...")
    for attempt in range(12):  # Wait up to ~60 seconds
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            # Skip WiFi devices
            if ":" in serial:
                continue
            if state == "device":
                print(f"  Found device: {serial}")
                return serial
            if state == "unauthorized":
                print("  Device found but UNAUTHORIZED.")
                print("  Check the frame's screen for a USB debugging authorization popup.")
                print("  Tap 'Allow' or 'OK', then press Enter here.")
                input("  Press Enter to retry...")
                continue

        if attempt == 0:
            print("  No device found. Make sure the frame is connected via USB.")
            print("  USB debugging must be enabled on the frame.")
        if attempt < 11:
            time.sleep(5)

    print("  ERROR: No device found after 60 seconds.")
    return None


def get_device_ip(serial: str) -> str | None:
    # Try ip route
    try:
        output = run_adb(serial, "shell", "ip", "route")
        match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    except Exception:
        pass

    # Try ifconfig wlan0
    try:
        output = run_adb(serial, "shell", "ifconfig", "wlan0")
        match = re.search(r"inet addr:(\d+\.\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    except Exception:
        pass

    # Try ip addr show wlan0
    try:
        output = run_adb(serial, "shell", "ip", "addr", "show", "wlan0")
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    except Exception:
        pass

    return None


def detect_resolution(device: str) -> tuple[int, int] | None:
    """Get screen resolution from a connected ADB device via `wm size`.

    `device` can be a USB serial or a wifi endpoint like '192.168.1.5:5555'.
    Returns (width, height) or None on failure.
    """
    try:
        result = subprocess.run(
            ["adb", "-s", device, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    match = re.search(r"Physical size:\s*(\d+)x(\d+)", result.stdout)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def enable_tcpip(serial: str) -> None:
    try:
        subprocess.run(
            ["adb", "-s", serial, "tcpip", "5555"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        pass  # tcpip command sometimes hangs but still works


def connect_wifi(ip: str, port: int) -> bool:
    device = f"{ip}:{port}"
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["adb", "connect", device],
                capture_output=True, text=True, timeout=15,
            )
            if "connected" in result.stdout.lower():
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    return False


def discover_photo_path(device: str) -> str | None:
    for path in KNOWN_PHOTO_PATHS:
        try:
            result = subprocess.run(
                ["adb", "-s", device, "shell", "ls", path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                continue

            # Verify write access by actually trying to create and remove a file.
            # Just relying on `ls` succeeding is not sufficient — on some
            # Android versions /sdcard paths are readable but not writable.
            test_file = path + ".adb_setup_test"
            touch_result = subprocess.run(
                ["adb", "-s", device, "shell", "touch", test_file],
                capture_output=True, text=True, timeout=10,
            )
            # Clean up the test file regardless of whether touch reported success
            subprocess.run(
                ["adb", "-s", device, "shell", "rm", "-f", test_file],
                capture_output=True, text=True, timeout=10,
            )
            if touch_result.returncode != 0:
                # `touch` failed — this directory is not writable
                continue
            print(f"  Found writable directory: {path}")
            return path
        except subprocess.TimeoutExpired:
            continue
    return None


def update_config(ip: str, port: int, photo_path: str) -> None:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    if "frame" not in config:
        config["frame"] = {}
    config["frame"]["adb_ip"] = ip
    config["frame"]["adb_port"] = port
    config["frame"]["photo_path"] = photo_path

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    print(f"\n  Config saved to {config_path}")


def run_adb(serial: str, *args: str) -> str:
    cmd = ["adb", "-s", serial] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ADB command timed out after 30s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError("adb is not installed on this system")
    return result.stdout


if __name__ == "__main__":
    main()
