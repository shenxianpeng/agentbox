-- Migration 003: store the W3C traceparent captured at run creation.
--
-- The API records the current trace context when a run is enqueued; the
-- launcher attaches it before claiming the run so the whole lifecycle
-- (API -> launcher -> runner) appears as a single Logfire trace.
--
-- The runner role does not need this column (the launcher passes the
-- context via the TRACEPARENT env var), so it is intentionally left out
-- of the agentbox_runner column grants from migration 002.

ALTER TABLE runs ADD COLUMN IF NOT EXISTS traceparent TEXT;
