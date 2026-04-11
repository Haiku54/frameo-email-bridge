"""Video processing pipeline for Frameo email bridge.

Handles duration trimming, resolution scaling, and re-encoding videos
for pushing to a Frameo digital frame. Uses ffmpeg via subprocess.
"""

import json
import logging
import platform
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}


class VideoProcessingError(Exception):
    pass


class FfmpegNotInstalledError(VideoProcessingError):
    pass


def is_video_file(path: Path) -> bool:
    """Check if a file has a video extension."""
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def process_video(input_path: Path, output_path: Path, config: dict) -> Path:
    """Process a single video: trim to max duration, scale, re-encode as H.264/AAC.

    Returns the output path on success. Raises VideoProcessingError on failure.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    _ensure_ffmpeg_installed()

    logger.info("Processing video: %s", input_path.name)

    duration = get_video_duration(input_path)
    logger.debug("Video duration: %.1fs", duration)

    max_duration = float(config.get("max_video_duration_seconds", 10))
    max_w = int(config.get("resolution_width", 1280))
    max_h = int(config.get("resolution_height", 800))
    max_bytes = int(config.get("video_max_file_size_mb", 20) * 1024 * 1024)

    trim_seconds = min(duration, max_duration)
    if duration > max_duration:
        logger.info(
            "Video is %.1fs, trimming to %.1fs (max_video_duration_seconds)",
            duration, max_duration,
        )

    # Try encoding with CRF 23 first; if too large, retry with higher CRF (lower quality)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for crf in (23, 26, 29, 32):
        _encode_video(
            input_path=input_path,
            output_path=output_path,
            trim_seconds=trim_seconds,
            max_w=max_w,
            max_h=max_h,
            crf=crf,
        )
        size = output_path.stat().st_size
        if size <= max_bytes:
            logger.info(
                "Processed video: %s -> %s (%d KB, crf=%d)",
                input_path.name, output_path.name, size // 1024, crf,
            )
            return output_path
        logger.debug("Size %d KB at crf %d, re-encoding...", size // 1024, crf)

    logger.warning(
        "Video %s still %d KB at crf 32, keeping anyway",
        output_path.name, output_path.stat().st_size // 1024,
    )
    return output_path


def get_video_duration(path: Path) -> float:
    """Return video duration in seconds, via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise FfmpegNotInstalledError(_ffmpeg_install_hint())
    except subprocess.TimeoutExpired:
        raise VideoProcessingError(f"ffprobe timed out on {path.name}")

    if result.returncode != 0:
        raise VideoProcessingError(
            f"ffprobe failed for {path.name}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        raise VideoProcessingError(f"Could not parse duration from ffprobe: {e}")


def _encode_video(
    input_path: Path,
    output_path: Path,
    trim_seconds: float,
    max_w: int,
    max_h: int,
    crf: int,
) -> None:
    """Encode video with H.264/AAC, scaled to fit within max_w x max_h, trimmed to trim_seconds."""
    # Scale filter: fit within max_w x max_h, preserve aspect ratio, ensure even dimensions
    scale_filter = (
        f"scale='if(gt(a,{max_w}/{max_h}),min({max_w},iw),-2)':"
        f"'if(gt(a,{max_w}/{max_h}),-2,min({max_h},ih))'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-t", f"{trim_seconds:.2f}",
        "-vf", scale_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",    # Maximum compatibility
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",  # Allow streaming playback
        "-loglevel", "error",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise FfmpegNotInstalledError(_ffmpeg_install_hint())
    except subprocess.TimeoutExpired:
        raise VideoProcessingError(f"ffmpeg timed out encoding {input_path.name}")

    if result.returncode != 0:
        raise VideoProcessingError(
            f"ffmpeg failed for {input_path.name}: {result.stderr.strip()}"
        )


def _ensure_ffmpeg_installed() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FfmpegNotInstalledError(_ffmpeg_install_hint())


def _ffmpeg_install_hint() -> str:
    os_name = platform.system()
    if os_name == "Linux":
        return "ffmpeg not found. Install with: sudo apt install ffmpeg"
    if os_name == "Darwin":
        return "ffmpeg not found. Install with: brew install ffmpeg"
    if os_name == "Windows":
        return "ffmpeg not found. Download from https://ffmpeg.org/download.html"
    return "ffmpeg not found. See https://ffmpeg.org/download.html"
