import asyncio

import pytest

from chirpo.core import messages, vision
from chirpo.core.domain import Attachment, AttachmentKind
from chirpo.core.errors import MessagingUnavailableError
from chirpo.core.llm import IMAGE_OWN, IMAGE_REPLIED
from tests.fakes import FakeMessaging, make_message

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
SVG = b"<svg><script>alert(1)</script></svg>"


def image(file_id: str, mime: str | None = "image/png", size: int | None = 1000) -> Attachment:
    return Attachment(kind=AttachmentKind.IMAGE, file_id=file_id, mime_type=mime, size=size)


async def gather(
    messaging: FakeMessaging,
    own: tuple[Attachment, ...] = (),
    replied: tuple[Attachment, ...] = (),
) -> vision.Seen:
    message = make_message("make an issue", attachments=own, reply_to_attachments=replied)
    return await vision.gather_images(messaging, message)


async def test_nothing_attached_means_nothing_to_do() -> None:
    messaging = FakeMessaging()
    seen = await gather(messaging)
    assert (seen.images, seen.notes) == ((), [])
    assert messaging.downloads == []


async def test_the_messages_own_image_is_downloaded_and_labelled() -> None:
    messaging = FakeMessaging()
    messaging.files["A"] = JPEG
    [seen] = (await gather(messaging, own=(image("A", "image/png"),))).images
    assert (seen.label, seen.mime_type, seen.data) == (IMAGE_OWN, "image/jpeg", JPEG)  # by content


async def test_the_replied_to_image_comes_after_the_messages_own() -> None:
    messaging = FakeMessaging()
    messaging.files |= {"A": PNG, "B": JPEG}
    seen = await gather(messaging, own=(image("A"),), replied=(image("B", "image/jpeg"),))
    assert [(i.label, i.data) for i in seen.images] == [(IMAGE_OWN, PNG), (IMAGE_REPLIED, JPEG)]


async def test_at_most_three_images_are_read() -> None:
    messaging = FakeMessaging()
    messaging.files |= {name: PNG for name in "ABCD"}
    seen = await gather(messaging, own=(image("A"), image("B")), replied=(image("C"), image("D")))
    assert len(seen.images) == vision.MAX_IMAGES == 3
    assert sorted(file for file, _ in messaging.downloads) == ["A", "B", "C"]
    assert seen.notes == [messages.IMAGES_LIMIT]


async def test_downloads_run_at_the_same_time() -> None:
    started: list[str] = []
    both_started = asyncio.Event()

    class Slow(FakeMessaging):
        async def download(self, attachment: Attachment, *, max_bytes: int) -> bytes:
            started.append(attachment.file_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)  # stuck if they ran one by one
            return PNG

    seen = await gather(Slow(), own=(image("A"),), replied=(image("B"),))
    assert len(seen.images) == 2


@pytest.mark.parametrize(
    ("attachment", "note"),
    [
        (image("S", "image/svg+xml"), messages.IMAGE_UNSUPPORTED),
        (image("G", "image/gif"), messages.IMAGE_UNSUPPORTED),
        (image("T", size=5_000_000), messages.IMAGE_TOO_BIG),
    ],
)
async def test_images_that_are_refused_up_front_are_not_downloaded(
    attachment: Attachment, note: str
) -> None:
    messaging = FakeMessaging()
    seen = await gather(messaging, own=(attachment,))
    assert (seen.images, seen.notes) == ((), [note])
    assert messaging.downloads == []


async def test_a_file_that_isnt_what_it_claims_is_refused_after_the_download() -> None:
    messaging = FakeMessaging()
    messaging.files["X"] = SVG
    seen = await gather(messaging, own=(image("X", "image/png"),))
    assert (seen.images, seen.notes) == ((), [messages.IMAGE_UNSUPPORTED])


async def test_a_file_that_grew_is_too_big() -> None:
    messaging = FakeMessaging()
    messaging.files["X"] = PNG + b"\x00" * (4 * 1024 * 1024)
    seen = await gather(messaging, own=(image("X", size=None),))
    assert seen.notes == [messages.IMAGE_TOO_BIG]


async def test_a_file_the_platform_no_longer_has() -> None:
    seen = await gather(FakeMessaging(), own=(image("gone"),))
    assert (seen.images, seen.notes) == ((), [messages.IMAGE_UNAVAILABLE])


async def test_a_platform_failure_never_breaks_the_request() -> None:
    class Down(FakeMessaging):
        async def download(self, attachment: Attachment, *, max_bytes: int) -> bytes:
            raise MessagingUnavailableError("down")

    seen = await gather(Down(), own=(image("A"),))
    assert (seen.images, seen.notes) == ((), [messages.IMAGE_UNAVAILABLE])


async def test_a_slow_download_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "DOWNLOAD_TIMEOUT", 0.05)

    class Stuck(FakeMessaging):
        async def download(self, attachment: Attachment, *, max_bytes: int) -> bytes:
            await asyncio.sleep(5)
            return PNG

    seen = await gather(Stuck(), own=(image("A"),))
    assert (seen.images, seen.notes) == ((), [messages.IMAGE_UNAVAILABLE])


async def test_one_bad_image_doesnt_cost_the_good_one() -> None:
    messaging = FakeMessaging()
    messaging.files["A"] = PNG
    seen = await gather(messaging, own=(image("A"), image("gone")))
    assert len(seen.images) == 1
    assert seen.notes == [messages.IMAGE_UNAVAILABLE]


async def test_files_and_voice_are_only_mentioned_once() -> None:
    messaging = FakeMessaging()
    pdf = Attachment(kind=AttachmentKind.FILE, file_id="P", mime_type="application/pdf", size=10)
    voice = Attachment(kind=AttachmentKind.VOICE, file_id="V", mime_type="audio/ogg", size=10)
    seen = await gather(messaging, own=(pdf, voice), replied=(pdf,))
    assert (seen.images, seen.notes) == ((), [messages.FILES_NOT_READ])
    assert messaging.downloads == []


async def test_the_same_problem_is_only_mentioned_once() -> None:
    seen = await gather(FakeMessaging(), own=(image("a"), image("b")))
    assert seen.notes == [messages.IMAGE_UNAVAILABLE]


async def test_any_failure_leaves_the_image_out_instead_of_failing_the_request() -> None:
    class Broken(FakeMessaging):
        async def download(self, attachment: Attachment, *, max_bytes: int) -> bytes:
            raise RuntimeError("the platform sent something odd")

    seen = await gather(Broken(), own=(image("A"),))
    assert (seen.images, seen.notes) == ((), [messages.IMAGE_UNAVAILABLE])
