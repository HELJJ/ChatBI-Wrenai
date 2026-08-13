CREATE TABLE IF NOT EXISTS chat_audit_requests (
    request_id UUID PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    status TEXT NOT NULL,
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,

    CONSTRAINT chat_audit_requests_status_check
        CHECK (status IN ('running', 'succeeded', 'failed')),
    CONSTRAINT chat_audit_requests_state_check CHECK (
        (
            status = 'running'
            AND answer IS NULL
            AND error IS NULL
            AND completed_at IS NULL
        )
        OR
        (
            status = 'succeeded'
            AND answer IS NOT NULL
            AND error IS NULL
            AND completed_at IS NOT NULL
        )
        OR
        (
            status = 'failed'
            AND answer IS NULL
            AND error IS NOT NULL
            AND completed_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS chat_audit_requests_session_started_idx
    ON chat_audit_requests (session_id, started_at DESC);

CREATE INDEX IF NOT EXISTS chat_audit_requests_thread_idx
    ON chat_audit_requests (thread_id);

CREATE INDEX IF NOT EXISTS chat_audit_requests_status_started_idx
    ON chat_audit_requests (status, started_at);

CREATE TABLE IF NOT EXISTS chat_sql_attempts (
    attempt_id UUID PRIMARY KEY,
    request_id UUID NOT NULL
        REFERENCES chat_audit_requests(request_id),
    sequence INTEGER NOT NULL,
    semantic_sql TEXT NOT NULL,
    executed_sql TEXT,
    status TEXT NOT NULL,
    row_limit INTEGER NOT NULL,
    returned_row_count INTEGER NOT NULL DEFAULT 0,
    result_truncated BOOLEAN NOT NULL DEFAULT false,
    result JSONB,
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,

    CONSTRAINT chat_sql_attempts_request_sequence_unique
        UNIQUE (request_id, sequence),
    CONSTRAINT chat_sql_attempts_status_check
        CHECK (status IN ('running', 'success', 'failed')),
    CONSTRAINT chat_sql_attempts_sequence_check
        CHECK (sequence >= 1),
    CONSTRAINT chat_sql_attempts_row_limit_check
        CHECK (row_limit >= 1),
    CONSTRAINT chat_sql_attempts_returned_row_count_check
        CHECK (
            returned_row_count >= 0
            AND returned_row_count <= row_limit
        ),
    CONSTRAINT chat_sql_attempts_duration_check
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT chat_sql_attempts_state_check CHECK (
        (
            status = 'running'
            AND result IS NULL
            AND error IS NULL
            AND completed_at IS NULL
            AND duration_ms IS NULL
        )
        OR
        (
            status = 'success'
            AND executed_sql IS NOT NULL
            AND result IS NOT NULL
            AND error IS NULL
            AND completed_at IS NOT NULL
            AND duration_ms IS NOT NULL
        )
        OR
        (
            status = 'failed'
            AND result IS NULL
            AND error IS NOT NULL
            AND completed_at IS NOT NULL
            AND duration_ms IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS chat_sql_attempts_request_sequence_idx
    ON chat_sql_attempts (request_id, sequence);

CREATE TABLE IF NOT EXISTS chat_session_leases (
    session_id TEXT PRIMARY KEY,
    lease_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
