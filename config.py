"""공통 설정 · 경로 · FFmpeg 부트스트랩.

[가장 중요한 역할]
MoviePy 가 사용할 FFmpeg 를 `imageio-ffmpeg` 가 배포하는 **내장 바이너리**로 고정한다.
따라서 Windows 에서 FFmpeg 를 따로 내려받거나 Path 환경변수를 건드릴 필요가 전혀 없다.

MoviePy 를 쓰는 모듈은 반드시 아래 순서를 지킬 것::

    import config                      # 1) 먼저 import (여기서 FFmpeg 경로가 확정된다)
    config.bootstrap_ffmpeg()
    from moviepy import VideoFileClip  # 2) 그 다음에 moviepy import
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# .env 로딩 (없어도 그냥 넘어간다)
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv 미설치 환경
    load_dotenv = None

# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
OUTPUT_DIR = BASE_DIR / "output"
CLIPS_DIR = ASSETS_DIR / "clips"

SCRIPT_PATH = ASSETS_DIR / "script.txt"        # Phase 1 결과물
META_PATH = ASSETS_DIR / "script_meta.json"    # Phase 1 → Phase 4 로 넘기는 제목/설명
HISTORY_PATH = ASSETS_DIR / "history.json"     # 다룬 주제 기록 (중복 방지)
AUDIO_PATH = ASSETS_DIR / "audio.mp3"          # Phase 2 결과물
VIDEO_PATH = OUTPUT_DIR / "final_video.mp4"    # Phase 3 결과물

CLIENT_SECRETS_PATH = BASE_DIR / "client_secrets.json"  # Phase 4 OAuth
TOKEN_PATH = BASE_DIR / "token.json"                    # Phase 4 토큰 캐시

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def ensure_dirs() -> None:
    """생성물이 들어갈 디렉터리를 만든다."""
    for directory in (ASSETS_DIR, OUTPUT_DIR, CLIPS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 환경변수 헬퍼
# --------------------------------------------------------------------------
def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Phase 1 · 대본
# --------------------------------------------------------------------------
DEFAULT_FEEDS = ",".join(
    [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://kr.investing.com/rss/news_301.rss",
    ]
)
FEED_URLS = [u.strip() for u in env("FEED_URLS", DEFAULT_FEEDS).split(",") if u.strip()]
MAX_HEADLINES = env_int("MAX_HEADLINES", 8)

# 주제 중복 방지: 최근 며칠 안에 다룬 소재를 제외할지 (기본 3개월)
HISTORY_DAYS = env_int("HISTORY_DAYS", 90)
# 제목 키워드가 이 자카드 유사도 이상 겹치면 같은 사건으로 본다 (0~1)
HISTORY_SIMILARITY = float(env("HISTORY_SIMILARITY", "0.6"))
# history.json 보존 기간. 이보다 오래된 항목은 정리한다.
HISTORY_RETENTION_DAYS = env_int("HISTORY_RETENTION_DAYS", 365)

LLM_PROVIDER = env("LLM_PROVIDER", "openai").lower()   # openai | gemini | none
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")

# 1분 미만 쇼츠. 한국어 TTS 는 대략 분당 330~380자 → 55초 목표로 300자 안팎.
TARGET_CHARS = env_int("TARGET_CHARS", 300)
MAX_VIDEO_SECONDS = env_int("MAX_VIDEO_SECONDS", 58)

# --------------------------------------------------------------------------
# Phase 2 · TTS
# --------------------------------------------------------------------------
TTS_VOICE = env("TTS_VOICE", "ko-KR-InJoonNeural")  # 한국어 남성
TTS_RATE = env("TTS_RATE", "+8%")
TTS_VOLUME = env("TTS_VOLUME", "+0%")

# --------------------------------------------------------------------------
# Phase 3 · 영상
# --------------------------------------------------------------------------
PEXELS_API_KEY = env("PEXELS_API_KEY")
PEXELS_QUERY = env("PEXELS_QUERY", "bitcoin")
PEXELS_CLIP_COUNT = env_int("PEXELS_CLIP_COUNT", 3)

VIDEO_W = env_int("VIDEO_W", 1080)
VIDEO_H = env_int("VIDEO_H", 1920)
VIDEO_FPS = env_int("VIDEO_FPS", 30)

SUBTITLE_FONT_SIZE = env_int("SUBTITLE_FONT_SIZE", 62)
SUBTITLE_BOTTOM_MARGIN = env_int("SUBTITLE_BOTTOM_MARGIN", 320)

# --------------------------------------------------------------------------
# Phase 4 · 업로드
# --------------------------------------------------------------------------
YOUTUBE_PRIVACY = env("YOUTUBE_PRIVACY", "private")
YOUTUBE_CATEGORY_ID = env("YOUTUBE_CATEGORY_ID", "25")  # 25 = News & Politics
YOUTUBE_TAGS = [t.strip() for t in env("YOUTUBE_TAGS", "비트코인,암호화폐,코인뉴스,shorts").split(",") if t.strip()]

# 설명란 맨 끝에 붙는 면책 조항. 대본이 방향성을 단정하므로 항상 따라붙게 한다.
# .env 에서 YOUTUBE_DISCLAIMER 로 문구를 바꾸거나, 빈 값으로 두면 붙지 않는다.
DEFAULT_DISCLAIMER = (
    "[본 영상은 개인의 주관적인 차트 분석이며, 투자 권유나 리딩이 아닙니다. "
    "모든 투자의 책임은 본인에게 있습니다.]"
)
YOUTUBE_DISCLAIMER = os.environ.get("YOUTUBE_DISCLAIMER", DEFAULT_DISCLAIMER).strip()


# --------------------------------------------------------------------------
# 스케줄러 (scheduler.py)
# --------------------------------------------------------------------------
API_BASE_URL = env("API_BASE_URL", "http://127.0.0.1:8000")
SCHEDULE_DAY = env("SCHEDULE_DAY", "sunday").lower()   # 매주 실행 요일
SCHEDULE_TIME = env("SCHEDULE_TIME", "21:00")          # HH:MM (24시간)


# --------------------------------------------------------------------------
# FFmpeg 부트스트랩
# --------------------------------------------------------------------------
_ffmpeg_path: str | None = None


def bootstrap_ffmpeg() -> str:
    """imageio-ffmpeg 내장 FFmpeg 를 MoviePy 가 쓰도록 고정하고 경로를 돌려준다.

    MoviePy 는 import 시점에 ``FFMPEG_BINARY`` 환경변수를 읽는다.
    그래서 moviepy 를 import 하기 전에 이 함수를 호출해야 한다.
    """
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path

    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "imageio-ffmpeg 가 설치되어 있지 않습니다. `pip install -r requirements.txt` 를 먼저 실행하세요."
        ) from exc

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if not Path(exe).exists():  # pragma: no cover
        raise RuntimeError(f"내장 FFmpeg 바이너리를 찾지 못했습니다: {exe}")

    # MoviePy / imageio 양쪽이 모두 이 바이너리를 바라보게 만든다.
    os.environ["FFMPEG_BINARY"] = exe
    os.environ["IMAGEIO_FFMPEG_EXE"] = exe
    _ffmpeg_path = exe
    return exe


# --------------------------------------------------------------------------
# 한글 폰트 탐색 (자막용)
# --------------------------------------------------------------------------
_FONT_CANDIDATES = [
    # Windows 기본 탑재 폰트 — 별도 설치 불필요
    r"C:\Windows\Fonts\malgun.ttf",       # 맑은 고딕
    r"C:\Windows\Fonts\malgunbd.ttf",     # 맑은 고딕 Bold
    r"C:\Windows\Fonts\NanumGothic.ttf",
    r"C:\Windows\Fonts\gulim.ttc",
    r"C:\Windows\Fonts\batang.ttc",
    # macOS / Linux 폴백 (개발용)
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def find_korean_font() -> str:
    """자막에 쓸 한글 폰트 파일 경로를 찾는다.

    ``SUBTITLE_FONT`` 환경변수가 있으면 그것을 최우선으로 쓴다.
    """
    override = env("SUBTITLE_FONT")
    if override and Path(override).exists():
        return override

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate

    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. .env 에 SUBTITLE_FONT=<ttf 경로> 를 지정하세요.\n"
        "  (Windows 예시: SUBTITLE_FONT=C:\\Windows\\Fonts\\malgun.ttf)"
    )


# 진행 로그를 가로챌 구독자들. API 서버가 작업별 로그를 모을 때 쓴다.
_log_sinks: list = []


def add_log_sink(sink) -> None:
    """log() 호출을 함께 받아볼 콜백을 등록한다. sink(phase, message) 형태."""
    _log_sinks.append(sink)


def remove_log_sink(sink) -> None:
    if sink in _log_sinks:
        _log_sinks.remove(sink)


def log(phase: str, message: str) -> None:
    """단계 표시가 붙은 진행 로그."""
    print(f"[{phase}] {message}", flush=True)
    sys.stdout.flush()
    for sink in list(_log_sinks):
        try:
            sink(phase, message)
        except Exception:
            # 로그 구독자의 오류가 파이프라인을 멈추게 해선 안 된다.
            pass
