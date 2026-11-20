-- dashkit_auth 스키마의 표(계약: contracts/auth.md "데이터 모델").
-- data-db/init/02-auth.sh가 superuser(postgres)로 한 트랜잭션 안에서 적용하고 schema_version에 기록한다.
-- 역할 이름(DATA_DB_AUTH_USER)에 대한 권한은 이 파일이 아니라 02-auth.sh가 매번 다시 준다.
-- 시각은 전부 timestamptz. uuid는 gen_random_uuid()(PostgreSQL 13+ 내장).

CREATE TABLE dashkit_auth.users (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text NOT NULL UNIQUE CHECK (email = lower(email) AND email <> ''),
    password_hash        text NULL,
    role                 text NOT NULL CHECK (role IN ('admin', 'operator', 'user')),
    status               text NOT NULL CHECK (status IN ('pending', 'invited', 'active', 'suspended')),
    must_change_password boolean NOT NULL DEFAULT false,
    source               text NOT NULL CHECK (source IN ('signup', 'invite')),
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL,
    email_verified_at    timestamptz NULL,
    password_changed_at  timestamptz NULL,
    last_login_at        timestamptz NULL,
    login_count          integer NOT NULL DEFAULT 0,
    failed_count         integer NOT NULL DEFAULT 0,
    locked_until         timestamptz NULL
);

CREATE TABLE dashkit_auth.sessions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES dashkit_auth.users (id) ON DELETE CASCADE,
    token_hash   bytea NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    ip           inet NULL,
    user_agent   text NULL
);
CREATE INDEX sessions_user_id_idx ON dashkit_auth.sessions (user_id);

CREATE TABLE dashkit_auth.tokens (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES dashkit_auth.users (id) ON DELETE CASCADE,
    purpose    text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password', 'invite')),
    token_hash bytea NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX tokens_user_purpose_idx ON dashkit_auth.tokens (user_id, purpose);

CREATE TABLE dashkit_auth.groups (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL UNIQUE CHECK (name <> ''),
    description text NOT NULL DEFAULT '',
    is_default  boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE dashkit_auth.group_members (
    group_id uuid NOT NULL REFERENCES dashkit_auth.groups (id) ON DELETE CASCADE,
    user_id  uuid NOT NULL REFERENCES dashkit_auth.users (id) ON DELETE CASCADE,
    added_at timestamptz NOT NULL DEFAULT now(),
    added_by uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX group_members_user_id_idx ON dashkit_auth.group_members (user_id);

CREATE TABLE dashkit_auth.group_tabs (
    group_id uuid NOT NULL REFERENCES dashkit_auth.groups (id) ON DELETE CASCADE,
    project  text NOT NULL,
    tab_key  text NOT NULL,
    PRIMARY KEY (group_id, project, tab_key)
);

CREATE TABLE dashkit_auth.view_events (
    id        bigserial PRIMARY KEY,
    user_id   uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL,
    project   text NOT NULL,
    tab_key   text NOT NULL,
    viewed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX view_events_tab_idx ON dashkit_auth.view_events (project, tab_key, viewed_at);
CREATE INDEX view_events_user_idx ON dashkit_auth.view_events (user_id, viewed_at);

CREATE TABLE dashkit_auth.audit_log (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    actor_id     uuid NULL,
    actor_email  text NULL,
    action       text NOT NULL,
    target_type  text NULL,
    target_id    text NULL,
    target_label text NULL,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip           inet NULL
);
CREATE INDEX audit_log_at_idx ON dashkit_auth.audit_log (at);

CREATE TABLE dashkit_auth.outbox (
    id         bigserial PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES dashkit_auth.users (id) ON DELETE CASCADE,
    to_email   text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('verify_email', 'reset_password', 'invite', 'already_registered')),
    link       text NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_created_idx ON dashkit_auth.outbox (created_at);
