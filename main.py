import asyncio
import logging
import os
import time
import traceback
from contextlib import contextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI

import media_generator
import script_generator
import tts_generator
import youtube_upload
from db import close_pool, get_pool
from utils.logging_config import configure_logging
from utils.notifier import send_discord_alert

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI()
scheduler = AsyncIOScheduler()

CHANNEL_NAME = youtube_upload.CHANNEL_NAME


@contextmanager
def _timed_stage(name: str):
    """파이프라인 각 단계 소요 시간을 INFO로 남긴다. 나중에 렌더링 병목 추적용."""
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.info("[stage:%s] took %.1fs", name, time.perf_counter() - start)


async def _already_uploaded_today(channel_name: str) -> bool:
    """하루 최대 1건 업로드 제한. 크레딧 한도와 별개로 이 조건도 반드시 통과해야 한다."""
    pool = await get_pool()
    count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM api_usage_log
        WHERE channel_name = $1 AND action = 'upload' AND created_at >= date_trunc('day', now())
        """,
        channel_name,
    )
    return count > 0


def _build_title_and_description(data: script_generator.MarketData) -> tuple[str, str]:
    direction = "상승" if data.spy_change_pct >= 0 else "하락"
    title = f"S&P500 오늘 {direction} {abs(data.spy_change_pct)}% | {data.mover_ticker} 특징주 브리핑 #Shorts"
    description = (
        "오늘의 시장 브리핑\n"
        f"SPY 종가: {data.spy_close} ({direction} {abs(data.spy_change_pct)}%)\n"
        f"특징주: {data.mover_ticker} ({abs(data.mover_change_pct)}%)\n"
        f"#shorts #moneylogic #주식 #{data.mover_ticker}"
    )
    return title, description


async def run_daily_short_job() -> None:
    """
    데이터 수집 -> 대본(Gemini) -> TTS(edge-tts) -> 배경 이미지(Pexels) -> 영상 합성(MoviePy) -> 유튜브 업로드.
    yfinance/Gemini/requests/MoviePy는 전부 블로킹 호출이라 asyncio.to_thread로 돌려 이벤트 루프를 막지 않는다.
    각 단계가 만든 임시 파일은 성공/실패와 무관하게 finally에서 전부 지운다 (디스크 에러 방지).
    크리티컬 실패(할당량 초과, 인증 풀림, 렌더링 실패 등)는 디스코드로 즉시 알림한다.
    """
    if await _already_uploaded_today(CHANNEL_NAME):
        logger.info("Already uploaded today for %s, skipping.", CHANNEL_NAME)
        return

    stage = "init"
    audio_path = None
    image_path = None
    output_path = None
    try:
        stage = "market_data"
        with _timed_stage(stage):
            market_data = await asyncio.to_thread(script_generator.fetch_market_data)

        stage = "script"
        with _timed_stage(stage):
            script_text = await asyncio.to_thread(script_generator.generate_script, market_data)

        stage = "tts"
        with _timed_stage(stage):
            audio_path = await tts_generator.synthesize_speech(script_text)

        stage = "background_image"
        with _timed_stage(stage):
            keyword = "stock market rally" if market_data.spy_change_pct >= 0 else "stock market crash"
            image_path = await asyncio.to_thread(
                media_generator.fetch_background_image, [keyword, "finance", "stock market"]
            )

        stage = "render"
        with _timed_stage(stage):
            output_path = await asyncio.to_thread(
                media_generator.generate_short, image_path, script_text, audio_path
            )

        stage = "upload"
        with _timed_stage(stage):
            title, description = _build_title_and_description(market_data)
            await youtube_upload.upload_short(
                video_path=output_path,
                title=title,
                description=description,
                channel_name=CHANNEL_NAME,
            )
    except RuntimeError as e:
        # 할당량 초과, 크리덴셜 없음, 데이터/이미지/대본 수급 실패 등 예상 가능한 실패 -> 로그 남기고 알림.
        logger.warning("Daily short job aborted at stage '%s': %s", stage, e)
        await asyncio.to_thread(
            send_discord_alert, f"⚠️ MoneyLogic 파이프라인 중단 (stage={stage})\n{e}"
        )
    except Exception:
        # 예상 못한 실패도 여기서 삼켜서 스케줄러 자체가 죽지 않게 한다. 전체 traceback은 logs/app.log에 남는다.
        logger.exception("Daily short job failed unexpectedly at stage '%s'", stage)
        tb = traceback.format_exc()[-1500:]
        await asyncio.to_thread(
            send_discord_alert, f"🚨 MoneyLogic 파이프라인 크리티컬 에러 (stage={stage})\n```{tb}```"
        )
    finally:
        for path in (audio_path, image_path, output_path):
            if path and os.path.exists(path):
                os.remove(path)


@app.on_event("startup")
async def startup() -> None:
    await get_pool()
    scheduler.add_job(
        run_daily_short_job,
        CronTrigger(hour=10, minute=0),
        jitter=3600,  # 매크로 탐지 회피용 랜덤 지연 (최대 ±1시간)
        id="daily_short_upload",
        max_instances=1,  # 이전 실행이 안 끝났으면 겹쳐 돌지 않는다
        coalesce=True,
    )
    scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    scheduler.shutdown(wait=False)
    await close_pool()


@app.get("/health")
async def health():
    return {"status": "ok"}
