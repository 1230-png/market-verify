"""Phase 4 — 완성된 쇼츠를 유튜브에 비공개로 업로드.

Google OAuth 2.0 설치형 앱 흐름을 쓴다. 최초 1회만 브라우저가 열려 계정 승인을
요청하고, 발급된 토큰은 token.json 에 저장돼 다음부터는 자동 로그인된다.

준비물
  1. Google Cloud Console 에서 프로젝트 생성 → YouTube Data API v3 사용 설정
  2. 'OAuth 클라이언트 ID' → 애플리케이션 유형 **데스크톱 앱** 으로 생성
  3. 내려받은 JSON 을 이 폴더에 `client_secrets.json` 이름으로 저장

실행::

    python youtube_uploader.py
    python youtube_uploader.py --privacy unlisted
    python youtube_uploader.py --title "직접 지정한 제목" --video output/final_video.mp4
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import config

PHASE = "Phase 4"

# 업로드 전용 스코프. 채널의 다른 정보는 건드리지 않는다.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
def get_credentials():
    """token.json 을 재사용하고, 없거나 만료되면 로컬 브라우저로 인증한다."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if config.TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), SCOPES)
        except Exception as exc:
            config.log(PHASE, f"저장된 토큰을 읽지 못했습니다({exc}). 다시 인증합니다.")
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        config.log(PHASE, "토큰 갱신 중…")
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as exc:
            config.log(PHASE, f"토큰 갱신 실패({exc}). 처음부터 인증합니다.")

    if not config.CLIENT_SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"{config.CLIENT_SECRETS_PATH} 가 없습니다.\n"
            "  Google Cloud Console → API 및 서비스 → 사용자 인증 정보 →\n"
            "  'OAuth 클라이언트 ID' 를 **데스크톱 앱** 유형으로 만들고 JSON 을 내려받아\n"
            f"  {config.CLIENT_SECRETS_PATH} 로 저장하세요."
        )

    config.log(PHASE, "브라우저에서 Google 계정 승인을 진행하세요…")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.CLIENT_SECRETS_PATH), SCOPES)
    # port=0 이면 비어 있는 포트를 자동으로 골라 로컬 리디렉션 서버를 띄운다.
    creds = flow.run_local_server(port=0, prompt="consent", authorization_prompt_message="")
    _save_token(creds)
    config.log(PHASE, f"인증 완료 — 토큰 저장: {config.TOKEN_PATH}")
    return creds


def _save_token(creds) -> None:
    config.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# 제목 / 설명 구성
# ---------------------------------------------------------------------------
def build_metadata(title: str | None = None, description: str | None = None) -> dict:
    """script_meta.json + script.txt 를 바탕으로 제목/설명을 자동 작성한다."""
    meta = {}
    if config.META_PATH.exists():
        try:
            meta = json.loads(config.META_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config.log(PHASE, "script_meta.json 파싱 실패 — 대본에서 제목을 만듭니다.")

    script = ""
    if config.SCRIPT_PATH.exists():
        script = config.SCRIPT_PATH.read_text(encoding="utf-8").strip()

    final_title = title or meta.get("title") or (script.split("\n")[0] if script else "암호화폐 시황")
    final_desc = description or meta.get("description") or ""

    body_lines = [final_desc.strip(), ""] if final_desc.strip() else []
    if script:
        body_lines += ["[오늘의 대본]", script, ""]
    sources = meta.get("sources") or []
    if sources:
        body_lines.append("[참고 기사]")
        body_lines += [f"- {s.get('title','')} {s.get('link','')}".strip() for s in sources[:5]]
        body_lines.append("")
    body_lines.append("#Shorts " + " ".join(f"#{t}" for t in config.YOUTUBE_TAGS))

    # YouTube 는 제목/설명에 '<' 와 '>' 를 허용하지 않는다.
    def clean(text: str) -> str:
        return text.replace("<", "(").replace(">", ")")

    return {
        "title": clean(final_title)[:100],
        "description": clean("\n".join(body_lines))[:4900],
        "tags": config.YOUTUBE_TAGS[:20],
    }


# ---------------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------------
def upload(
    video_path: str | Path | None = None,
    title: str | None = None,
    description: str | None = None,
    privacy: str | None = None,
) -> str:
    """영상을 업로드하고 videoId 를 돌려준다."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    path = Path(video_path) if video_path else config.VIDEO_PATH
    if not path.exists():
        raise FileNotFoundError(f"{path} 가 없습니다. 먼저 video_renderer.py 를 실행하세요.")

    meta = build_metadata(title, description)
    privacy = privacy or config.YOUTUBE_PRIVACY

    config.log(PHASE, f"업로드 대상: {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
    config.log(PHASE, f"제목: {meta['title']}")
    config.log(PHASE, f"공개 설정: {privacy}")

    youtube = build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": config.YOUTUBE_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": privacy,           # private / unlisted / public
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    error_count = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                config.log(PHASE, f"업로드 {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in RETRIABLE_STATUS_CODES:
                error_count += 1
                if error_count > MAX_RETRIES:
                    raise
                sleep_for = random.uniform(1, 2 ** error_count)
                config.log(PHASE, f"일시적 오류 {exc.resp.status} — {sleep_for:.1f}초 후 재시도")
                time.sleep(sleep_for)
                continue
            raise

    video_id = response["id"]
    config.log(PHASE, f"업로드 완료 → https://www.youtube.com/watch?v={video_id}")
    config.log(PHASE, "YouTube 스튜디오에서 처리가 끝나면 쇼츠로 노출됩니다 (세로 60초 미만).")
    return video_id


def main() -> int:
    parser = argparse.ArgumentParser(description="완성 영상 → 유튜브 업로드")
    parser.add_argument("--video", default=None, help=f"기본값: {config.VIDEO_PATH}")
    parser.add_argument("--title", default=None)
    parser.add_argument("--description", default=None)
    parser.add_argument(
        "--privacy",
        choices=["private", "unlisted", "public"],
        default=config.YOUTUBE_PRIVACY,
    )
    args = parser.parse_args()

    try:
        upload(args.video, args.title, args.description, args.privacy)
    except FileNotFoundError as exc:
        config.log(PHASE, f"오류: {exc}")
        return 1
    except Exception as exc:
        config.log(PHASE, f"업로드 실패 ({type(exc).__name__}): {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
