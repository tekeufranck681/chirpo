"""Model-written text on its way to GitHub: it must not ping people, close issues or carry links
that hide where they go (D51, M5.2)."""

import re

import pytest

from chirpo.core.outbound import neutralise

ZWSP = "​"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ask @annie-gh to look", f"ask @{ZWSP}annie-gh to look"),
        ("@annie first", f"@{ZWSP}annie first"),
        ("cc @org/team", f"cc @{ZWSP}org/team"),
        ("(@annie)", f"(@{ZWSP}annie)"),
        ("mail me: franck@example.com", "mail me: franck@example.com"),
        ("see.@victim", f"see.@{ZWSP}victim"),
        ("(@a) -@b", f"(@{ZWSP}a) -@{ZWSP}b"),
        ("a lone @ sign", "a lone @ sign"),
    ],
)
def test_mentions_are_broken_so_nobody_is_notified(text: str, expected: str) -> None:
    assert neutralise(text) == expected


@pytest.mark.parametrize("word", ["fixes", "Fixed", "close", "closes", "RESOLVED", "resolve"])
def test_closing_keywords_are_broken(word: str) -> None:
    out = neutralise(f"{word} #12")
    assert out == f"{word}{ZWSP} #12"


def test_closing_keyword_with_a_colon_or_another_repo_is_broken() -> None:
    assert neutralise("fixes: #3") == f"fixes{ZWSP}: #3"
    assert neutralise("closes acme/api#9") == f"closes{ZWSP} acme/api#9"


def test_plain_issue_references_and_other_words_stay() -> None:
    text = "see #12, the prefix fix is not related; fixture #3"
    assert neutralise(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "[our docs](https://evil.example/x)",
        "![shot](https://evil.example/p.png)",
        "[click here][1]\n\n[1]: https://evil.example",
        "[x]\n[x]: https://evil.example",
        "![i][1]\n\n[1]: https://evil.example/p.png",
        "[[x]](https://evil.example)",
        "[a\nb](https://evil.example)",
        "[x]( https://evil.example )",
        "\\[x\\](https://evil.example)",  # already escaped by the model: must not undo ours
    ],
)
def test_no_link_or_image_can_form_but_the_address_stays_visible(text: str) -> None:
    out = neutralise(text)
    unescaped = re.sub(r"\\.", "", out)  # what is left once every escaped character is removed
    assert "[" not in unescaped and "]" not in unescaped
    assert "https://evil.example" in out


def test_a_plain_backslash_is_still_shown_as_one() -> None:
    assert neutralise("C:\\temp") == "C:\\\\temp"


def test_html_that_renders_links_or_images_is_escaped() -> None:
    assert neutralise('<a href="https://x.example">click</a>') == (
        '&lt;a href="https://x.example">click&lt;/a>'
    )
    assert neutralise('<img src="https://x.example/p.png">') == (
        '&lt;img src="https://x.example/p.png">'
    )


def test_ordinary_angle_brackets_stay() -> None:
    assert neutralise("List<String> fails when a < b") == "List<String> fails when a < b"


def test_the_empty_string_and_plain_text_pass_through() -> None:
    assert neutralise("") == ""
    assert neutralise("Checkout crashes on Android 13") == "Checkout crashes on Android 13"
