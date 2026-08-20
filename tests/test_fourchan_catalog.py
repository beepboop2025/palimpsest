"""Board and safety locks for the 4chan rumour tap."""

from collectors.fourchan_catalog import (
    ALLOWED_BOARDS,
    BLOCKED_BOARDS,
    classify_minor_safety,
)


def test_board_lock_is_news_and_int_only():
    assert ALLOWED_BOARDS == {"news", "int"}
    assert BLOCKED_BOARDS == {"pol", "b"}
    assert ALLOWED_BOARDS.isdisjoint(BLOCKED_BOARDS)


def test_minor_safety_drops_when_unsure_or_denied():
    assert classify_minor_safety("") == "drop"
    assert classify_minor_safety("teen rumor") == "drop"
    assert classify_minor_safety("Guangdong factory fire") == "pass"
