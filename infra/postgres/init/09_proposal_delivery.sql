ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS github_issue_url text,
    ADD COLUMN IF NOT EXISTS github_pr_url text,
    ADD COLUMN IF NOT EXISTS dispatched_at timestamptz,
    ADD COLUMN IF NOT EXISTS dispatch_error text;
