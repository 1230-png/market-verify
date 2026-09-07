import logging
import os

import requests

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 1900  # 디스코드 실제 한도(2000)보다 약간 여유를 둠


def send_discord_alert(message: str) -> None:
    """
    크리티컬 에러를 디스코드 웹훅으로 즉시 알림.
    알림 전송 자체가 실패해도(웹훅 URL 미설정, 네트워크 오류 등) 절대 예외를 위로 던지지 않는다 —
    에러 핸들러 안에서 호출되므로, 여기서 죽으면 원래 에러 처리/클린업이 통째로 날아간다.
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set, skipping Discord alert: %s", message)
        return

    try:
        resp = requests.post(webhook_url, json={"content": message[:DISCORD_MESSAGE_LIMIT]}, timeout=10)
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to send Discord alert")
