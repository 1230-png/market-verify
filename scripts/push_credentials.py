"""
로컬에서 만든 token.json을 읽어 PostgreSQL youtube_credentials 테이블에 넣는다.
수동으로 SQL 문자열 이스케이핑하다 오타 내는 걸 방지하기 위한 용도.

사용법:
    DATABASE_URL=postgresql://moneylogic:pw@localhost:5432/moneylogic \
        python scripts/push_credentials.py token.json MoneyLogic
"""
import asyncio
import json
import os
import sys

import asyncpg


async def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python push_credentials.py <token.json path> <channel_name>")
        sys.exit(1)

    token_path, channel_name = sys.argv[1], sys.argv[2]
    with open(token_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            """
            INSERT INTO youtube_credentials
                (channel_name, token, refresh_token, token_uri, client_id, client_secret, scopes)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (channel_name) DO UPDATE SET
                token = EXCLUDED.token,
                refresh_token = EXCLUDED.refresh_token,
                token_uri = EXCLUDED.token_uri,
                client_id = EXCLUDED.client_id,
                client_secret = EXCLUDED.client_secret,
                scopes = EXCLUDED.scopes,
                updated_at = now()
            """,
            channel_name,
            data["token"],
            data["refresh_token"],
            data["token_uri"],
            data["client_id"],
            data["client_secret"],
            ",".join(data["scopes"]),
        )
        print(f"credentials pushed for channel '{channel_name}'")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
