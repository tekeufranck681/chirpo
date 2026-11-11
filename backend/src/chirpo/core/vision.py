"""The images a request shows the model (D50): the message's own and the one it replies to.

They are fetched while GitHub is read, in parallel and with a timeout, and nothing here fails the
request: an image that can't be used is left out and one line says why. The bytes live only as
long as the request.
"""

import asyncio
import logging
from dataclasses import dataclass

from chirpo.core import messages
from chirpo.core.domain import Attachment, AttachmentKind, InboundMessage
from chirpo.core.errors import AttachmentTooLargeError, UnsupportedImageError
from chirpo.core.images import MAX_IMAGE_BYTES, can_read, load_image
from chirpo.core.llm import IMAGE_OWN, IMAGE_RECENT, IMAGE_REPLIED, RequestImage
from chirpo.core.ports import MessagingAdapter

MAX_IMAGES = 3
DOWNLOAD_TIMEOUT = 8.0  # seconds, per image

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Seen:
    images: tuple[RequestImage, ...]
    notes: list[str]  # what was left out and why, for the reply


async def gather_images(messaging: MessagingAdapter, message: InboundMessage) -> Seen:
    wanted = [(IMAGE_OWN, a) for a in message.attachments]
    wanted += [(IMAGE_REPLIED, a) for a in message.reply_to_attachments]
    notes: list[str] = []
    candidates: list[tuple[str, Attachment]] = []
    for label, attachment in wanted:
        if attachment.kind is not AttachmentKind.IMAGE:
            notes.append(messages.FILES_NOT_READ)
        elif len(candidates) < MAX_IMAGES:
            candidates.append((label, attachment))
        else:
            notes.append(messages.IMAGES_LIMIT)
    loaded = await asyncio.gather(*(_load(messaging, label, a) for label, a in candidates))
    images = tuple(item for item in loaded if isinstance(item, RequestImage))
    notes += [item for item in loaded if isinstance(item, str)]
    return Seen(images, list(dict.fromkeys(notes)))  # each problem once


async def gather_recent(messaging: MessagingAdapter, attachment: Attachment) -> Seen:
    """The most recent image of the chat, which the model asked to see (D52)."""
    loaded = await _load(messaging, IMAGE_RECENT, attachment)
    if isinstance(loaded, RequestImage):
        return Seen((loaded,), [])
    return Seen((), [loaded])


async def _load(
    messaging: MessagingAdapter, label: str, attachment: Attachment
) -> RequestImage | str:
    """The image, or the line that says why it was left out."""
    if not can_read(attachment):
        too_big = attachment.size is not None and attachment.size > MAX_IMAGE_BYTES
        return messages.IMAGE_TOO_BIG if too_big else messages.IMAGE_UNSUPPORTED
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT):
            image = await load_image(messaging, attachment)
    except AttachmentTooLargeError:
        return messages.IMAGE_TOO_BIG
    except UnsupportedImageError:
        return messages.IMAGE_UNSUPPORTED
    except Exception as error:
        log.warning("image_unavailable error=%s", type(error).__name__)
        return messages.IMAGE_UNAVAILABLE
    return RequestImage(label=label, mime_type=image.mime_type, data=image.data)
