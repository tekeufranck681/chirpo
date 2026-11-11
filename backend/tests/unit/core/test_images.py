import pytest

from chirpo.core.domain import Attachment, AttachmentKind
from chirpo.core.errors import (
    AttachmentTooLargeError,
    AttachmentUnavailableError,
    UnsupportedImageError,
)
from chirpo.core.images import MAX_IMAGE_BYTES, can_read, load_image, sniff
from tests.fakes import FakeMessaging

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def image(
    file_id: str = "F1", mime: str | None = "image/png", size: int | None = 1000
) -> Attachment:
    return Attachment(kind=AttachmentKind.IMAGE, file_id=file_id, mime_type=mime, size=size)


@pytest.mark.parametrize(
    ("data", "mime"),
    [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")],
)
def test_images_are_recognised_by_their_first_bytes(data: bytes, mime: str) -> None:
    assert sniff(data) == mime


@pytest.mark.parametrize(
    "data",
    [
        SVG,
        b"<!DOCTYPE html><html><script>x</script></html>",
        b"GIF89a" + b"\x00" * 10,
        b"%PDF-1.7",
        b"RIFF\x24\x00\x00\x00WAVEfmt ",  # RIFF, but not WebP
        b"\x89PNG",  # cut short
        b"",
    ],
)
def test_everything_else_is_refused(data: bytes) -> None:
    assert sniff(data) is None


@pytest.mark.parametrize(
    ("attachment", "readable"),
    [
        (image(mime="image/jpeg"), True),
        (image(mime="image/png"), True),
        (image(mime="image/webp"), True),
        (image(mime="image/svg+xml"), False),
        (image(mime="image/gif"), False),
        (image(mime=None), False),
        (image(size=None), True),  # Telegram sometimes leaves the size out: checked on download
        (image(size=MAX_IMAGE_BYTES), True),
        (image(size=MAX_IMAGE_BYTES + 1), False),
        (Attachment(kind=AttachmentKind.FILE, file_id="F", mime_type="image/png", size=10), False),
        (Attachment(kind=AttachmentKind.VOICE, file_id="V", mime_type="audio/ogg", size=10), False),
    ],
)
def test_which_attachments_may_be_read(attachment: Attachment, readable: bool) -> None:
    assert can_read(attachment) is readable


async def test_an_image_is_downloaded_and_typed_by_its_content() -> None:
    messaging = FakeMessaging()
    messaging.files["F1"] = JPEG
    loaded = await load_image(messaging, image(mime="image/png"))  # the claim is wrong
    assert (loaded.mime_type, loaded.data) == ("image/jpeg", JPEG)
    assert messaging.downloads == [("F1", MAX_IMAGE_BYTES)]


async def test_a_file_that_isnt_an_image_is_refused_whatever_it_claims() -> None:
    messaging = FakeMessaging()
    messaging.files["F1"] = SVG
    with pytest.raises(UnsupportedImageError):
        await load_image(messaging, image(mime="image/png"))


async def test_an_unreadable_attachment_is_not_even_downloaded() -> None:
    messaging = FakeMessaging()
    messaging.files["F1"] = SVG
    with pytest.raises(UnsupportedImageError):
        await load_image(messaging, image(mime="image/svg+xml"))
    assert messaging.downloads == []


async def test_download_problems_are_passed_on() -> None:
    messaging = FakeMessaging()
    with pytest.raises(AttachmentUnavailableError):
        await load_image(messaging, image("gone"))
    messaging.files["big"] = PNG + b"\x00" * MAX_IMAGE_BYTES
    with pytest.raises(AttachmentTooLargeError):
        await load_image(messaging, image("big", size=None))


@pytest.mark.parametrize("fields", [{"size": -1}, {"file_id": ""}])
def test_an_attachment_needs_an_id_and_a_sane_size(fields: dict[str, object]) -> None:
    values: dict[str, object] = {"kind": AttachmentKind.IMAGE, "file_id": "F1"} | fields
    with pytest.raises(ValueError):
        Attachment(**values)  # type: ignore[arg-type]
