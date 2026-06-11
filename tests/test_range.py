from datetime import date

import app as appmod


def test_7d_window():
    assert appmod._range_to_window("7d", date(2026, 6, 10)) == (date(2026, 6, 4), date(2026, 6, 10))


def test_30d_window():
    assert appmod._range_to_window("30d", date(2026, 6, 10)) == (date(2026, 5, 12), date(2026, 6, 10))


def test_1y_window():
    assert appmod._range_to_window("1y", date(2026, 6, 10)) == (date(2025, 6, 11), date(2026, 6, 10))


def test_all_window():
    start, end = appmod._range_to_window("all", date(2026, 6, 10))
    assert start == date(2000, 1, 1)
    assert end == date(2026, 6, 10)


def test_unknown_defaults_to_7d():
    assert appmod._range_to_window("bogus", date(2026, 6, 10)) == (date(2026, 6, 4), date(2026, 6, 10))
