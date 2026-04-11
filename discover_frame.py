#!/usr/bin/env python3
"""Network scanner to discover Frameo frames on the local WiFi network.

Scans the local subnet for devices with ADB listening on port 5555,
then verifies which ones are actually Frameo frames.
"""

import ipaddress
import logging
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

ADB_PORT = 5555
SCAN_TIMEOUT = 0.5  # seconds per host for the port scan
ADB_CONNECT_TIMEOUT = 8
MAX_WORKERS = 64


def find_frames() -> list[dict]:
    """Full discovery: scan subnet, connect via ADB, identify Frameo frames.

    Returns a list of dicts: {ip, model, resolution, photo_path}.
    """
    subnet = get_local_subnet()
    if not subnet:
        logger.warning("Could not detect local subnet")
        return []

    print(f"  Scanning {subnet} for ADB devices on port {ADB_PORT}...")
    candidates = scan_port(subnet, ADB_PORT)

    if not candidates:
        print("  No ADB devices found on the network.")
        return []

    print(f"  Found {len(candidates)} host(s) with port {ADB_PORT} open. Verifying...")
    frames = []
    for ip in candidates:
        info = identify_frameo(ip)
        if info:
            frames.append(info)
            print(f"  Frameo frame: {ip} ({info.get('model', 'unknown model')})")

    return frames


def get_local_subnet() -> str | None:
    """Detect the local IPv4 subnet as CIDR string (e.g., '192.168.1.0/24').

    Parses `ip -4 -o addr show` output, picks the first non-loopback interface.
    """
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _fallback_subnet()

    for line in result.stdout.splitlines():
        # Example: "2: wlan0    inet 192.168.1.42/24 brd 192.168.1.255 scope global ..."
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not match:
            continue
        ip_str, prefix = match.group(1), int(match.group(2))
        if ip_str.startswith("127."):
            continue
        try:
            network = ipaddress.ip_network(f"{ip_str}/{prefix}", strict=False)
            return str(network)
        except ValueError:
            continue

    return _fallback_subnet()


def _fallback_subnet() -> str | None:
    """Fallback: use a UDP socket to detect our IP, assume /24."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
        return str(network)
    except OSError:
        return None


def scan_port(subnet: str, port: int) -> list[str]:
    """Parallel TCP connect scan of the subnet for the given port.

    Returns list of IPs that accepted the connection.
    """
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return []

    hosts = [str(h) for h in network.hosts()]
    if len(hosts) > 1024:
        # Safety: don't scan huge networks
        logger.warning("Subnet too large (%d hosts), skipping scan", len(hosts))
        return []

    alive = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_check_port, ip, port): ip for ip in hosts}
        for future in as_completed(futures):
            if future.result():
                alive.append(futures[future])

    return sorted(alive, key=lambda ip: tuple(int(p) for p in ip.split(".")))


def _check_port(ip: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SCAN_TIMEOUT)
    try:
        result = sock.connect_ex((ip, port))
        return result == 0
    except OSError:
        return False
    finally:
        sock.close()


def identify_frameo(ip: str) -> dict | None:
    """Try to connect to the device via ADB and check if it's a Frameo frame.

    Returns a dict with frame info, or None if not a Frameo frame.
    """
    device = f"{ip}:{ADB_PORT}"

    # Connect
    if not _adb_connect(device):
        return None

    try:
        # Check if Frameo package is installed
        packages = _adb_shell(device, "pm", "list", "packages")
        if "net.frameo" not in packages:
            return None

        # Get model
        model = _adb_shell(device, "getprop", "ro.product.model").strip() or "unknown"

        # Get resolution
        resolution = (0, 0)
        size_output = _adb_shell(device, "wm", "size")
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", size_output)
        if match:
            resolution = (int(match.group(1)), int(match.group(2)))

        return {
            "ip": ip,
            "model": model,
            "resolution": resolution,
            "photo_path": "/sdcard/DCIM/",
        }
    except Exception as e:
        logger.debug("Failed to query %s: %s", ip, e)
        return None


def _adb_connect(device: str) -> bool:
    try:
        result = subprocess.run(
            ["adb", "connect", device],
            capture_output=True, text=True, timeout=ADB_CONNECT_TIMEOUT,
        )
        return "connected" in result.stdout.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _adb_shell(device: str, *args: str) -> str:
    result = subprocess.run(
        ["adb", "-s", device, "shell"] + list(args),
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Frameo Network Discovery")
    print("=" * 40)
    frames = find_frames()
    print()
    if not frames:
        print("No Frameo frames found.")
        return
    print(f"Found {len(frames)} Frameo frame(s):")
    for i, f in enumerate(frames, 1):
        w, h = f["resolution"]
        print(f"  {i}. {f['ip']}  model={f['model']}  resolution={w}x{h}")


if __name__ == "__main__":
    main()
