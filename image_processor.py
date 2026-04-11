"""Image processing pipeline for Frameo email bridge.

Handles resize, format conversion, EXIF rotation, metadata stripping,
and file size optimization for pushing photos to a Frameo digital frame.
"""

import logging
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
HEIC_EXTENSIONS = {".heic", ".heif"}
MIN_DIMENSION = 100  # Skip tiny images (tracking pixels, signatures)


class ImageProcessingError(Exception):
    pass


class HeicNotSupportedError(ImageProcessingError):
    pass


def process_image(input_path: Path, output_path: Path, config: dict) -> Path:
    """Process a single image: convert, rotate, resize, strip metadata, optimize size.

    Returns the output path on success. Raises ImageProcessingError on failure.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    suffix = input_path.suffix.lower()

    logger.info("Processing image: %s", input_path.name)

    try:
        if suffix in HEIC_EXTENSIONS:
            img = _open_heic(input_path)
        else:
            img = Image.open(input_path)
            img.load()
    except HeicNotSupportedError:
        raise
    except (UnidentifiedImageError, OSError) as e:
        raise ImageProcessingError(f"Cannot open image {input_path.name}: {e}") from e

    # Skip tiny images
    if img.width < MIN_DIMENSION or img.height < MIN_DIMENSION:
        raise ImageProcessingError(
            f"Image too small ({img.width}x{img.height}), skipping"
        )

    # For animated images, use first frame
    if hasattr(img, "n_frames") and img.n_frames > 1:
        img.seek(0)
        logger.debug("Animated image, using first frame")

    # Auto-rotate based on EXIF orientation
    img = ImageOps.exif_transpose(img)

    # Resize to fit frame resolution
    max_w = config.get("resolution_width", 800)
    max_h = config.get("resolution_height", 480)
    orig_w, orig_h = img.width, img.height
    img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    logger.debug("Resized %dx%d -> %dx%d", orig_w, orig_h, img.width, img.height)

    # Sharpen after resize to recover detail lost during downscaling.
    # UnsharpMask(radius, percent, threshold):
    #   radius=1  — small halo, tight sharpening good for text and edges
    #   percent=120 — moderate strength (100=subtle, 150=strong)
    #   threshold=3 — ignore noise in smooth areas
    img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=3))

    # Convert RGBA/P to RGB (JPEG doesn't support transparency)
    if img.mode in ("RGBA", "P", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Note: metadata (EXIF/IPTC/XMP) stripping happens implicitly during
    # JPEG save — Pillow does not propagate EXIF unless you explicitly pass
    # `exif=` to save(). We previously copied pixels into a fresh image to
    # strip metadata, but that allocates ~120 MB of Python objects per image
    # on a 1280x800 source, causing OOM on Raspberry Pi.

    # Save with size optimization
    max_bytes = int(config.get("max_file_size_mb", 2) * 1024 * 1024)
    initial_quality = config.get("jpeg_quality", 85)
    _save_within_size_limit(img, output_path, initial_quality, max_bytes)

    logger.info(
        "Processed: %s -> %s (%d KB)",
        input_path.name,
        output_path.name,
        output_path.stat().st_size // 1024,
    )
    return output_path


def _open_heic(input_path: Path) -> Image.Image:
    """Open a HEIC/HEIF file using pillow-heif."""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        img = Image.open(input_path)
        img.load()
        return img
    except ImportError:
        raise HeicNotSupportedError(
            "HEIC support requires pillow-heif. Install with: pip install pillow-heif"
        )


def _save_within_size_limit(
    img: Image.Image, output_path: Path, initial_quality: int, max_bytes: int
) -> None:
    """Save as JPEG, reducing quality iteratively if file exceeds max size."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Clamp to a safe range so the loop always runs at least once even if
    # the user configured jpeg_quality outside 30-100.
    quality = max(30, min(100, int(initial_quality)))

    buf = BytesIO()
    size = 0
    while quality >= 30:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        size = buf.tell()

        if size <= max_bytes:
            output_path.write_bytes(buf.getvalue())
            return

        logger.debug("Size %d KB at quality %d, reducing...", size // 1024, quality)
        quality -= 5

    # Last resort: save at quality 30 even if over limit
    logger.warning(
        "Image %s still %d KB at quality 30, saving anyway",
        output_path.name,
        size // 1024,
    )
    output_path.write_bytes(buf.getvalue())
