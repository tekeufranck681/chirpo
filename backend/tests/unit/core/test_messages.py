import pytest

from chirpo.core import messages


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1, "a minute"),
        (60, "a minute"),
        (61, "2 minutes"),
        (12 * 60 + 30, "13 minutes"),
        (59 * 60, "59 minutes"),
        (3600, "1 hour"),
        (3601, "2 hours"),
        (12 * 3600, "12 hours"),
    ],
)
def test_waits_are_said_in_plain_words(seconds: int, text: str) -> None:
    assert messages.duration(seconds) == text
