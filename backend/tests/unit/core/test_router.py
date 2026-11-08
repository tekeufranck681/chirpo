import pytest

from chirpo.core.router import Command, is_explicitly_addressed, parse_command
from tests.fakes import make_message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ping", Command("ping", "")),
        ("/PING", Command("ping", "")),
        ("/help me please", Command("help", "me please")),
        ("  /ping  ", Command("ping", "")),
        ("/ping\nsecond line", Command("ping", "second line")),
        ("/ping\targ", Command("ping", "arg")),
        ("/ping\N{NO-BREAK SPACE}arg", Command("ping", "arg")),
    ],
)
def test_parse_command(text: str, expected: Command) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text", ["ping", "hello /ping", "/", "/bad-name", "/" + "x" * 33, "", "   "]
)
def test_not_a_command(text: str) -> None:
    assert parse_command(text) is None


def test_plain_group_message_is_not_explicitly_addressed() -> None:
    assert not is_explicitly_addressed(make_message("lunch at 1?"))


@pytest.mark.parametrize("flag", ["mentions_bot", "replies_to_bot", "is_private_chat"])
def test_mentions_replies_and_private_chats_are_explicit(flag: str) -> None:
    assert is_explicitly_addressed(make_message("hello", **{flag: True}))
