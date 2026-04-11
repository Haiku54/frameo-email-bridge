# Frameo Email Bridge

Send photos and videos to your Frameo digital photo frame by email. The service monitors a Gmail inbox and automatically pushes matching attachments to your frame over WiFi.

## How It Works

```
Email (photo/video) → Gmail → Poll IMAP → Subject filter → Download
→ Resize/trim/optimize → Push to frame via ADB WiFi → Appears on frame
```

> **The frame does NOT need a USB cable during normal operation.** USB is only used once for the initial setup.
> After that, everything works over WiFi. The frame and the service just need to be on the same WiFi network.

## Features

- **Photos**: JPEG, PNG, HEIC/HEIF, BMP, GIF, TIFF, WEBP — auto-resized, EXIF-rotated, metadata-stripped
- **Videos**: MP4, MOV, M4V, AVI, MKV, WEBM, 3GP — trimmed to configurable max duration, re-encoded for frame compatibility
- **Subject filter**: only process emails with a specific subject line (e.g. "Frameo")
- **Sender whitelist**: restrict to specific email addresses
- **Auto-discovery**: finds your frame on the network via port scan (after first USB setup)
- **Self-healing IP**: if the frame's IP changes (DHCP lease renewal, router reboot, Android MAC randomization), the service automatically rediscovers it on the next push attempt and updates `config.yaml` — no manual intervention needed
- **Interactive setup**: one command, answers questions, configures everything
- **Never crashes**: network errors, bad images, frame offline — all handled gracefully
- **Docker support**: run as a container with `docker compose up -d`

---

## Quick Start

```bash
git clone https://github.com/Haiku54/frameo-email-bridge.git
cd frameo-email-bridge
bash setup.sh      # interactive — asks for everything
```

### What `bash setup.sh` does

1. Verifies Python 3.10+, `adb`, and `ffmpeg` are installed
2. Creates a Python virtual environment at `.venv/`
3. Installs the Python dependencies from `requirements.txt`
4. Creates runtime directories (`inbox/`, `processed/`, `archive/`, `logs/`, `data/`)
5. Copies `config.yaml.example` → `config.yaml` if missing
6. **Automatically launches `configure.py`** — the interactive wizard

### What `configure.py` will ask you

- Your Gmail address and App Password (input hidden)
- A subject filter keyword (e.g. `Frameo`) — leave blank to accept all
- An allowed-senders whitelist — blank to accept all
- Whether to accept video attachments + maximum video duration
- Confirmation to auto-discover your Frameo frame
- Confirmation to push a test image

It then detects the frame (USB first, network scan second) and saves everything to `config.yaml`.

### Running the service after setup

```bash
source .venv/bin/activate
python main.py
```

### Re-running configuration later

To change your Gmail password, subject filter, frame IP, or any other setting, run the configuration wizard again:

```bash
.venv/bin/python configure.py
```

It will read your existing `config.yaml` and let you accept each current value by pressing Enter, or type a new one.

---

## Prerequisites

- **Python 3.10+**
- **ADB** (Android Debug Bridge) — required for the initial frame setup
- **ffmpeg** — required only if you want video support
- **Gmail account with App Password** (regular passwords won't work)
- **Frameo frame** on the same WiFi network as the service host
- **USB cable** for the one-time initial setup

### Installing system dependencies

| OS | Commands |
|----|----------|
| Ubuntu / Debian / Raspberry Pi | `sudo apt install python3 python3-venv adb ffmpeg` |
| Fedora / RHEL | `sudo dnf install python3 android-tools ffmpeg` |
| macOS (Homebrew) | `brew install python android-platform-tools ffmpeg` |
| Arch Linux | `sudo pacman -S python android-tools ffmpeg` |

---

## Create a Gmail App Password

Gmail requires an App Password for IMAP access. Regular passwords will NOT work.

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already enabled
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Select app: **Mail**, device: **Other** (type "Frameo")
5. Click **Generate**
6. Copy the 16-character password (e.g., `abcd efgh ijkl mnop`)
7. Paste it when `configure.py` asks for the password

---

## How the Service Finds Your Frame

The setup script tries three methods in order:

1. **USB-connected frame** (first-time setup)
   - Runs `adb tcpip 5555` to enable WiFi ADB
   - Gets the frame's IP via `adb shell ip route`
   - Reconnects over WiFi
2. **Network scan** (if no USB device)
   - Scans your local subnet (e.g. `192.168.1.0/24`) for ADB port 5555
   - For each hit, connects and checks if Frameo is installed
   - Works on any machine on the same WiFi after the first-time USB setup
3. **Manual IP entry** (fallback)
   - You enter the IP yourself

**Tip**: once you've run the USB setup once, any other computer on the same network can run `python configure.py` and it will find the frame without USB.

---

## Handling IP Changes

Routers assign IP addresses dynamically (DHCP). On Android 10+ frames, **MAC randomization** means the frame can get a new MAC — and therefore a new IP — every time it reconnects to WiFi, even if you set a DHCP reservation in your router.

### Automatic recovery (built in)

The service handles this automatically:

1. When `adb push` fails 3 times in a row (because the frame's IP changed), the service runs `discover_frame.py` internally
2. The network scanner finds the frame by looking for ADB port 5555 + the Frameo package (`net.frameo.frame`) — it does not care about MAC or IP
3. If found at a new IP, `config.yaml` is updated in place and the push is retried
4. Rediscovery is throttled to once per minute so a long offline period doesn't spam the network with scans

You can verify this is working by watching `logs/frameo_bridge.log` — you'll see messages like:

```
WARNING: Push attempt 3/3 failed for photo.jpg: Cannot connect to 192.168.1.8:5555
INFO: Attempting to rediscover Frameo frame on the network...
INFO: Frame IP changed: 192.168.1.8 -> 192.168.1.14. Updating config.
INFO: Saved updated frame IP to config.yaml
INFO: Pushed to frame: photo.jpg
```

**This means you don't strictly need a static IP.** The service will adapt automatically.

### Static IP / DHCP Reservation (optional)

If you still want a fixed IP (faster startup, no rediscovery delay), you can set a DHCP reservation in your router. Note that this only works reliably if you also disable MAC randomization on the frame (see "Step 1" below), otherwise the MAC will keep changing and the reservation will silently stop matching.

### Step 1: Find the correct MAC address

> **WARNING — Android MAC randomization:** Most Frameo frames run Android 10+, which uses a **randomized MAC per WiFi network** by default (for privacy). This means the MAC address that the router sees is **NOT** the hardware MAC printed on the frame or reported by `cat /sys/class/net/wlan0/address`. If you use the wrong MAC in the reservation, the router will simply ignore it.
>
> The MAC you need is whatever **the router actually sees** for this frame on this network.

The most reliable method — **read it directly from the router**:

1. Log into your router's admin page (usually `http://192.168.1.1` or `http://192.168.1.254`)
2. Find the page showing connected clients / DHCP leases (names vary: "Device list", "Connected devices", "DHCP Clients", "Attached devices")
3. Look for a device whose host name contains `android-...` or whose IP matches the one the service is currently using
4. Copy the MAC address shown in that row — this is the MAC you need

Alternative method — ADB (useful for cross-checking):

```bash
adb -s <frame-ip>:5555 shell ip link show wlan0
```

Look for the line starting with `link/ether XX:XX:XX:XX:XX:XX` — **this may or may not match what the router sees**, depending on the frame's MAC-randomization setting. If the two don't match, **trust the router's view**.

Do NOT use the output of `cat /sys/class/net/wlan0/address` — on some Android versions this returns the hardware MAC, not the effective MAC used on the network.

### Step 2: Create the reservation

Find the DHCP reservation page in your router admin:

| Router brand | Path |
|-------------|------|
| **Asus** | LAN → DHCP Server → Manually Assigned IP |
| **TP-Link** | DHCP → Address Reservation |
| **D-Link** | Setup → Network Settings → Add DHCP Reservation |
| **Netgear** | LAN Setup → Address Reservation |
| **Bezeq / HOT / Partner / Cellcom** | Advanced → DHCP → Static IP Assignment |
| **Huawei** | LAN → LAN Settings → DHCP Static Address |

Steps:
1. Enter the MAC address from Step 1
2. Assign a static IP outside the DHCP pool, or any unused IP (e.g. `192.168.1.200`)
3. Save the reservation

### Step 3: Force the frame to pick up the new reservation

The router will only apply the reservation on the **next DHCP lease request**. The current lease is still valid, so nothing changes until you do one of these:

- **Easiest:** delete the current DHCP lease for the frame from the router's "Connected devices" / "DHCP client list" page (most routers have a "Release" or "Delete" button on each row)
- **Or:** reboot the frame (power cycle or `adb -s <frame-ip>:5555 reboot`)
- **Or:** toggle WiFi off and on in the frame's Settings → WiFi screen
- **Or:** just wait — the lease will expire after a few hours and the frame will renew with the new IP

### Step 4: Verify

```bash
source .venv/bin/activate
python discover_frame.py
```

The scanner should now find the frame at the IP you reserved. If the IP matches what you set in the router, re-run `python configure.py` — it will auto-update `config.yaml` with the new IP.

### If the reservation doesn't work

- **MAC mismatch:** this is the #1 issue. Double-check that the MAC you entered in the router exactly matches what the router sees in its own client list (including case and separators — some routers want `AA:BB:CC:DD:EE:FF`, others `AA-BB-CC-DD-EE-FF`).
- **MAC randomization keeps changing:** if the MAC the router sees changes each time the frame reconnects, go into the frame's WiFi settings and change **MAC type / Privacy → "Use device MAC"** (not "Randomized MAC") for that specific network. Then re-check the MAC in the router and update the reservation if it changed.
- **Lease not released:** if the reservation is correct but the frame still has the old IP, the old lease is still held. Delete it manually in the router.

---

## Subject Filter

To avoid processing every email in your inbox, set a **subject filter**. Only emails whose subject contains this substring (case-insensitive) will be processed.

In `config.yaml`:
```yaml
email:
  subject_filter: "Frameo"
```

Examples:
- Subject `Frameo` → matches
- Subject `For Frameo` → matches
- Subject `Beach vacation pics` → ignored

Leave `subject_filter` empty to accept all subjects.

---

## Video Support

Videos are supported via **ffmpeg**. The service automatically:
1. Reads the video duration
2. If it's longer than `max_video_duration_seconds`, trims it
3. Scales it to fit the frame resolution (preserving aspect ratio)
4. Re-encodes as H.264 + AAC (Frameo-compatible)
5. Pushes it to the frame

> **WARNING**: Different Frameo models have different maximum video durations.
> - Older models: 10 seconds
> - Newer models: 15 seconds
>
> Start with 10 seconds if unsure. You can change the limit in `config.yaml`:
> ```yaml
> processing:
>   max_video_duration_seconds: 10
> ```

Supported input formats: `.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`, `.3gp`.

---

## Running the Service

There are 3 ways to run the service. Pick the one that fits your needs.

### Option A: Manual (for testing)

```bash
cd ~/frameo-email-bridge
source .venv/bin/activate
python main.py
```

Press `Ctrl+C` to stop. Logs appear in the terminal and in `logs/frameo_bridge.log`.

### Option B: systemd service (recommended for 24/7 operation)

Runs in the background, starts on boot, restarts on crashes. Best for Raspberry Pi or any Linux host.

**Step 1** — Create the service file (replace `YOUR_USERNAME` and the path with yours):

```bash
sudo nano /etc/systemd/system/frameo-bridge.service
```

```ini
[Unit]
Description=Frameo Email Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/frameo-email-bridge
ExecStart=/home/YOUR_USERNAME/frameo-email-bridge/.venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

**Step 2** — Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable frameo-bridge   # auto-start on boot
sudo systemctl start frameo-bridge    # start now
```

**Useful commands:**

| Command | What it does |
|---------|-------------|
| `sudo systemctl status frameo-bridge` | Check if running |
| `sudo journalctl -u frameo-bridge -f` | Watch logs in real-time |
| `sudo systemctl stop frameo-bridge` | Stop |
| `sudo systemctl restart frameo-bridge` | Restart |
| `sudo systemctl disable frameo-bridge` | Don't start on boot |

### Option C: Docker

Runs the service in a container. No Python dependencies on the host — but you still need `adb` and the interactive setup script to generate `config.yaml` before first launch.

**Requirements on the host:**
- Docker + Docker Compose
- `adb` (needed once, for initial frame setup by `configure.py`)
- `python3` + `python3-venv` (needed once, to run `configure.py`)

**Step 1 — Generate `config.yaml` on the host:**

```bash
git clone https://github.com/Haiku54/frameo-email-bridge.git
cd frameo-email-bridge
bash setup.sh    # creates config.yaml via the interactive wizard
```

This creates all runtime directories and writes `config.yaml` with your Gmail credentials and the discovered frame IP.

**Step 2 — Build and launch the container:**

```bash
docker compose up -d
```

If you skip Step 1 and try to run `docker compose up` on a fresh clone, the container will fail fast with a clear error — Docker would otherwise create `./config.yaml` as an empty directory, which is not recoverable without manual cleanup.

**Useful commands:**

| Command | What it does |
|---------|-------------|
| `docker compose logs -f` | Watch logs in real-time |
| `docker compose down` | Stop and remove the container |
| `docker compose up -d --build` | Rebuild image and restart |
| `docker compose restart` | Restart the container |

**How state is persisted:**

The compose file bind-mounts every runtime directory to the host so nothing is lost on container restart:
- `./config.yaml` (read-only)
- `./inbox/`, `./processed/` — in-flight attachments (retry queue)
- `./archive/` — successfully pushed files
- `./logs/` — rotating log files
- `./data/` — SQLite DB of processed email UIDs

> `network_mode: host` is required in `docker-compose.yml` so the container can reach the Frameo frame's ADB port on your LAN.

---

## Configuration Reference

All settings live in `config.yaml`. See `config.yaml.example` for the full template.

| Setting | Default | Description |
|---------|---------|-------------|
| `email.imap_server` | `imap.gmail.com` | IMAP server address |
| `email.imap_port` | `993` | IMAP port (SSL) |
| `email.poll_interval_seconds` | `120` | How often to check for new emails |
| `email.subject_filter` | `""` | Only emails with this subject are processed (empty = all) |
| `email.allowed_senders` | `[]` | Sender whitelist (empty = all) |
| `frame.adb_ip` | set by configure.py | Frame's WiFi IP |
| `frame.adb_port` | `5555` | ADB TCP port on the frame |
| `frame.photo_path` | `/sdcard/DCIM/` | Where photos go on the frame |
| `frame.resolution_width` | auto-detected | Horizontal resolution |
| `frame.resolution_height` | auto-detected | Vertical resolution |
| `frame.push_timeout` | `60` | Seconds to wait for `adb push` (increase for large videos) |
| `processing.max_file_size_mb` | `2` | Max JPEG file size |
| `processing.jpeg_quality` | `95` | JPEG quality (30-100) |
| `processing.strip_exif` | `true` | Remove photo metadata |
| `processing.convert_heic` | `true` | Convert HEIC/HEIF to JPEG |
| `processing.accept_videos` | `true` | Process video attachments |
| `processing.max_video_duration_seconds` | `10` | Trim videos longer than this |
| `processing.video_max_file_size_mb` | `20` | Max output video size |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `adb devices` shows empty | Check USB cable, try a different port, enable USB debugging on the frame |
| `adb devices` shows "unauthorized" | Look for a popup on the frame's screen and tap "Allow USB debugging" |
| Network scan finds nothing | WiFi ADB may have dropped. Reconnect USB and re-run `python configure.py` |
| Frame IP changed after reboot | Set a static IP / DHCP reservation in your router (see above) |
| IMAP login failed | You need a Gmail App Password, not your regular password |
| `"Less secure apps"` error | Google removed this option. Use App Passwords instead |
| Photos don't appear on frame | Make sure `photo_path` is `/sdcard/DCIM/` (NOT `/sdcard/Frameo/`) |
| Photos look blurry | Check that `resolution_width`/`resolution_height` match your frame's actual resolution |
| Videos don't play | Frame's max duration may be lower than your setting. Reduce `max_video_duration_seconds` |
| `ffmpeg not found` | Install with `sudo apt install ffmpeg`, or set `accept_videos: false` |
| HEIC files not converted | `pillow-heif` should be installed automatically. Re-run `setup.sh` |
| Service crashes on Raspberry Pi | Check `logs/frameo_bridge.log`. Ensure stable WiFi connection |

---

## Project Structure

```
frameo-email-bridge/
├── main.py                  # Service entry point
├── configure.py             # Interactive setup (first-time + reconfig)
├── email_monitor.py         # Gmail IMAP polling + subject filter
├── image_processor.py       # Photo resize/convert/optimize
├── video_processor.py       # Video trim/resize/re-encode (ffmpeg)
├── frame_pusher.py          # ADB push to frame
├── discover_frame.py        # Network scan for Frameo frames
├── adb_setup.py             # Low-level USB→WiFi ADB setup helpers
├── setup.sh                 # Bootstrap script (venv + deps + configure)
├── config.yaml.example      # Template configuration
├── requirements.txt         # Python dependencies
├── Dockerfile               # Docker image definition
├── docker-compose.yml       # Docker Compose config
├── docker-entrypoint.sh     # Validates config.yaml before starting the container
├── LICENSE                  # MIT
├── inbox/                   # Downloaded attachments (transient)
├── processed/               # Processed files awaiting push (transient)
├── archive/                 # Successfully pushed files
└── logs/                    # Log files
```

---

## License

MIT. See [LICENSE](LICENSE).
