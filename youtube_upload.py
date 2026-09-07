import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from db import get_pool

logger = logging.getLogger(__name__)

CHANNEL_NAME = "MoneyLogic"
UPLOAD_COST = 1600
DAILY_QUOTA_LIMIT = 8000


async def _load_credentials(channel_name: str) -> Credentials:
    """DB에 저장된 token.json 값으로 Credentials를 조립한다. 서버에서 OAuth 플로우는 절대 타지 않는다."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT token, refresh_token, token_uri, client_id, client_secret, scopes
        FROM youtube_credentials
        WHERE channel_name = $1
        """,
        channel_name,
    )
    if row is None:
        raise RuntimeError(f"No stored credentials for channel '{channel_name}'")

    creds = Credentials(
        token=row["token"],
        refresh_token=row["refresh_token"],
        token_uri=row["token_uri"],
        client_id=row["client_id"],
        client_secret=row["client_secret"],
        scopes=row["scopes"].split(","),
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        await pool.execute(
            "UPDATE youtube_credentials SET token = $1, updated_at = now() WHERE channel_name = $2",
            creds.token,
            channel_name,
        )

    return creds


async def _check_quota(channel_name: str) -> None:
    pool = await get_pool()
    used_today = await pool.fetchval(
        """
        SELECT COALESCE(SUM(cost), 0) FROM api_usage_log
        WHERE channel_name = $1 AND created_at >= date_trunc('day', now())
        """,
        channel_name,
    )
    if used_today + UPLOAD_COST > DAILY_QUOTA_LIMIT:
        raise RuntimeError("Quota limit reached")


async def _log_usage(channel_name: str, cost: int, action: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO api_usage_log (channel_name, cost, action) VALUES ($1, $2, $3)",
        channel_name,
        cost,
        action,
    )


async def upload_short(
    video_path: str,
    title: str,
    description: str,
    channel_name: str = CHANNEL_NAME,
) -> str:
    """쇼츠 업로드. 할당량 초과 시 RuntimeError('Quota limit reached')로 즉시 중단."""
    await _check_quota(channel_name)
    creds = await _load_credentials(channel_name)

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "22"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")

    try:
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("Upload progress: %d%%", int(status.progress() * 100))

        video_id = response["id"]
        await _log_usage(channel_name, UPLOAD_COST, "upload")
        logger.info("Uploaded video id=%s", video_id)
        return video_id
    finally:
        # MediaFileUpload가 내부적으로 열어둔 파일 핸들을 명시적으로 닫아 fd 누수를 막는다.
        fd = getattr(media, "_fd", None)
        if fd is not None:
            try:
                fd.close()
            except Exception:
                logger.debug("media file handle close failed", exc_info=True)
