-- Async extraction tasks for dengbao (等级保护测评) PDF uploads.
-- A row is created on accept, claimed by the single background worker,
-- and terminalized with either the extracted report (jsonb) or a typed
-- public error. The uploaded bytes are stored with the task so pending
-- work survives service restarts and re-runs without the caller.

CREATE TABLE IF NOT EXISTS report_tasks (
    task_id UUID PRIMARY KEY,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    file_bytes BYTEA NOT NULL,
    doc_type TEXT NOT NULL,
    status TEXT NOT NULL,
    result JSONB,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    CONSTRAINT report_tasks_status_check
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    CONSTRAINT report_tasks_doc_type_check
        CHECK (doc_type IN ('dengbao')),
    CONSTRAINT report_tasks_state_check CHECK (
        (
            status IN ('pending', 'running')
            AND result IS NULL
            AND error_code IS NULL
            AND completed_at IS NULL
        )
        OR
        (
            status = 'succeeded'
            AND result IS NOT NULL
            AND error_code IS NULL
            AND completed_at IS NOT NULL
        )
        OR
        (
            status = 'failed'
            AND result IS NULL
            AND error_code IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);

-- Dedup lookups (in-flight reuse, completed-result cache hits).
CREATE INDEX IF NOT EXISTS report_tasks_sha_status_idx
    ON report_tasks (file_sha256, status);

-- Worker claim scan.
CREATE INDEX IF NOT EXISTS report_tasks_status_created_idx
    ON report_tasks (status, created_at);
