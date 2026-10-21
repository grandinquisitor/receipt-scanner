import pytest

from receipt_scanner.extractors import ReceiptFields


@pytest.mark.parametrize("total, cents", [
    ("23.45", 2345),
    ("$23.45", 2345),
    ("23.45 EUR", 2345),
    ("12,50", 1250),
    ("1,234.56", 123456),
    ("1.234,56", 123456),
    ("-5.00", -500),
    ("7", 700),
    (None, None),
    ("", None),
    ("-", None),
    (".", None),
    ("n/a", None),
])
def test_total_cents(total, cents):
    assert ReceiptFields(total=total).total_cents() == cents


@pytest.mark.xfail(strict=True, reason="a lone comma is always read as a decimal separator, so 1,234 becomes 1.234")
def test_total_cents_thousands_separator_without_decimals():
    assert ReceiptFields(total="1,234").total_cents() == 123400


@pytest.mark.parametrize("value, expected", [
    ("2026-03-04", "2026-03-04"),
    (" 2026-03-04 ", "2026-03-04"),
    ("03/04/2026", None),
    ("2026-02-30", None),
    (None, None),
])
def test_date_is_iso_or_none(value, expected):
    assert ReceiptFields(date=value).date == expected


@pytest.mark.parametrize("value, expected", [
    ("top", "top"),
    (" RIGHT ", "right"),
    ("sideways", None),
    (None, None),
])
def test_text_top(value, expected):
    assert ReceiptFields(text_top=value).text_top == expected


def test_bad_fields_dont_sink_the_others():
    f = ReceiptFields(vendor="Hardware Co", total="9.99", date="yesterday", text_top="diagonal")
    assert (f.vendor, f.total_cents(), f.date, f.text_top) == ("Hardware Co", 999, None, None)
