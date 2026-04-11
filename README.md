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

The setup script will:
1. Create a Python virtual environment
2. Install dependencies
3. Ask for your Gmail credentials, subject filter, video settings
4. Auto-discover your Frameo frame (USB or network scan)
5. Save everything to `config.yaml`
6. Optionally send a test image

Then to run the service:

```bash
source .venv/bin/activate
python main.py
```

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

## Static IP / DHCP Reservation (IMPORTANT)

By default, routers assign IP addresses dynamically (DHCP). Your frame's IP might change after a reboot or power outage, which would break the service. **Strongly recommended**: set a static IP / DHCP reservation in your router.

### How to do it

Log into your router (usually `http://192.168.1.1` or `http://192.168.1.254`) and find the DHCP settings:

| Router brand | Path |
|-------------|------|
| **Asus** | LAN → DHCP Server → Manually Assigned IP |
| **TP-Link** | DHCP → Address Reservation |
| **D-Link** | Setup → Network Settings → Add DHCP Reservation |
| **Netgear** | LAN Setup → Address Reservation |
| **Bezeq / HOT** | Advanced → DHCP → Static IP Assignment |
| **Huawei** | LAN → LAN Settings → DHCP Static Address |

Steps:
1. Find the Frameo frame in the list of connected devices (usually `K1003T` or `Frameo`)
2. Copy its MAC address
3. Assign it a static IP (e.g. `192.168.1.200`)
4. Save and reboot the frame

After this, the frame always gets the same IP and you never need to re-run configure.py because of an IP change.

**Find the MAC address** with:
```bash
adb -s <frame-ip>:5555 shell cat /sys/class/net/wlan0/address
```

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

No Python or dependencies needed on the host (other than Docker + ADB for the first setup).

```bash
# 1. Run configure.py once on the host to generate config.yaml
# 2. Build and run:
docker compose up -d
```

**Useful commands:**

| Command | What it does |
|---------|-------------|
| `docker compose logs -f` | Watch logs |
| `docker compose down` | Stop |
| `docker compose up -d --build` | Rebuild and restart |
| `docker compose restart` | Restart |

> `network_mode: host` in docker-compose.yml is required so the container can reach the frame's ADB port on your LAN.

---

## Configuration Reference

All settings live in `config.yaml`. See `config.yaml.example` for the full template.

| Setting | Default | Description |
|---------|---------|-------------|
| `email.imap_server` | `imap.gmail.com` | IMAP server address |
| `email.poll_interval_seconds` | `120` | How often to check for new emails |
| `email.subject_filter` | `""` | Only emails with this subject are processed (empty = all) |
| `email.allowed_senders` | `[]` | Sender whitelist (empty = all) |
| `frame.adb_ip` | set by configure.py | Frame's WiFi IP |
| `frame.photo_path` | `/sdcard/DCIM/` | Where photos go on the frame |
| `frame.resolution_width` | auto-detected | Horizontal resolution |
| `frame.resolution_height` | auto-detected | Vertical resolution |
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
├── LICENSE                  # MIT
├── inbox/                   # Downloaded attachments (transient)
├── processed/               # Processed files awaiting push (transient)
├── archive/                 # Successfully pushed files
└── logs/                    # Log files
```

---

## License

MIT. See [LICENSE](LICENSE).
