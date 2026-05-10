CREATE TABLE IF NOT EXISTS indexed_documents (
    path text PRIMARY KEY,
    sha256 text,
    chunk_count int,
    indexed_at timestamptz DEFAULT now()
);
