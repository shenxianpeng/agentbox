CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    max_concurrent  INT  NOT NULL DEFAULT 5,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'default')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS runs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    status        TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    agent_name    TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    egress_allow  TEXT[] NOT NULL DEFAULT '{}',
    attempt       INT  NOT NULL DEFAULT 0,
    max_attempts  INT  NOT NULL DEFAULT 3,
    result        JSONB,
    error         TEXT,
    cost_estimate NUMERIC(10,6),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS runs_queue_idx ON runs (tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_index  INT  NOT NULL,
    kind        TEXT NOT NULL
        CHECK (kind IN ('model_call', 'tool_call')),
    fingerprint TEXT NOT NULL,
    payload     JSONB NOT NULL,
    token_count INT,
    cost        NUMERIC(10,6),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, step_index)
);

CREATE TABLE IF NOT EXISTS leases (
    run_id       UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    owner        TEXT NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scoped_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    credential    TEXT NOT NULL,
    scope         TEXT NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scoped_creds_run_idx ON scoped_credentials (run_id);
