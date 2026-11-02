"""Tests for the ordered-products list parsed from confirmation emails.

The confirmation and order-change emails carry a "Products you've ordered"
table. Home Assistant flattens the HTML with BeautifulSoup into one cell per
line, so a row becomes the product name, the quantity and the price on three
consecutive lines. These cases mirror that layout with placeholder products.
"""

from datetime import date, datetime
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from custom_components.ocado.const import OcadoEmail, OcadoOrder
from custom_components.ocado.utils import (
    delivery_attributes,
    email_triage,
    order_parse,
    parse_products,
)

ORDER_NUMBER = "1234567890123"

PRODUCTS_BODY = """We have just placed your next Ocado Reserved order for 8:00pm – 9:00pm on 13 November 2026.
Delivery time: Between 8:00pm and 9:00pm
Delivery date: 13 November 2026
Order reference: 1234567890123
Total (estimated): £12.35
Products you’ve ordered
Product
Qty
Price to pay (£)
Sample Organic Firm Tofu
2
£ 5.20
Sample Oat Drink 1L
1
£ 1.85
Sample 3 x 100g Hummus Pots
3
£ 5.30
Editing your order
It’s easy to edit what you’ve ordered.
You can edit this order until 11:12pm, 12 November 2026"""

EXPECTED = [
    {"name": "Sample Organic Firm Tofu", "quantity": 2, "price": "5.20"},
    {"name": "Sample Oat Drink 1L", "quantity": 1, "price": "1.85"},
    {"name": "Sample 3 x 100g Hummus Pots", "quantity": 3, "price": "5.30"},
]


def _email(body: str | None, products: list | None = None) -> OcadoEmail:
    return OcadoEmail(
        message_id=b"1",
        email_type="confirmation",
        email_date=date(2026, 11, 1),
        from_address="orders@ocado.com",
        subject="Confirmation of your order",
        body=body,
        order_number=ORDER_NUMBER,
        delivery_datetime=datetime(2026, 11, 13, 20, 0),
        delivery_window_end=datetime(2026, 11, 13, 21, 0),
        products=products,
    )


def test_parse_products() -> None:
    """Each table row becomes name, integer quantity and price."""
    assert parse_products(PRODUCTS_BODY) == EXPECTED


def test_parse_products_order_change_header() -> None:
    """Order-change emails title the last column just "Price"."""
    assert parse_products(PRODUCTS_BODY.replace("Price to pay (£)", "Price")) == EXPECTED


def test_parse_products_straight_apostrophe() -> None:
    """The header matches with a plain apostrophe as well as a curly one."""
    assert parse_products(PRODUCTS_BODY.replace("you’ve ordered", "you've ordered")) == EXPECTED


def test_parse_products_without_table() -> None:
    """An email with no product table gives an empty list."""
    assert parse_products("Order reference: 1234567890123\nThanks for shopping") == []
    assert parse_products(None) == []


def test_parse_products_stops_at_end_of_table() -> None:
    """Lines after the table are never read as products."""
    names = [p["name"] for p in parse_products(PRODUCTS_BODY)]
    assert "Editing your order" not in names


def test_order_parse_attaches_products() -> None:
    """A confirmation email's order carries its products."""
    assert order_parse(_email(PRODUCTS_BODY)).products == EXPECTED


def test_order_parse_prefers_triaged_products() -> None:
    """Products already chosen during triage are kept as-is."""
    chosen = [{"name": "Kept", "quantity": 1, "price": "1.00"}]
    assert order_parse(_email(PRODUCTS_BODY, products=chosen)).products == chosen


def test_delivery_attributes_include_products() -> None:
    """The delivery sensors expose the products; an empty list when there's no order."""
    order = order_parse(_email(PRODUCTS_BODY))
    assert delivery_attributes(order)["products"] == EXPECTED
    assert delivery_attributes(None)["products"] == []


def test_as_dict_includes_products() -> None:
    """The Orders sensor's dict form carries the products."""
    order = OcadoOrder(
        updated=date(2026, 11, 1),
        order_number=ORDER_NUMBER,
        delivery_datetime=None,
        delivery_window_end=None,
        edit_datetime=None,
        estimated_total="12.35",
        products=EXPECTED,
    )
    assert order.as_dict()["products"] == EXPECTED


def _raw(subject: str, sent: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Ocado <orders@ocado.com>"
    msg["Subject"] = subject
    msg["Date"] = sent
    msg.set_content(body)
    return bytes(msg)


def test_triage_keeps_products_when_newest_email_has_none() -> None:
    """An order-change email without a table falls back to the latest one that had it."""
    newer = _raw(
        "Confirmation of your order changes",
        "Wed, 04 Nov 2026 10:00:00 +0000",
        PRODUCTS_BODY.split("Products you’ve ordered", maxsplit=1)[0]
        + "You can edit this order until 11:12pm, 12 November 2026",
    )
    older = _raw(
        "Confirmation of your order",
        "Fri, 30 Oct 2026 20:00:00 +0000",
        PRODUCTS_BODY,
    )
    server = MagicMock()
    server.search.return_value = ("OK", [b"1 2"])
    # Emails are walked newest-first, so message 2 (newer) is fetched first.
    server.fetch.side_effect = lambda mid, _: ("OK", [(b"", {b"2": newer, b"1": older}[mid])])
    coordinator = MagicMock(
        imap_host="imap.example.com",
        imap_port=993,
        email_address="me@example.com",
        password="x",
        imap_folder="INBOX",
        imap_days=31,
        data=None,
    )
    with patch("custom_components.ocado.utils.imap", return_value=server):
        _, triaged = email_triage(coordinator)
    assert triaged is not None
    assert len(triaged.confirmations) == 1
    newest = triaged.confirmations[0]
    assert newest.subject == "Confirmation of your order changes"
    assert newest.products == EXPECTED
    assert order_parse(newest).products == EXPECTED
