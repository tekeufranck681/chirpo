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

from chirpo.core.context import clean
from chirpo.core.links import GAP, KEYWORDS

ZWSP = "\u200b"

# GitHub notifies for @name and @org/team when it follows anything but a word character.
_MENTION = re.compile(r"(?<!\w)@(?=\w)")
# A closing keyword followed by an issue reference ("fixes #1", "closes: acme/api#2").
_CLOSING = re.compile(
    rf"\b({KEYWORDS})(?={GAP}(?:[\w.-]+/[\w.-]+)?#\d)",
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
# A bare address Telegram still makes tappable ("deploy-login.com", "bit.ly", "t.me", "tg://",
# "mailto:"). Web endings only, so file names like main.py or app.ts stay.
_BARE_LINK = re.compile(
    r"\btg://|\bmailto:|\bt\.me\b|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|ly|me|co|app|dev|xyz|"
    r"info|biz|top|site|online|link|click|live|shop|ru|cn|tk|ml|ga|cf|gq|cc|to|gg|ai)\b",
    re.IGNORECASE,
)
# "/unlink" becomes a tappable bot command in a group, also in quotes or brackets. A slash after a
# letter, another slash or a colon is part of a path or a URL and stays.
_COMMAND = re.compile(r"(?<![\w/:])/(?=\w)")


def _defuse(line: str) -> str:
    return break_mentions(_COMMAND.sub("/" + ZWSP, line))


def plain_lines(text: str, *, line_limit: int, max_lines: int) -> str | None:
    """Several lines of model text made safe to post, like `plain_reply` line by line; a line
    with a link is dropped, not the whole text. None when no line is left."""
    kept = []
    for line in text.splitlines():
        visible = clean(line, line_limit)
        if visible and not _LINK.search(visible) and not _BARE_LINK.search(visible):
            kept.append(_defuse(visible))
    return "\n".join(kept[:max_lines]) or None


def shown(text: str, limit: int) -> str:
    """Outside text (an issue or milestone title, a display name) made safe to show in chat:
    one visible line cut to `limit`, no invisible characters, no @mention that notifies anyone
    and no tappable command. Links stay readable; link previews are off."""
    return _defuse(clean(text, limit))


def plain_reply(text: str, limit: int) -> str | None:
    """Model-written chat text made safe to post, or None when it shouldn't be posted at all:
    like `shown`, and a reply containing a link is dropped rather than repaired."""
    line = clean(text, limit)
    if not line or _LINK.search(line):
        return None
    return _defuse(line)
