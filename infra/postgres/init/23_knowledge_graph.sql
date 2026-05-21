-- Phase 2: knowledge graph schema
--
-- Property-graph model on Postgres: nodes (typed entities like person,
-- device, area, preference) connected by typed edges (owns, located_in,
-- prefers, etc.). Both nodes and edges carry JSONB attributes for
-- flexible per-type metadata.
--
-- Design choices:
-- 1. Single nodes table with (type, label) discriminator, JSONB attrs.
--    Simpler than per-type tables; we don't have schema migrations per
--    type and queries stay uniform.
-- 2. Edges are directed (source -> target) with rel_type — same shape
--    as Neo4j. Undirected relationships are modeled as two edges.
-- 3. external_ref column on nodes lets us link a graph node back to a
--    row in the original tables (e.g. household_members.id=2 →
--    kg_nodes WHERE type='person' AND external_ref='household_members:2').
--    Upserts use (type, external_ref) as the unique key when present.
-- 4. confidence on both nodes and edges so the routine inference engine
--    can mark "Saeed OWNS BYD (confidence=0.95, learned from HA)" vs
--    "Saeed PROBABLY LIKES Italian food (confidence=0.4, inferred)".
-- 5. Soft-delete via deleted_at instead of CASCADE — graphs benefit
--    from preserved history for "what relationships did we believe at
--    time T?" queries.
BEGIN;

CREATE TABLE IF NOT EXISTS kg_nodes (
    id              bigserial PRIMARY KEY,
    type            text NOT NULL,
    label           text NOT NULL,
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    external_ref    text NULL,
    source          text NULL,
    confidence      real NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz NULL
);

-- Lookup paths used by the store.
CREATE INDEX IF NOT EXISTS kg_nodes_type_idx
    ON kg_nodes(type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kg_nodes_label_idx
    ON kg_nodes(lower(label)) WHERE deleted_at IS NULL;
-- (type, external_ref) is the upsert key when external_ref is present.
CREATE UNIQUE INDEX IF NOT EXISTS kg_nodes_external_ref_uq
    ON kg_nodes(type, external_ref)
    WHERE external_ref IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kg_nodes_attrs_gin
    ON kg_nodes USING gin(attributes);

CREATE TABLE IF NOT EXISTS kg_edges (
    id              bigserial PRIMARY KEY,
    source_node_id  bigint NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    target_node_id  bigint NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    rel_type        text NOT NULL,
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    source          text NULL,
    confidence      real NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz NULL
);

-- The 3-tuple (source, target, rel_type) is the natural identity of an
-- edge in a property graph. Allow only one live edge per tuple via a
-- partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS kg_edges_unique_live
    ON kg_edges(source_node_id, target_node_id, rel_type)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kg_edges_source_idx
    ON kg_edges(source_node_id, rel_type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kg_edges_target_idx
    ON kg_edges(target_node_id, rel_type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS kg_edges_rel_type_idx
    ON kg_edges(rel_type) WHERE deleted_at IS NULL;

COMMIT;
