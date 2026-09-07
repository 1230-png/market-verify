"""
최소 self-check. 실제 DB/외부 API 호출 없이 핵심 방어 로직만 검증한다.
python test_pipeline.py 로 실행.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("GEMINI_API_KEY", "unused")
os.environ.setdefault("PEXELS_API_KEY", "unused")

sys.path.insert(0, os.path.dirname(__file__))

import media_generator  # noqa: E402
import script_generator  # noqa: E402
import youtube_upload  # noqa: E402
import main  # noqa: E402
from utils import notifier  # noqa: E402


def _fake_pool(fetchval_return):
    pool = AsyncMock()
    pool.fetchval.return_value = fetchval_return
    return pool


async def test_quota_blocks_over_limit():
    pool = _fake_pool(7000)  # 이미 7000 소모 + 1600 업로드 = 8600 > 8000
    youtube_upload.get_pool = AsyncMock(return_value=pool)
    try:
        await youtube_upload._check_quota("MoneyLogic")
        assert False, "should have raised"
    except RuntimeError as e:
        assert "Quota limit reached" in str(e)


async def test_quota_allows_under_limit():
    pool = _fake_pool(1000)
    youtube_upload.get_pool = AsyncMock(return_value=pool)
    await youtube_upload._check_quota("MoneyLogic")  # raise 안 하면 통과


async def test_daily_limit_skips_second_upload():
    pool = _fake_pool(1)  # 오늘 이미 1건 업로드됨
    main.get_pool = AsyncMock(return_value=pool)
    assert await main._already_uploaded_today("MoneyLogic") is True


async def test_daily_limit_allows_first_upload():
    pool = _fake_pool(0)
    main.get_pool = AsyncMock(return_value=pool)
    assert await main._already_uploaded_today("MoneyLogic") is False


def test_pct_change_math():
    assert script_generator._pct_change(100, 105) == 5.0
    assert script_generator._pct_change(100, 95) == -5.0


def test_fetch_background_images_falls_back_to_next_keyword():
    """첫 키워드가 실패해도 다음 키워드로 넘어가고, 성공하면 그 결과를 반환한다."""
    fail_resp = Mock(status_code=200)
    fail_resp.raise_for_status = Mock()
    fail_resp.json.return_value = {"photos": []}  # 검색 결과 없음 -> RuntimeError

    ok_search_resp = Mock(status_code=200)
    ok_search_resp.raise_for_status = Mock()
    ok_search_resp.json.return_value = {"photos": [{"src": {"portrait": "http://img/example.jpg"}}]}

    ok_image_resp = Mock(status_code=200, content=b"fake-jpeg-bytes")
    ok_image_resp.raise_for_status = Mock()

    calls = {"n": 0}

    def fake_get(url, *args, **kwargs):
        if url == media_generator.PEXELS_SEARCH_URL:
            calls["n"] += 1
            return fail_resp if calls["n"] == 1 else ok_search_resp
        return ok_image_resp

    # 이 샌드박스가 Windows라 진짜 '/tmp'가 없다 — 배포 대상인 Ubuntu에서는 media_generator.TMP_DIR 그대로 사용.
    tmp_dir = os.path.join(os.path.dirname(__file__), "_tmp_test")
    os.makedirs(tmp_dir, exist_ok=True)
    with patch.object(media_generator, "TMP_DIR", tmp_dir), patch.object(
        media_generator.requests, "get", side_effect=fake_get
    ):
        paths = media_generator.fetch_background_images(["no-results-keyword", "finance"], n=1)
        try:
            assert len(paths) == 1 and os.path.exists(paths[0])
            assert calls["n"] == 2  # 첫 키워드 실패 후 두 번째로 넘어갔는지 확인
        finally:
            for p in paths:
                os.remove(p)
            os.rmdir(tmp_dir)


def test_discord_alert_never_raises_without_webhook():
    """웹훅 URL이 없어도 에러 핸들러 안에서 호출되는 함수라 절대 예외를 던지면 안 된다."""
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    notifier.send_discord_alert("test message")  # 예외 없이 조용히 리턴하면 통과


def test_discord_alert_never_raises_on_network_failure():
    os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/fake/fake"
    with patch.object(notifier.requests, "post", side_effect=ConnectionError("boom")):
        notifier.send_discord_alert("test message")  # 여기서도 예외 없이 리턴해야 함


async def run_all():
    await test_quota_blocks_over_limit()
    await test_quota_allows_under_limit()
    await test_daily_limit_skips_second_upload()
    await test_daily_limit_allows_first_upload()
    test_pct_change_math()
    test_fetch_background_images_falls_back_to_next_keyword()
    test_discord_alert_never_raises_without_webhook()
    test_discord_alert_never_raises_on_network_failure()
    print("OK: quota + daily-limit + market-math + pexels-fallback + discord-notifier guards behave as expected")


if __name__ == "__main__":
    asyncio.run(run_all())
