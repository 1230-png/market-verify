"""파이프라인을 즉시 1회 실행한다 (스케줄러 대기 없이).

    docker compose exec app python run_job.py
    docker compose exec app python run_job.py --force

--force 는 "하루 1건" 업로드 제한을 우회한다. 테스트용이며, 실제로 영상이
한 편 더 업로드되고 유튜브 API 쿼터도 그만큼 더 소모된다.
main 을 import 하는 순간 load_dotenv()/configure_logging() 이 실행되므로
여기서 따로 부르지 않는다. 스케줄러는 start() 하지 않으니 뜨지 않는다.
"""

import asyncio
import sys

import main
from db import close_pool


async def _run(force: bool) -> None:
    try:
        await main.run_daily_short_job(force=force)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_run(force="--force" in sys.argv))
