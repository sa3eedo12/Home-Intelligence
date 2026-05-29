-- Phase: Noon Minutes order tracking
--
-- noon_orders: one row per order observed (deduped by source + external_order_id)
--   items_json keeps the structured items list from the Noon API
--   raw_json keeps the full payload so we can re-extract anything later
--   without re-polling Noon
-- noon_credentials: the cookies + headers needed to call the Noon API
--   stored as a single row (id=1) updated when the user pastes a fresh cURL.
--   keeps expires_at to surface "session expired" before the next poll fails.

CREATE TABLE IF NOT EXISTS noon_orders (
    id              bigserial PRIMARY KEY,
    source          text NOT NULL DEFAULT 'noon_minutes',
    external_id     text NOT NULL,
    status          text,
    ordered_at      timestamptz,
    delivered_at    timestamptz,
    total_amount    numeric(10,2),
    total_currency  text DEFAULT 'AED',
    item_count      int,
    items_json      jsonb NOT NULL DEFAULT '[]'::jsonb,
    raw_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS noon_orders_natural_key
  ON noon_orders(source, external_id);
CREATE INDEX IF NOT EXISTS noon_orders_ordered_at_idx
  ON noon_orders(ordered_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS noon_credentials (
    id              int PRIMARY KEY DEFAULT 1,
    cookies         jsonb NOT NULL DEFAULT '{}'::jsonb,
    headers         jsonb NOT NULL DEFAULT '{}'::jsonb,
    address_key     text,
    instant_zone    text,
    customer_email  text,
    -- bookkeeping
    cookie_expires_at timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    last_poll_at    timestamptz,
    last_poll_status text,
    last_poll_error text,
    CONSTRAINT noon_credentials_singleton CHECK (id = 1)
);

INSERT INTO noon_credentials(id) VALUES (1) ON CONFLICT DO NOTHING;
