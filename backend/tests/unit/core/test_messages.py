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


def test_no_text_is_defined_twice() -> None:
    """A second definition silently replaces the first for every user of the name."""
    import ast
    from pathlib import Path

    from chirpo.core import messages

    tree = ast.parse(Path(messages.__file__).read_text())
    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    assert not duplicated, f"defined more than once in messages.py: {duplicated}"
