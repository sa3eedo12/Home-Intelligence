"""Tests for the Noon Minutes order poller (parsers + shape, no network)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.noon_poller import (
    _parse_dt,
    _shape_order,
    _to_decimal,
    parse_curl,
)


# ── cURL parsing ────────────────────────────────────────────────


def test_parse_curl_extracts_cookies_and_headers() -> None:
    curl = """curl 'https://minutes.noon.com/_svc/instant/order/list' \\
-X 'POST' \\
-H 'Accept: application/json' \\
-H 'x-addresskey: 76cc75abb69df7540f27c1e877046270-5' \\
-H 'x-nooninstant-zonecode: W00106306A' \\
-H 'Cookie: _natnetidv2=jwt.payload.sig; visitor_id=abc-123; ak_bmsc=XYZ' \\
--data-raw '{"pageNr":1}'"""
    parsed = parse_curl(curl)
    assert parsed["cookies"]["_natnetidv2"] == "jwt.payload.sig"
    assert parsed["cookies"]["visitor_id"] == "abc-123"
    assert parsed["cookies"]["ak_bmsc"] == "XYZ"
    assert parsed["address_key"] == "76cc75abb69df7540f27c1e877046270-5"
    assert parsed["instant_zone"] == "W00106306A"
    # Cookie header is consumed, NOT also stored as a plain header
    assert "cookie" not in parsed["headers"]


def test_parse_curl_rejects_non_noon_url() -> None:
    curl = """curl 'https://example.com/' \\
-H 'Cookie: a=b'"""
    with pytest.raises(ValueError, match="noon.com"):
        parse_curl(curl)


def test_parse_curl_rejects_missing_curl_prefix() -> None:
    with pytest.raises(ValueError):
        parse_curl("wget https://minutes.noon.com/anything")


def test_parse_curl_decodes_customer_email_from_whoami_cookie() -> None:
    """x-whoami-rest is a base64-JSON cookie that includes the user's
    email — we pull it out so the dashboard can show 'authenticated as'."""
    import base64
    import json
    from urllib.parse import quote

    whoami = {"customer": {"email": "alice@example.com"}}
    encoded = quote(base64.b64encode(json.dumps(whoami).encode()).decode())
    curl = f"""curl 'https://minutes.noon.com/_svc/instant/order/list' \\
-H 'Cookie: x-whoami-rest={encoded}; _natnetidv2=x.y.z'"""
    parsed = parse_curl(curl)
    assert parsed["customer_email"] == "alice@example.com"


# ── Order shaping ───────────────────────────────────────────────


def test_shape_order_handles_canonical_fields() -> None:
    """Use the real Noon Minutes payload shape (orderNr, orderTotal,
    placedAt, statusCode, items with sku/title/brand/qty/price)."""
    raw = {
        "orderNr": "IAEG5S306MU8HQPEZA",
        "statusCode": "delivered",
        "status": "Delivered",
        "placedAt": "2026-05-28T21:34:52",   # Asia/Dubai local (naive)
        "deliveredAt": None,
        "orderTotal": 30.2,
        "orderSubtotal": 30.2,
        "currencyCode": "AED",
        "items": [
            {
                "sku": "Z50ED630A8E759900C781Z-1",
                "titleEn": "Barakat Fresh Vitamin C Shot",
                "title": "Fresh Vitamin C Shot",
                "brandEn": "Barakat",
                "qty": 4,
                "price": 5.75,
                "imageKey": "pzsku/Z50ED.../foo.jpg",
                "media": [{"type": "image", "path": "https://..."}],  # noise
            },
            {"sku": "B", "title": "Eggs", "qty": 1, "price": 14.5},
        ],
    }
    out = _shape_order(raw)
    assert out["source"] == "noon_minutes"
    assert out["external_id"] == "IAEG5S306MU8HQPEZA"
    # statusCode preferred over display status, lowercased
    assert out["status"] == "delivered"
    assert out["total_amount"] == 30.2
    assert out["total_currency"] == "AED"
    assert out["item_count"] == 2
    # placedAt is naive ISO → localised to Asia/Dubai
    assert out["ordered_at"].isoformat().startswith("2026-05-28T21:34:52")
    assert "Asia/Dubai" in str(out["ordered_at"].tzinfo) or "+04:00" in out["ordered_at"].isoformat()
    # Items slimmed to useful subset
    first = out["items_json"][0]
    assert first["sku"] == "Z50ED630A8E759900C781Z-1"
    assert first["title"] == "Barakat Fresh Vitamin C Shot"
    assert first["brand"] == "Barakat"
    assert first["qty"] == 4
    assert first["price"] == 5.75
    # noise dropped
    assert "media" not in first
    # raw_json keeps the full original
    assert out["raw_json"] == raw


def test_shape_order_falls_through_to_alternate_keys() -> None:
    """Noon ships slightly different keys per experience tier — nimda
    instead of orderNr, grandTotal instead of orderTotal, etc.
    Shaping must not fail just because of that."""
    raw = {
        "nimda": "ORD-99",
        "orderStatus": "Confirmed",
        "createdAt": "2026-05-28T13:20:00+00:00",
        "grandTotal": "45.0",
        "orderItems": [{"productName": "Bread", "quantity": 1}],
    }
    out = _shape_order(raw)
    assert out["external_id"] == "ORD-99"
    assert out["status"] == "confirmed"
    assert out["total_amount"] == 45.0
    assert out["item_count"] == 1
    assert out["ordered_at"] == datetime(2026, 5, 28, 13, 20, tzinfo=UTC)


def test_shape_order_handles_epoch_ms_timestamps() -> None:
    raw = {"id": "1", "createdAt": 1716902400000}  # epoch ms
    out = _shape_order(raw)
    assert out["ordered_at"] == datetime(2024, 5, 28, 13, 20, tzinfo=UTC)


def test_shape_order_returns_empty_id_when_no_natural_key() -> None:
    """Without a Noon-side ID we can't dedupe — _shape_order returns
    "" so the caller can skip insertion."""
    out = _shape_order({"status": "weird"})
    assert out["external_id"] == ""


def test_shape_order_tolerates_garbage_timestamps_and_totals() -> None:
    raw = {
        "id": "X",
        "createdAt": "not a date",
        "totalAmount": "nope",
        "items": "should-be-list",
    }
    out = _shape_order(raw)
    assert out["external_id"] == "X"
    assert out["ordered_at"] is None
    assert out["total_amount"] is None
    assert out["items_json"] == []
    assert out["item_count"] is None


def test_shape_order_against_real_noon_payload(tmp_path) -> None:
    """Pin compatibility against the captured Noon Minutes response —
    if Noon reshapes a field, this test surfaces it."""
    import json as _json
    from pathlib import Path
    fixture = Path("/Users/saeed/.copilot/session-state/35a36466-ca81-41d2-b475-96bd50aed7b4/files/paste-1780048937392.txt")
    if not fixture.exists():
        pytest.skip("fixture not present in this environment")
    data = _json.loads(fixture.read_text())
    assert data.get("totalPages", 0) > 0
    orders = data["orders"]
    assert len(orders) > 0
    # All orders should shape cleanly with non-empty external_ids
    shaped = [_shape_order(o) for o in orders]
    assert all(s["external_id"] for s in shaped)
    assert all(s["total_amount"] is not None for s in shaped)
    assert all(s["ordered_at"] is not None for s in shaped)
    # Every external_id is unique (so the upsert key actually dedups)
    assert len({s["external_id"] for s in shaped}) == len(shaped)


# ── Number/date coercions ──────────────────────────────────────


def test_to_decimal_rounds_to_two_places() -> None:
    assert _to_decimal(12.34567) == 12.35
    assert _to_decimal("99") == 99.0
    assert _to_decimal(None) is None
    assert _to_decimal("invalid") is None


def test_parse_dt_handles_iso_z_suffix_and_offsets() -> None:
    assert _parse_dt("2026-01-15T10:00:00Z") == datetime(2026, 1, 15, 10, tzinfo=UTC)
    assert _parse_dt("2026-01-15T10:00:00+04:00").hour == 10
    assert _parse_dt(None) is None
    assert _parse_dt("not a date") is None


# ── poll_once: end-to-end with mocked HTTP ──────────────────────


def _pool_with(conn: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = cm
    return pool


@pytest.mark.asyncio
async def test_poll_once_persists_new_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import noon_poller

    # Stub the HTTP fetch — return one order, then an empty page (loop exits)
    fetches: list[int] = []

    def fake_fetch(page: int, creds: dict) -> dict:
        fetches.append(page)
        if page == 1:
            return {
                "orders": [{"nimda": "N1", "totalAmount": 50.0,
                            "createdAt": "2026-05-28T10:00:00Z",
                            "items": [{"name": "Milk", "qty": 1}]}],
                "totalPages": 2,
            }
        return {"orders": [], "totalPages": 2}

    monkeypatch.setattr(noon_poller, "_fetch_page", fake_fetch)

    conn = MagicMock()
    # _load_credentials returns row with cookies
    conn.fetchrow = AsyncMock(return_value={
        "cookies": {"_natnetidv2": "abc"},
        "headers": {},
        "address_key": "AK",
        "instant_zone": "Z",
        "customer_email": "x@y.z",
        "cookie_expires_at": None,
    })
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _pool_with(conn)

    summary = await noon_poller.poll_once(pool, max_pages=3)
    assert summary["ok"] is True
    assert summary["fetched"] == 1
    assert summary["inserted"] == 1
    # Stopped after page 2 because it was empty
    assert summary["pages"] == 2


@pytest.mark.asyncio
async def test_poll_once_handles_auth_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import noon_poller

    def fake_fetch(page: int, creds: dict) -> dict:
        raise noon_poller.NoonAuthExpired("HTTP 403")

    monkeypatch.setattr(noon_poller, "_fetch_page", fake_fetch)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "cookies": {"_natnetidv2": "abc"}, "headers": {},
        "address_key": None, "instant_zone": None,
        "customer_email": None, "cookie_expires_at": None,
    })
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = _pool_with(conn)

    summary = await noon_poller.poll_once(pool)
    assert summary["ok"] is False
    assert summary["reason"] == "auth_expired"
    # Status was recorded for dashboard surfacing
    assert any(
        "auth_expired" in str(call.args)
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_poll_once_skips_when_no_credentials() -> None:
    from orchestrator import noon_poller

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)  # no row in noon_credentials
    summary = await noon_poller.poll_once(_pool_with(conn))
    assert summary == {"ok": False, "reason": "no_credentials"}


@pytest.mark.asyncio
async def test_poll_once_skips_when_no_pool() -> None:
    from orchestrator import noon_poller

    summary = await noon_poller.poll_once(None)
    assert summary == {"ok": False, "reason": "no_pool"}
