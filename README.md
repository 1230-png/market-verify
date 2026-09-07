# MoneyLogic Shorts Automation

유튜브 채널 'MoneyLogic' 쇼츠 자동화 파이프라인. Hetzner Ubuntu 서버에서 Docker Compose로 무인 운영.

매일 랜덤 시각(10:00 ±1시간)에 1건만: S&P500/특징주 데이터 수집 → Gemini 대본 → edge-tts 음성 →
Pexels 배경 이미지 → MoviePy 렌더링 → 유튜브 업로드. 실패 시 디스코드로 알림.

## 1. docker-compose로 배포하기

```bash
git clone <repo-url> moneylogic && cd moneylogic
cp .env.example .env
vi .env   # POSTGRES_PASSWORD, GEMINI_API_KEY, PEXELS_API_KEY, DISCORD_WEBHOOK_URL 채우기

docker compose up -d --build
docker compose logs -f app     # 기동 로그 확인 (logs/app.log에도 동일하게 쌓임)
curl http://localhost:8000/health
```

- `db` 컨테이너는 최초 기동 시 `schema.sql`을 자동으로 실행해 테이블을 만든다 (`docker-entrypoint-initdb.d`).
  이미 데이터가 있는 볼륨에 다시 붙이면 재실행되지 않으니, 스키마를 바꿨다면 직접
  `docker compose exec db psql -U moneylogic -d moneylogic -f /docker-entrypoint-initdb.d/schema.sql` 로 적용한다.
- 재배포(코드 변경 후): `docker compose up -d --build app`
- 중지: `docker compose down` (볼륨은 남음, `-v`를 붙이면 DB 데이터까지 삭제되니 주의)

## 2. 유튜브 OAuth 인증 (최초 1회, 로컬 PC에서)

서버는 OAuth 플로우를 절대 타지 않는다 — 로컬에서 딱 한 번 인증해서 만든 `token.json`을 DB에 밀어넣는 방식이다.

1. Google Cloud Console에서 OAuth 클라이언트(데스크톱 앱) 생성 후 `client_secret.json` 다운로드.
2. 로컬 PC(브라우저 뜨는 환경)에서 아래처럼 1회성 인증 스크립트를 실행해 `token.json`을 만든다:

   ```python
   # 로컬에서만 실행 — 이 파일은 리포에 포함하지 않음
   from google_auth_oauthlib.flow import InstalledAppFlow

   SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
   flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
   creds = flow.run_local_server(port=0)

   with open("token.json", "w") as f:
       f.write(creds.to_json())
   ```

3. 만들어진 `token.json`을 서버로 옮긴 뒤, 아래 둘 중 하나로 `youtube_credentials` 테이블에 넣는다.

   **(권장) 스크립트로 넣기** — 수동 SQL 오타 방지:
   ```bash
   pip install asyncpg
   DATABASE_URL=postgresql://moneylogic:<POSTGRES_PASSWORD>@localhost:5432/moneylogic \
       python scripts/push_credentials.py token.json MoneyLogic
   ```
   (docker-compose로 DB를 띄운 상태라면 5432 포트를 호스트에 노출하거나, `docker compose exec app python scripts/push_credentials.py ...`로 컨테이너 안에서 실행)

   **수동 SQL로 넣기** — `token.json` 내용을 직접 옮겨 적는 예시:
   ```sql
   INSERT INTO youtube_credentials
       (channel_name, token, refresh_token, token_uri, client_id, client_secret, scopes)
   VALUES (
       'MoneyLogic',
       'ya29.xxxxxxxx',                                   -- token.json의 "token"
       '1//0gxxxxxxxx',                                   -- token.json의 "refresh_token"
       'https://oauth2.googleapis.com/token',              -- token.json의 "token_uri"
       'xxxxxx.apps.googleusercontent.com',                -- token.json의 "client_id"
       'GOCSPX-xxxxxx',                                    -- token.json의 "client_secret"
       'https://www.googleapis.com/auth/youtube.upload'    -- token.json의 "scopes" (여러 개면 콤마로 이어붙임)
   )
   ON CONFLICT (channel_name) DO UPDATE SET
       token = EXCLUDED.token,
       refresh_token = EXCLUDED.refresh_token,
       updated_at = now();
   ```
   `access_token`(`token`)은 만료되면 앱이 자동으로 refresh해서 DB에 재기록하므로, 위 값을 다시 넣을 일은 거의 없다.
   refresh_token이 만료되거나 revoke되면(=계정에서 앱 연결 해제 등) 1번부터 다시.

## 3. 로컬 PC에서 테스트

```bash
python -m venv venv && source venv/bin/activate   # Windows는 venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # 값 채우기

# DB만 도커로 띄우고 앱은 로컬에서 직접 실행
docker compose up -d db
uvicorn main:app --reload

# 핵심 방어 로직(할당량/1일1건 제한/시장데이터 계산/Pexels 폴백) self-check
python test_pipeline.py

curl http://localhost:8000/health
```

- 스케줄러를 안 기다리고 바로 한 번 돌려보고 싶으면:
  `python -c "import asyncio, main; asyncio.run(main.run_daily_short_job())"`
- 로그는 콘솔과 `logs/app.log`에 동시에 남는다 (10MB마다 회전, 5개 보관).
- 크리티컬 에러(할당량 초과/인증 풀림/렌더링 실패 등) 발생 시 `DISCORD_WEBHOOK_URL`로 즉시 알림이 간다.
