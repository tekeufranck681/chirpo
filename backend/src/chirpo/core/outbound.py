"""Cleans model-written text before it is posted to the code host (M5.2).

The text may come from chat or from a screenshot, so it is untrusted. Posted as is, it could notify
people (`@mentions`), close issues by accident (`fixes #12`) or carry a link or picture that hides
where it goes. Markdown is not parsed (there are too many ways to write a link): every `[`, `]` and
`\\` is escaped, so no link, image or reference definition can form, while a URL stays visible as
plain text. A zero-width space after the `@` or the keyword breaks those patterns on GitHub's side
and is invisible to readers. The card shows only the start of a long body, so none of this relies on
the person confirming having read every character.
"""

import re
import unicodedata

from chirpo.core.actions import unsafe_char

ZWSP = "\u200b"

# GitHub notifies for @name and @org/team when it follows anything but a word character.
_MENTION = re.compile(r"(?<!\w)@(?=\w)")
# A closing keyword followed by an issue reference ("fixes #1", "closes: acme/api#2").
_CLOSING = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?=\s*:?\s+(?:[\w.-]+/[\w.-]+)?#\d)",
    re.IGNORECASE,
)
_MARKDOWN_SYNTAX = re.compile(r"[\\\[\]]")
# Tags that render a link, a picture or something that loads: the rest of GitHub's HTML is inert.
_ACTIVE_TAG = re.compile(
    r"<(?=/?(?:a|img|iframe|object|embed|form|input|button|video|audio|source|link|meta)\b)",
    re.IGNORECASE,
)


def break_mentions(text: str) -> str:
    return _MENTION.sub("@" + ZWSP, text)


def neutralise(text: str) -> str:
    text = _MARKDOWN_SYNTAX.sub(lambda m: "\\" + m.group(), text)
    text = _ACTIVE_TAG.sub("&lt;", text)
    text = _CLOSING.sub(lambda m: m.group(1) + ZWSP, text)
    return break_mentions(text)


# A link or a t.me address: Telegram makes them tappable, so a model must not post one.
_LINK = re.compile(r"https?://|www\.|\bt\.me/|\b[\w-]+\.[a-z]{2,}/", re.IGNORECASE)
# "/unlink" at the start of a word becomes a tappable bot command in a group.
_COMMAND = re.compile(r"(?<!\S)/(?=\w)")
_JOINERS = "\u200c\u200d"  # needed by some scripts and by emoji


def _hidden(char: str) -> bool:
    """Format characters (word joiner, the Tag block, ...) can hide text or defeat the `@` break."""
    return unicodedata.category(char) == "Cf" and char not in _JOINERS


def plain_reply(text: str, limit: int) -> str | None:
    """Model-written chat text made safe to post, or None when it shouldn't be posted at all.

    One visible line cut to `limit`, no invisible characters, no @mention that notifies anyone and
    no tappable command; a reply containing a link is dropped (None) rather than repaired."""
    visible = "".join(char for char in text if not unsafe_char(char) and not _hidden(char))
    line = " ".join(visible.split())[:limit]
    if not line or _LINK.search(line):
        return None
    return break_mentions(_COMMAND.sub("/" + ZWSP, line))
