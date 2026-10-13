-- Audit tables: one row per pipeline run, plus the result of every data-quality check.
-- Written over a separate autocommit connection so that a FAILED run is still recorded
-- even though the main load transaction is rolled back.

CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS audit.pipeline_runs (
    run_id         bigserial   PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text        NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    failed_stage   text,        -- extract | transform | load | quality | database | config
    error_message  text,
    details        jsonb        -- row counts, null counts, rejected values, DQ summary
);

CREATE TABLE IF NOT EXISTS audit.dq_results (
    run_id       bigint      NOT NULL REFERENCES audit.pipeline_runs (run_id),
    check_name   text        NOT NULL,
    stage        text        NOT NULL,   -- staging | analytics
    severity     text        NOT NULL CHECK (severity IN ('CRITICAL', 'WARN')),
    status       text        NOT NULL CHECK (status IN ('PASS', 'FAIL', 'WARN')),
    violations   bigint      NOT NULL,
    description  text        NOT NULL,
    checked_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, check_name)
);
