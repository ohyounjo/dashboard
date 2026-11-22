-- 자동 적재용 API 토큰 (#38, contracts/auth.md).
-- 원문과 접두사 조각은 저장하지 않고 SHA-256 해시(token_hash)와 메타데이터만 저장한다.
CREATE TABLE dashkit_auth.api_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 100),
    project      text NOT NULL CHECK (project <> ''),
    token_hash   bytea NOT NULL UNIQUE,
    tables       text[] NOT NULL CHECK (cardinality(tables) > 0),
    created_by   uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_used_at timestamptz NULL,
    revoked_at   timestamptz NULL,
    revoked_by   uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL
);

CREATE INDEX api_tokens_project_created_idx ON dashkit_auth.api_tokens (project, created_at DESC);
