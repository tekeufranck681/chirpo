"""Closing keywords in a pull request's description (M6.15, D38).

GitHub links a PR to an issue, and closes the issue when the PR is merged into the default
branch, only through a keyword in the description: `Closes #12`, `Fixes owner/repo#12`, ... The
API has no other way to make the link, so chirpo adds or removes such a line and keeps the
author's text as it is.
"""

import re

KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# Between a keyword and the issue: a few spaces and a colon. Bounded, so a description full of
# spaces can't make the match backtrack for seconds and stall the worker.
GAP = r"[ \t]{0,10}:?[ \t]{1,10}"
_LEADING_KEYWORD = re.compile(rf"^{KEYWORDS}{GAP}", re.IGNORECASE)
# A template line waiting for a number: "Closes #" alone on its line
_PLACEHOLDER = re.compile(
    rf"^([ \t]*[-*]?[ \t]*{KEYWORDS}{GAP})#[ \t]*$", re.IGNORECASE | re.MULTILINE
)


def issue_ref(number: int, repo: str | None = None) -> str:
    """`#12`, or `owner/repo#12` for an issue in another repository."""
    return f"{repo}#{number}" if repo else f"#{number}"


def _pattern(refs: tuple[str, ...]) -> re.Pattern[str]:
    names = "|".join(re.escape(ref) for ref in refs)
    return re.compile(rf"\b{KEYWORDS}{GAP}(?:{names})(?![\w/])", re.IGNORECASE)


def is_linked(body: str, *refs: str) -> bool:
    """Whether the description closes the issue under any of its spellings."""
    return bool(_pattern(refs).search(body))


def with_link(body: str, ref: str) -> str:
    """The description with a `Closes <ref>` line at the end, unless it already closes it."""
    if is_linked(body, ref):
        return body
    text = _lines(body)
    if _PLACEHOLDER.search(text):  # a template's empty "Closes #": fill it in
        return _PLACEHOLDER.sub(lambda match: f"{match.group(1)}{ref}", text, count=1)
    text = text.rstrip()
    return f"{text}\n\nCloses {ref}" if text else f"Closes {ref}"


def without_link(body: str, *refs: str) -> str:
    """The description without the keyword for the issue: a line that only says it goes (with the
    blank line before it, when that would leave two); in a sentence ("Fixes #12 and tidies up")
    only the keyword goes, so the text still reads. Nothing else in the text changes."""
    pattern = _pattern(refs)
    lines = _lines(body).split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        if pattern.search(line) and not pattern.sub("", line).strip(" \t-*.,;"):
            after = lines[index + 1] if index + 1 < len(lines) else ""
            if kept and not kept[-1].strip() and not after.strip():
                kept.pop()
            continue
        kept.append(pattern.sub(lambda match: _LEADING_KEYWORD.sub("", match.group(0)), line))
    return "\n".join(kept).strip("\n")


def _lines(body: str) -> str:
    """GitHub's web editor saves Windows line ends: one kind of line end makes the edit exact."""
    return body.replace("\r\n", "\n")
