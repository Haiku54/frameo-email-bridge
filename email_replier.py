"""Hebrew reply-summary sender for Frameo email bridge.

After the bridge processes an email and pushes its attachments to the
photo frame, this module sends a friendly Hebrew reply back to the
sender summarising what arrived ("וואלה! עלו 3 תמונות למסגרת 🖼️ ...")
and explaining anything that didn't make it in family-readable terms.

The reply is sent over SMTP using the same Gmail credentials configured
for IMAP. Threading headers (In-Reply-To, References, Re:) make the
reply land inside the original conversation in Gmail.

Failures here never propagate up the call stack — the email→frame
pipeline must keep working even if SMTP is broken. See
`EmailReplier.send_summary_safe`.
"""

from __future__ import annotations

import dataclasses
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from email_monitor import EmailMonitor

logger = logging.getLogger(__name__)


class FailureReason(str, Enum):
    HEIC_UNSUPPORTED = "heic_unsupported"
    IMAGE_BAD_FORMAT = "image_bad_format"
    IMAGE_TOO_SMALL = "image_too_small"
    VIDEO_TOO_LONG = "video_too_long"
    VIDEO_FFMPEG_MISSING = "video_ffmpeg_missing"
    VIDEO_ENCODE_FAILED = "video_encode_failed"
    VIDEO_TIMEOUT = "video_timeout"
    FRAME_DISCONNECTED = "frame_disconnected"
    PUSH_FAILED = "push_failed"
    UNKNOWN = "unknown"


# Reasons that mean "tried but the frame is offline right now". For these,
# the bridge defers the reply to a later cycle so the family doesn't get a
# false "didn't make it" when in fact the file goes up 60 seconds later.
TRANSIENT_REASONS: frozenset[FailureReason] = frozenset({
    FailureReason.FRAME_DISCONNECTED,
})


@dataclasses.dataclass
class AttachmentOutcome:
    original_filename: str
    is_video: bool
    state: Literal["pending", "pushed", "failed"]
    reason: FailureReason | None = None
    detail: dict | None = None


@dataclasses.dataclass
class EmailOutcome:
    uid: str
    sender_addr: str          # bare addr, e.g. mom@gmail.com
    sender_display: str       # original "Name <addr>" From header
    subject: str
    message_id: str | None
    items: list[AttachmentOutcome]


# ──────────────────────────────────────────────────────────────────────
# Hebrew rendering — pure functions, easy to eyeball/test
# ──────────────────────────────────────────────────────────────────────

def render_subject(original_subject: str) -> str:
    """Prepend 'Re: ' if not already present (case-insensitive)."""
    s = (original_subject or "").strip()
    if not s:
        return "Re: (ללא נושא)"
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}"


def _images_phrase(n: int) -> str:
    """Hebrew noun + matching count for images (תמונה, feminine)."""
    if n == 1:
        return "תמונה אחת"
    if n == 2:
        return "שתי תמונות"
    return f"{n} תמונות"


def _videos_phrase(n: int) -> str:
    """Hebrew noun + matching count for videos (סרטון, masculine)."""
    if n == 1:
        return "סרטון אחד"
    if n == 2:
        return "שני סרטונים"
    return f"{n} סרטונים"


def _verb_uploaded(n_images: int, n_videos: int) -> str:
    """Pick the Hebrew past-tense 'uploaded' verb that agrees with the count.

    - 1 image only: "עלתה" (feminine singular)
    - 1 video only: "עלה" (masculine singular)
    - everything else (plural / mixed): "עלו" (plural, no gender)
    """
    if n_images == 1 and n_videos == 0:
        return "עלתה"
    if n_videos == 1 and n_images == 0:
        return "עלה"
    return "עלו"


def _items_summary(n_images: int, n_videos: int) -> str:
    """Build "{verb} X תמונות וY סרטונים" — gender-aware."""
    verb = _verb_uploaded(n_images, n_videos)
    parts: list[str] = []
    if n_images:
        parts.append(_images_phrase(n_images))
    if n_videos:
        parts.append(_videos_phrase(n_videos))
    nouns = " ו".join(parts)
    return f"{verb} {nouns}"


def _reason_to_hebrew(reason: FailureReason | None, detail: dict | None) -> str:
    detail = detail or {}
    match reason:
        case FailureReason.HEIC_UNSUPPORTED:
            return "קובץ HEIC לא נתמך"
        case FailureReason.IMAGE_BAD_FORMAT:
            return "לא הצלחתי לפתוח את התמונה"
        case FailureReason.IMAGE_TOO_SMALL:
            w = detail.get("width", "?")
            h = detail.get("height", "?")
            return f"התמונה קטנה מדי ({w}×{h})"
        case FailureReason.VIDEO_TOO_LONG:
            duration = detail.get("duration", "?")
            limit = detail.get("limit", "?")
            try:
                duration = f"{float(duration):.0f}"
            except (TypeError, ValueError):
                pass
            try:
                limit = f"{float(limit):.0f}"
            except (TypeError, ValueError):
                pass
            return f"הסרטון ארוך מדי ({duration} שניות, מותר עד {limit})"
        case FailureReason.VIDEO_FFMPEG_MISSING:
            return "עיבוד וידאו לא זמין כרגע"
        case FailureReason.VIDEO_ENCODE_FAILED:
            return "לא הצלחתי לעבד את הסרטון"
        case FailureReason.VIDEO_TIMEOUT:
            return "עיבוד הסרטון לקח יותר מדי זמן"
        case FailureReason.PUSH_FAILED:
            return "שליחה למסגרת נכשלה"
        case FailureReason.FRAME_DISCONNECTED:
            # Should never appear in a sent reply (we defer those), but
            # handle it defensively in case the resolver lets one through.
            return "המסגרת לא הייתה מחוברת — ננסה שוב"
        case _:
            return "שגיאה לא מזוהה"


def _failure_lines(items: list[AttachmentOutcome]) -> list[str]:
    return [
        f"• {it.original_filename} — {_reason_to_hebrew(it.reason, it.detail)}"
        for it in items
        if it.state == "failed"
    ]


def render_body_hebrew(outcome: EmailOutcome) -> str:
    """Build the full reply body. Always returns non-empty UTF-8 text."""
    pushed = [it for it in outcome.items if it.state == "pushed"]
    failed = [it for it in outcome.items if it.state == "failed"]

    n_img_ok = sum(1 for it in pushed if not it.is_video)
    n_vid_ok = sum(1 for it in pushed if it.is_video)
    n_img_total = sum(1 for it in outcome.items if not it.is_video)
    n_vid_total = sum(1 for it in outcome.items if it.is_video)

    signature = "מהמסגרת באהבה ❤️"

    # All success
    if not failed:
        summary = _items_summary(n_img_ok, n_vid_ok)
        return (
            f"וואלה! {summary} למסגרת 🥰\n"
            f"כבר על המסגרת 🖼️\n"
            f"\n"
            f"{signature}\n"
        )

    # All failed
    if not pushed:
        bullets = "\n".join(_failure_lines(failed))
        return (
            f"אוי, התמונות לא הצליחו לעלות 😞\n"
            f"\n"
            f"{bullets}\n"
            f"\n"
            f"תשלחי שוב? 🙏\n"
            f"\n"
            f"{signature}\n"
        )

    # Partial: some pushed, some failed
    summary = _items_summary(n_img_ok, n_vid_ok)
    bullets = "\n".join(_failure_lines(failed))
    return (
        f"תודה! {summary} למסגרת ✨\n"
        f"אבל היו דברים שלא הצליחו:\n"
        f"{bullets}\n"
        f"\n"
        f"{signature}\n"
    )


# ──────────────────────────────────────────────────────────────────────
# SMTP send
# ──────────────────────────────────────────────────────────────────────

class EmailReplier:
    def __init__(self, config: dict, monitor: EmailMonitor):
        email_cfg = config.get("email", {})
        self.username: str = email_cfg["username"]
        self.password: str = email_cfg["password"]
        self.host: str = email_cfg.get("smtp_server", "smtp.gmail.com")
        self.port: int = int(email_cfg.get("smtp_port", 587))
        self.from_name: str = email_cfg.get("smtp_from_name", "Frameo")
        self.timeout: int = int(email_cfg.get("smtp_timeout", 30))
        self.monitor = monitor

    def send_summary_safe(self, outcome: EmailOutcome) -> None:
        """Send the summary reply. Never raises.

        Skips silently if the email has no attachments or a reply was
        already sent for this UID.
        """
        try:
            if not outcome.items:
                return
            if self.monitor.is_reply_sent(outcome.uid):
                return
            self._send(outcome)
            self.monitor.mark_reply_sent(outcome.uid)
            logger.info(
                "Reply summary sent to %s (uid=%s)",
                _mask_email(outcome.sender_addr), outcome.uid,
            )
        except Exception as e:
            logger.warning(
                "Reply send failed for %s (uid=%s): %s",
                _mask_email(outcome.sender_addr), outcome.uid, e,
            )

    def _send(self, outcome: EmailOutcome) -> None:
        msg = EmailMessage()
        msg["Subject"] = render_subject(outcome.subject)
        msg["From"] = f'"{self.from_name}" <{self.username}>'
        msg["To"] = outcome.sender_display or outcome.sender_addr
        msg["Date"] = formatdate(localtime=True)
        # Domain for our own Message-ID — prefer username's domain.
        try:
            domain = self.username.split("@", 1)[1]
        except IndexError:
            domain = "frameo.local"
        msg["Message-ID"] = make_msgid(domain=domain)
        if outcome.message_id:
            msg["In-Reply-To"] = outcome.message_id
            msg["References"] = outcome.message_id
        else:
            logger.info(
                "No Message-ID for uid=%s — reply will not thread",
                outcome.uid,
            )
        msg.set_content(render_body_hebrew(outcome))

        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)


def _mask_email(addr: str) -> str:
    """Mask the local part of an email address for logging.

    Matches the style used elsewhere in the project (main.mask_email).
    Duplicated here to keep email_replier self-contained and avoid an
    import cycle between main and email_replier.
    """
    if not addr or "@" not in addr:
        return addr or ""
    local, _, domain = addr.partition("@")
    if len(local) <= 2:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"
