-- Migration 002: Create restricted runner role with Row-Level Security.
--
-- The runner container should NOT use the superuser (agentbox) credentials.
-- Instead, it uses the `agentbox_runner` role which can only access rows
-- belonging to its own run_id (set via `app.run_id` session variable).
--
-- RLS policies use `current_setting('app.run_id')` to restrict access.
-- The application sets this at connection time:
--   SET app.run_id = '<run-uuid>';
--
-- Usage:
--   CREATE ROLE agentbox_runner WITH LOGIN PASSWORD 'agentbox_runner_dev';
--   (applied below with a default dev password; change in production)

-- ── Create the runner role (dev password; override via env in production) ──
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'agentbox_runner') THEN
        CREATE ROLE agentbox_runner WITH LOGIN PASSWORD 'agentbox_runner_dev';
    END IF;
END
$$;

-- ── Grant schema usage ──
GRANT USAGE ON SCHEMA public TO agentbox_runner;

-- ── runs: runner can SELECT any run row, UPDATE only rows matching its run_id ──
GRANT SELECT (id, tenant_id, status, agent_name, prompt, egress_allow,
              attempt, max_attempts, result, error, cost_estimate, created_at,
              started_at, finished_at)
    ON TABLE runs TO agentbox_runner;
GRANT UPDATE (status, result, error, cost_estimate, started_at, finished_at)
    ON TABLE runs TO agentbox_runner;

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;

CREATE POLICY runner_select_own_run ON runs
    FOR SELECT
    TO agentbox_runner
    USING (id::text = current_setting('app.run_id', true));  -- scoped to own run

CREATE POLICY runner_update_own_run ON runs
    FOR UPDATE
    TO agentbox_runner
    USING (id::text = current_setting('app.run_id', true))
    WITH CHECK (id::text = current_setting('app.run_id', true));

-- ── checkpoints: runner can only SELECT/INSERT rows matching its run_id ──
GRANT SELECT, INSERT (run_id, step_index, kind, fingerprint, payload, token_count, cost)
    ON TABLE checkpoints TO agentbox_runner;

ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;

CREATE POLICY runner_select_own_checkpoints ON checkpoints
    FOR SELECT
    TO agentbox_runner
    USING (run_id::text = current_setting('app.run_id', true));

CREATE POLICY runner_insert_own_checkpoints ON checkpoints
    FOR INSERT
    TO agentbox_runner
    WITH CHECK (run_id::text = current_setting('app.run_id', true));

-- ── leases: runner can manage only its own lease ──
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE leases TO agentbox_runner;

ALTER TABLE leases ENABLE ROW LEVEL SECURITY;

CREATE POLICY runner_manage_own_lease ON leases
    FOR ALL
    TO agentbox_runner
    USING (run_id::text = current_setting('app.run_id', true))
    WITH CHECK (run_id::text = current_setting('app.run_id', true));

-- ── scoped_credentials: runner has NO access (credentials are injected via proxy) ──
-- The runner does NOT need to read scoped_credentials at all.
-- The per-run token is injected via AGENTBOX_CREDENTIALS_JSON env var.
-- Explicitly revoke any default access.
ALTER TABLE scoped_credentials ENABLE ROW LEVEL SECURITY;

CREATE POLICY runner_no_access ON scoped_credentials
    FOR ALL
    TO agentbox_runner
    USING (false);

-- ── tenants: runner has no access ──
REVOKE ALL ON TABLE tenants FROM agentbox_runner;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;

CREATE POLICY runner_no_access_tenants ON tenants
    FOR ALL
    TO agentbox_runner
    USING (false);
