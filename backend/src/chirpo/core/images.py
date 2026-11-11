"""Which attachments chirpo reads, and how it checks them (D50).

Only JPEG, PNG and WebP. The claimed type only decides whether a file is worth downloading (a
PDF or an SVG is never fetched); what the file really is gets decided by its first bytes after
the download, and that is the type used from then on. SVG is text that can carry scripts, so it
never passes. chirpo never opens or renders an image: the bytes go to the model and are dropped.
"""

from dataclasses import dataclass

from chirpo.core.domain import Attachment, AttachmentKind
from chirpo.core.errors import UnsupportedImageError
from chirpo.core.ports import MessagingAdapter

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Image:
    mime_type: str  # what the bytes are, not what anyone claimed
    data: bytes


def can_read(attachment: Attachment) -> bool:
    """Cheap, before downloading: an image of an allowed type that isn't known to be too big."""
    return (
        attachment.kind is AttachmentKind.IMAGE
        and attachment.mime_type in ALLOWED_IMAGE_TYPES
        and (attachment.size is None or attachment.size <= MAX_IMAGE_BYTES)
    )


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def load_image(messaging: MessagingAdapter, attachment: Attachment) -> Image:
    """Downloads an attachment and checks what it really is.

    Raises `UnsupportedImageError`, or an `AttachmentError` when the download fails."""
    if not can_read(attachment):
        raise UnsupportedImageError("not an image chirpo reads")
    data = await messaging.download(attachment, max_bytes=MAX_IMAGE_BYTES)
    mime_type = sniff(data)
    if mime_type is None:
        raise UnsupportedImageError("the file isn't a JPEG, PNG or WebP")
    return Image(mime_type=mime_type, data=data)
