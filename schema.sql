-- OAuth 크리덴셜: 로컬에서 만든 token.json 값을 여기 그대로 넣어둔다. 서버는 절대 OAuth 플로우를 타지 않는다.
CREATE TABLE IF NOT EXISTS youtube_credentials (
    id              SERIAL PRIMARY KEY,
    channel_name    TEXT NOT NULL UNIQUE,
    token           TEXT NOT NULL,          -- access_token (만료되면 refresh 후 갱신됨)
    refresh_token   TEXT NOT NULL,
    token_uri       TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    client_secret   TEXT NOT NULL,
    scopes          TEXT NOT NULL,          -- 콤마(,)로 구분된 scope 목록
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 유튜브 API 크레딧 사용 로그. 업로드 직전 오늘 합계를 SUM(cost)로 조회해서 한도 체크.
CREATE TABLE IF NOT EXISTS api_usage_log (
    id              SERIAL PRIMARY KEY,
    channel_name    TEXT NOT NULL,
    cost            INTEGER NOT NULL,
    action          TEXT NOT NULL,          -- 'upload' 등
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_usage_log_channel_day
    ON api_usage_log (channel_name, created_at);
