"""Noon Minutes order poller.

Polls the Noon Minutes order list endpoint every N minutes using
``curl_cffi`` (which impersonates Safari/Chrome's TLS fingerprint to
satisfy Cloudflare). New orders go into ``noon_orders`` and surface on
the dashboard.

Auth model:
  Noon's web app drops a 5-minute JWT (``_natnetidv2``) plus longer-
  lived cookies. We don't try to refresh — that requires replicating
  Noon's internal token-rotation flow which moves around. Instead we
  trust that:
    1. Cloudflare's allow-list is the primary gate (impersonation
       handles this)
    2. The session cookies remain valid for hours-to-days of polling
       (empirically the JWT expiry isn't strictly enforced for the
       /list endpoint and the platform issues fresh access tokens on
       page navigation)
    3. When auth eventually fails, we log it with status
       'cookies_expired' and the dashboard prompts the user to paste
       a fresh cURL.

So the loop is dead simple: load cookies → POST → on 200 diff against
known external_ids → persist deltas → on 401/403 record the failure
and bail until the user refreshes credentials.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from home_agents_sdk.telemetry import get_logger

logger = get_logger("orchestrator.noon_poller")

ORDERS_URL = "https://minutes.noon.com/_svc/instant/order/list"

# Headers reused on every call. The four x-* zonecode/locale/addresskey
# values are stored per-credentials because they encode the user's
# delivery address + region (Noon validates them and 404s otherwise).
_STATIC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://minutes.noon.com",
    "Referer": "https://minutes.noon.com/uae-en/account/orders/",
    "x-experience": "nooninstant",
    "x-mp": "nooninstant",
    "x-mp-country": "ae",
    "x-locale": "en-ae",
    "x-platform": "web",
    "x-cms": "v2",
}


class NoonAuthExpired(RuntimeError):
    """Cookies no longer accepted by Cloudflare or Noon's auth gate."""


async def poll_once(pool: Any, *, max_pages: int = 3, start_page: int = 1) -> dict[str, Any]:
    """Poll the Noon Minutes /list endpoint and persist any new orders.

    Returns a structured summary suitable for logging or admin
    surfaces. Doesn't raise on auth-expired; writes the status to
    noon_credentials so the dashboard can surface it.

    ``max_pages`` caps how many pages we'll walk in one invocation.
    Routine polling uses the default (3) — enough to catch a burst of
    new orders without thrashing. Initial backfill should pass a much
    higher value to walk the full history; tune via the admin endpoint.
    ``start_page`` defaults to 1 (newest) but lets backfill jobs
    paginate without re-reading already-known pages.
    """
    if pool is None:
        return {"ok": False, "reason": "no_pool"}
    creds = await _load_credentials(pool)
    if not creds or not creds.get("cookies"):
        return {"ok": False, "reason": "no_credentials"}

    summary: dict[str, Any] = {
        "ok": True,
        "fetched": 0,
        "inserted": 0,
        "refreshed": 0,
        "pages": 0,
        "start_page": start_page,
        "max_pages": max_pages,
    }
    try:
        consecutive_known = 0
        for page in range(start_page, start_page + max_pages):
            page_data = await asyncio.to_thread(
                _fetch_page, page, creds
            )
            summary["pages"] += 1
            orders = page_data.get("orders") or []
            summary["fetched"] += len(orders)
            if not orders:
                break
            inserted, refreshed = await _persist_orders(pool, orders)
            summary["inserted"] += inserted
            summary["refreshed"] += refreshed
            total_pages = int(page_data.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break
            # During routine polling (start_page=1), stop early once we
            # hit a page where everything was already known — there's
            # nothing new beyond.
            if start_page == 1 and inserted == 0 and orders:
                consecutive_known += 1
                if consecutive_known >= 1:
                    break
        await _record_poll(pool, "ok", None)
    except NoonAuthExpired as exc:
        await _record_poll(pool, "auth_expired", str(exc))
        summary.update({"ok": False, "reason": "auth_expired", "error": str(exc)})
        logger.warning("noon_poll_auth_expired", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        await _record_poll(pool, "error", str(exc))
        summary.update({"ok": False, "reason": "error", "error": str(exc)})
        logger.warning("noon_poll_failed", error=str(exc))
    return summary


def _fetch_page(page: int, creds: dict[str, Any]) -> dict[str, Any]:
    """Synchronous curl_cffi call. Run in a thread from the async poll
    loop so the GIL release while curl is waiting on the network
    doesn't pin the event loop."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:
        raise RuntimeError("curl_cffi not installed") from exc

    headers = dict(_STATIC_HEADERS)
    headers.update(_creds_headers(creds))
    # User-Agent comes from the impersonation profile so don't force one
    response = cffi_requests.post(
        ORDERS_URL,
        headers=headers,
        cookies=dict(creds.get("cookies") or {}),
        json={"pageNr": page},
        impersonate="safari17_0",
        timeout=15,
    )
    if response.status_code in (401, 403):
        raise NoonAuthExpired(
            f"HTTP {response.status_code} on /list (Cloudflare or token gate)"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"unexpected status {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"bad json: {exc}; body={response.text[:200]}") from exc


def _creds_headers(creds: dict[str, Any]) -> dict[str, str]:
    """Translate stored credentials into per-request x-* headers."""
    out: dict[str, str] = {}
    if creds.get("address_key"):
        out["x-addresskey"] = creds["address_key"]
    if creds.get("instant_zone"):
        out["x-nooninstant-zonecode"] = creds["instant_zone"]
    extra = creds.get("headers") or {}
    if isinstance(extra, dict):
        # Pass through any other x-* headers the user captured (visitor-id,
        # zonecodes for other Noon experiences, build, etc.). Filter to x-*
        # to avoid clobbering Origin/Referer.
        for k, v in extra.items():
            if isinstance(k, str) and k.startswith("x-") and isinstance(v, str):
                out.setdefault(k, v)
    return out


async def _persist_orders(
    pool: Any, orders: list[dict[str, Any]]
) -> tuple[int, int]:
    """Upsert each order on (source, external_id). Returns
    (inserted_count, refreshed_count)."""
    inserted = 0
    refreshed = 0
    async with pool.acquire() as conn:
        for order in orders:
            shape = _shape_order(order)
            if not shape["external_id"]:
                continue
            status = await conn.execute(
                """
                INSERT INTO noon_orders(
                    source, external_id, status, ordered_at, delivered_at,
                    total_amount, total_currency, item_count,
                    items_json, raw_json
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    delivered_at = COALESCE(EXCLUDED.delivered_at, noon_orders.delivered_at),
                    total_amount = COALESCE(EXCLUDED.total_amount, noon_orders.total_amount),
                    item_count = COALESCE(EXCLUDED.item_count, noon_orders.item_count),
                    items_json = EXCLUDED.items_json,
                    raw_json = EXCLUDED.raw_json,
                    last_seen_at = now()
                """,
                shape["source"], shape["external_id"], shape["status"],
                shape["ordered_at"], shape["delivered_at"],
                shape["total_amount"], shape["total_currency"],
                shape["item_count"],
                json.dumps(shape["items_json"], default=str),
                json.dumps(shape["raw_json"], default=str),
            )
            if status.startswith("INSERT"):
                inserted += 1
            else:
                refreshed += 1
    return inserted, refreshed


def _shape_order(order: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw Noon order dict into our column shape.

    Field map confirmed against a real Noon Minutes /list payload:
      orderNr → external_id  (e.g. "IAEG5S306MU8HQPEZA")
      placedAt → ordered_at   (naive Asia/Dubai ISO string)
      deliveredAt → delivered_at (often null while in flight)
      orderTotal → total_amount (numeric)
      currencyCode → total_currency
      statusCode → status      (machine-friendly: 'delivered', 'cancelled', ...)
      items → items_json       (slimmed; full payload kept in raw_json)
    Falls through to alternate keys for forward compatibility if Noon
    re-shapes the schema for a different Noon experience tier.
    """
    external_id = (
        order.get("orderNr")
        or order.get("nimda")
        or order.get("orderId")
        or order.get("id")
    )
    items = (
        order.get("items")
        or order.get("orderItems")
        or order.get("lineItems")
        or []
    )
    total = (
        order.get("orderTotal")
        or order.get("totalAmount")
        or order.get("grandTotal")
        or order.get("total")
        or (order.get("payment") or {}).get("total")
    )
    currency = (
        order.get("currencyCode")
        or order.get("currency")
        or (order.get("payment") or {}).get("currency")
        or "AED"
    )
    # Prefer statusCode (machine ID) over status (display label)
    status = (
        order.get("statusCode")
        or order.get("status")
        or order.get("orderStatus")
    )
    return {
        "source": "noon_minutes",
        "external_id": str(external_id) if external_id is not None else "",
        "status": str(status).lower() if status else None,
        "ordered_at": _parse_dt(
            order.get("placedAt")
            or order.get("createdAt")
            or order.get("orderDate")
        ),
        "delivered_at": _parse_dt(
            order.get("deliveredAt")
            or order.get("completedAt")
            or order.get("deliveryDate")
        ),
        "total_amount": _to_decimal(total),
        "total_currency": str(currency),
        "item_count": len(items) if isinstance(items, list) else None,
        "items_json": [_slim_item(it) for it in items] if isinstance(items, list) else [],
        "raw_json": order,
    }


def _slim_item(item: Any) -> dict[str, Any]:
    """Pull only the fields that matter from a Noon line-item.

    The raw payload includes huge image URLs, AR titles, brand AR,
    media[] arrays, etc. We keep what's useful for the dashboard and
    pantry surface; the full thing is preserved in raw_json anyway."""
    if not isinstance(item, dict):
        return {}
    return {
        "sku": item.get("sku") or item.get("itemCode"),
        "title": (
            item.get("titleEn")
            or item.get("title")
            or item.get("productName")
            or item.get("name")
        ),
        "brand": item.get("brandEn") or item.get("brand"),
        "qty": item.get("qty") or item.get("quantity"),
        "price": item.get("price") or item.get("unitPrice"),
        "image_key": item.get("imageKey"),
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Noon often returns epoch milliseconds for timestamps
        try:
            if value > 1e12:
                return datetime.fromtimestamp(value / 1000, tz=UTC)
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            # Noon's placedAt is naive ISO in the user's local timezone
            # (Asia/Dubai for AE accounts). Localize so downstream queries
            # comparing against now() / Dubai-local times don't get a 4h
            # phantom shift.
            from zoneinfo import ZoneInfo
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Dubai"))
            except Exception:
                parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _to_decimal(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


async def _load_credentials(pool: Any) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cookies, headers, address_key, instant_zone,
                   customer_email, cookie_expires_at
            FROM noon_credentials WHERE id = 1
            """
        )
    if row is None:
        return None
    d = dict(row)
    for k in ("cookies", "headers"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                d[k] = {}
    return d


async def _record_poll(pool: Any, status: str, error: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE noon_credentials
            SET last_poll_at = now(),
                last_poll_status = $1,
                last_poll_error = $2
            WHERE id = 1
            """,
            status, error,
        )


# ── cURL parsing for the credentials-upload endpoint ────────────────


def parse_curl(curl: str) -> dict[str, Any]:
    """Parse a Safari/Chrome "Copy as cURL" multi-line string into the
    cookies + selected x-* headers we need to drive the poller.

    Tolerates the messy backslash-line-continuation format DevTools
    produces. We don't try to honour the URL/method — we always POST
    to ORDERS_URL — but we DO sanity-check that the URL is a Noon
    domain to catch obvious paste mistakes.
    """
    import re
    import shlex

    # Collapse line continuations and split into tokens shell-style
    flat = re.sub(r"\\\s*\n", " ", curl).strip()
    tokens = shlex.split(flat)
    if not tokens or tokens[0] != "curl":
        raise ValueError("expected a cURL string starting with 'curl'")

    url: str | None = None
    headers_raw: list[str] = []
    cookie_str: str | None = None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header") and i + 1 < len(tokens):
            headers_raw.append(tokens[i + 1])
            i += 2
            continue
        if t in ("-b", "--cookie") and i + 1 < len(tokens):
            cookie_str = tokens[i + 1]
            i += 2
            continue
        if t in ("-X", "--request", "--data-raw", "--data",
                 "-d", "-o", "--compressed"):
            # Either consume the next token if the flag takes a value
            # or drop the flag entirely. Conservatively skip pairs.
            if t not in ("--compressed",) and i + 1 < len(tokens):
                i += 2
            else:
                i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        if url is None and (t.startswith("http://") or t.startswith("https://")):
            url = t
        i += 1

    if url is None or "noon.com" not in url:
        raise ValueError(
            "cURL doesn't target a noon.com URL — paste the request from "
            "/account/orders/'s Network tab"
        )

    headers: dict[str, str] = {}
    for h in headers_raw:
        if ":" not in h:
            continue
        name, _, val = h.partition(":")
        name = name.strip()
        val = val.strip()
        if name.lower() == "cookie":
            cookie_str = val
            continue
        headers[name] = val

    cookies: dict[str, str] = {}
    if cookie_str:
        for kv in cookie_str.split("; "):
            if "=" not in kv:
                continue
            k, _, v = kv.partition("=")
            cookies[k.strip()] = v.strip().strip('"')

    return {
        "cookies": cookies,
        "headers": {k.lower(): v for k, v in headers.items()},
        "address_key": headers.get("x-addresskey") or headers.get("X-Addresskey"),
        "instant_zone": headers.get("x-nooninstant-zonecode")
                        or headers.get("X-Nooninstant-Zonecode"),
        "customer_email": _extract_customer_email(cookies),
    }


def _extract_customer_email(cookies: dict[str, str]) -> str | None:
    """Decode the x-whoami-rest cookie (base64-JSON) to pull the email."""
    raw = cookies.get("x-whoami-rest")
    if not raw:
        return None
    import base64
    try:
        # Cookie is URL-encoded base64
        from urllib.parse import unquote
        decoded = base64.b64decode(unquote(raw) + "==")
        data = json.loads(decoded)
        return (data.get("customer") or {}).get("email")
    except Exception:  # noqa: BLE001
        return None


async def store_credentials(pool: Any, parsed: dict[str, Any]) -> None:
    """Persist parsed credentials (singleton row, id=1)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO noon_credentials(
                id, cookies, headers, address_key, instant_zone,
                customer_email, updated_at, cookie_expires_at
            )
            VALUES (1, $1::jsonb, $2::jsonb, $3, $4, $5, now(),
                    _noon_cookie_exp($1::jsonb))
            ON CONFLICT (id) DO UPDATE SET
                cookies = EXCLUDED.cookies,
                headers = EXCLUDED.headers,
                address_key = COALESCE(EXCLUDED.address_key, noon_credentials.address_key),
                instant_zone = COALESCE(EXCLUDED.instant_zone, noon_credentials.instant_zone),
                customer_email = COALESCE(EXCLUDED.customer_email, noon_credentials.customer_email),
                updated_at = now(),
                cookie_expires_at = _noon_cookie_exp(EXCLUDED.cookies)
            """,
            json.dumps(parsed.get("cookies") or {}),
            json.dumps(parsed.get("headers") or {}),
            parsed.get("address_key"),
            parsed.get("instant_zone"),
            parsed.get("customer_email"),
        )


# Small SQL helper that pulls the JWT exp out of the _natnetidv2 cookie.
# Defined as a function so the UPDATE above stays readable. Falls back
# to NULL if the cookie isn't a parseable JWT.
_COOKIE_EXP_FUNC = """
CREATE OR REPLACE FUNCTION _noon_cookie_exp(cookies jsonb)
RETURNS timestamptz LANGUAGE plpgsql AS $$
DECLARE
  jwt text;
  payload text;
  exp_epoch bigint;
BEGIN
  jwt := cookies->>'_natnetidv2';
  IF jwt IS NULL THEN RETURN NULL; END IF;
  -- decode the middle segment as base64 (with padding)
  payload := split_part(jwt, '.', 2);
  payload := payload || repeat('=', (4 - char_length(payload) % 4) % 4);
  payload := translate(payload, '-_', '+/');
  BEGIN
    exp_epoch := (convert_from(decode(payload, 'base64'), 'UTF8')::jsonb ->> 'exp')::bigint;
    RETURN to_timestamp(exp_epoch);
  EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
  END;
END;
$$;
"""


async def ensure_helper_fn(pool: Any) -> None:
    """Create the _noon_cookie_exp helper function if missing.
    Called lazily from store_credentials so we don't need a separate
    migration step."""
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(_COOKIE_EXP_FUNC)
