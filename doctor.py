"""환경 진단 — 파이프라인을 돌리기 전에 무엇이 준비됐고 무엇이 빠졌는지 한 번에 확인한다.

    python doctor.py            # 네트워크 호출 없이 설정만 점검 (빠름)
    python doctor.py --online   # RSS·TTS·Pexels 에 실제로 접속해 확인
    python doctor.py --online --llm   # LLM 까지 실제 호출 (토큰이 아주 조금 소모됨)

각 항목은 OK / 경고 / 실패 로 표시된다.
  OK    문제 없음
  경고  없어도 파이프라인은 돌아감 (대체 경로가 있음)
  실패  이 상태로는 해당 단계가 실패함 — 고쳐야 함
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import config

OK, WARN, FAIL = "OK", "경고", "실패"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _try(name: str, fn, hint_on_fail: str = "", optional: bool = False) -> Result:
    """검사 하나를 실행하고 예외를 결과로 바꾼다."""
    try:
        detail = fn()
        return Result(name, OK, detail or "")
    except Exception as exc:
        # 예외 메시지가 길면(스택 포함 URL 등) 표가 읽기 어려워진다. 한 줄로 줄인다.
        message = " ".join(str(exc).split())
        if len(message) > 70:
            message = message[:70] + "…"
        return Result(name, WARN if optional else FAIL, f"{type(exc).__name__}: {message}", hint_on_fail)


# ---------------------------------------------------------------------------
# 기본 환경
# ---------------------------------------------------------------------------
def check_ffmpeg() -> Result:
    return _try(
        "FFmpeg (내장)",
        lambda: config.bootstrap_ffmpeg(),
        "pip install -r requirements.txt 를 다시 실행하세요.",
    )


def check_font() -> Result:
    return _try(
        "한글 자막 폰트",
        lambda: config.find_korean_font(),
        r".env 에 SUBTITLE_FONT=C:\Windows\Fonts\malgun.ttf 를 추가하세요.",
    )


def check_dirs() -> Result:
    def run() -> str:
        config.ensure_dirs()
        probe = config.OUTPUT_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return f"{config.ASSETS_DIR.name}/, {config.OUTPUT_DIR.name}/ 쓰기 가능"

    return _try("작업 폴더", run, "폴더 권한을 확인하거나 다른 위치에서 실행하세요.")


# ---------------------------------------------------------------------------
# Phase 1 — 뉴스 수집과 대본
# ---------------------------------------------------------------------------
def check_feeds(online: bool) -> list[Result]:
    if not config.FEED_URLS:
        return [Result("RSS 피드", FAIL, "FEED_URLS 가 비어 있음", ".env 의 FEED_URLS 를 채우세요.")]

    if not online:
        return [Result("RSS 피드", WARN, f"{len(config.FEED_URLS)}개 설정됨 (--online 으로 실제 확인)")]

    import feedparser
    import requests

    results = []
    alive = 0
    for url in config.FEED_URLS:
        short = url.replace("https://", "").replace("http://", "")[:46]
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.entries:
                alive += 1
                newest = parsed.entries[0].get("title", "")[:44]
                results.append(Result(f"  피드 {short}", OK, f"{len(parsed.entries)}건 · 최신: {newest}"))
            else:
                results.append(
                    Result(f"  피드 {short}", FAIL, "항목 0건", "피드 주소가 바뀌었을 수 있습니다.")
                )
        except Exception as exc:
            results.append(
                Result(f"  피드 {short}", FAIL, f"{type(exc).__name__}", "주소 확인 또는 다른 매체로 교체하세요.")
            )

    summary = Result(
        "RSS 피드",
        OK if alive else FAIL,
        f"{alive}/{len(config.FEED_URLS)}개 살아있음",
        "" if alive else "살아있는 피드가 없습니다. .env 의 FEED_URLS 를 교체하세요.",
    )
    return [summary, *results]


def check_llm(online: bool) -> Result:
    provider = config.LLM_PROVIDER
    key = config.OPENAI_API_KEY if provider == "openai" else config.GEMINI_API_KEY
    model = config.OPENAI_MODEL if provider == "openai" else config.GEMINI_MODEL

    if provider == "none":
        return Result("LLM 대본", WARN, "LLM_PROVIDER=none — 규칙 기반 폴백 대본 사용",
                      "품질을 위해 OpenAI 또는 Gemini 키를 설정하는 편이 좋습니다.")
    if not key:
        return Result("LLM 대본", WARN, f"{provider} 키 없음 — 규칙 기반 폴백으로 자동 전환",
                      f".env 에 {'OPENAI_API_KEY' if provider=='openai' else 'GEMINI_API_KEY'} 를 넣으세요.")
    if not online:
        return Result("LLM 대본", OK, f"{provider} / {model} (키 있음, --llm 으로 실제 호출 확인)")

    def run() -> str:
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "핑"}], max_tokens=5
            )
            return f"{model} 응답 확인 ({resp.choices[0].message.content!r})"

        from google import genai

        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=model, contents="핑")
        return f"{model} 응답 확인 ({(resp.text or '')[:20]!r})"

    return _try("LLM 대본", run, f"모델명({model})이 유효한지, 키에 크레딧이 있는지 확인하세요.")


def check_history() -> Result:
    import json

    if not config.HISTORY_PATH.exists():
        return Result("주제 히스토리", OK, "아직 없음 (첫 실행에서 생성됨)")
    try:
        entries = json.loads(config.HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Result("주제 히스토리", WARN, "파일이 손상됨", "assets/history.json 을 삭제하면 새로 시작합니다.")

    import script_maker

    recent = script_maker._recent(entries, config.HISTORY_DAYS)
    return Result(
        "주제 히스토리",
        OK,
        f"총 {len(entries)}건 · 최근 {config.HISTORY_DAYS}일 내 {len(recent)}건이 중복 필터에 사용됨",
    )


# ---------------------------------------------------------------------------
# Phase 2 — TTS
# ---------------------------------------------------------------------------
def check_tts(online: bool) -> Result:
    if not online:
        return Result("TTS 음성", WARN, f"{config.TTS_VOICE} (--online 으로 실제 확인)")

    def run() -> str:
        import tts_generator

        voices = tts_generator._run_async(tts_generator.list_korean_voices())
        names = {v["ShortName"] for v in voices}
        if config.TTS_VOICE in names:
            return f"{config.TTS_VOICE} 사용 가능 (한국어 음성 {len(voices)}개)"
        males = [v["ShortName"] for v in voices if v.get("Gender") == "Male"]
        return f"{config.TTS_VOICE} 없음 → 실행 시 {males[0] if males else '?'} 로 대체됨"

    return _try("TTS 음성", run, "인터넷 연결과 방화벽을 확인하세요. edge-tts 는 MS 서버가 필요합니다.")


# ---------------------------------------------------------------------------
# Phase 3 — 영상
# ---------------------------------------------------------------------------
def check_pexels(online: bool) -> Result:
    if not config.PEXELS_API_KEY:
        return Result("Pexels 영상", WARN, "키 없음 — 그라데이션 배경으로 대체됨",
                      "https://www.pexels.com/api/ 에서 무료로 발급받으세요.")
    if not online:
        return Result("Pexels 영상", OK, "키 있음 (--online 으로 실제 확인)")

    def run() -> str:
        import video_renderer

        videos = video_renderer.search_pexels_videos(config.PEXELS_QUERY, config.PEXELS_CLIP_COUNT)
        sizes = ", ".join(f"{v['width']}x{v['height']}" for v in videos)
        return f"'{config.PEXELS_QUERY}' 세로 영상 {len(videos)}개 확보 ({sizes})"

    return _try("Pexels 영상", run, "키가 맞는지, 검색어를 바꿔야 하는지 확인하세요.")


# ---------------------------------------------------------------------------
# Phase 4 — 업로드
# ---------------------------------------------------------------------------
def check_youtube() -> list[Result]:
    results = []

    if config.CLIENT_SECRETS_PATH.exists():
        results.append(Result("client_secrets.json", OK, "있음"))
    else:
        results.append(
            Result("client_secrets.json", FAIL, "없음",
                   "Google Cloud Console 에서 '데스크톱 앱' OAuth 클라이언트를 만들어 저장하세요.")
        )

    if config.TOKEN_PATH.exists():
        results.append(Result("token.json (인증)", OK, "인증 완료 — 백그라운드 업로드 가능"))
    else:
        results.append(
            Result("token.json (인증)", FAIL, "없음 — 백그라운드 실행이 인증에서 멈춥니다",
                   "터미널에서 `python youtube_uploader.py` 를 1회 실행해 브라우저 승인을 마치세요.")
        )

    results.append(Result("업로드 공개 설정", OK, config.YOUTUBE_PRIVACY))
    return results


# ---------------------------------------------------------------------------
# 스케줄러
# ---------------------------------------------------------------------------
def check_schedule() -> Result:
    import scheduler

    if not scheduler._valid_time(config.SCHEDULE_TIME):
        return Result("주간 스케줄", FAIL, f"시각 형식 오류: {config.SCHEDULE_TIME}",
                      ".env 의 SCHEDULE_TIME 을 HH:MM 형식으로 고치세요.")
    if config.SCHEDULE_DAY not in scheduler.VALID_DAYS:
        return Result("주간 스케줄", FAIL, f"요일 오류: {config.SCHEDULE_DAY}",
                      f"monday~sunday 중 하나여야 합니다.")

    import schedule as sched

    sched.clear()
    getattr(sched.every(), config.SCHEDULE_DAY).at(config.SCHEDULE_TIME).do(lambda: None)
    nxt = sched.next_run()
    sched.clear()
    return Result("주간 스케줄", OK,
                  f"매주 {config.SCHEDULE_DAY} {config.SCHEDULE_TIME} · 다음 실행 {nxt:%Y-%m-%d %H:%M}")


def check_api_server() -> Result:
    def run() -> str:
        import requests

        resp = requests.get(f"{config.API_BASE_URL.rstrip('/')}/health", timeout=5,
                            proxies={"http": "", "https": ""})
        resp.raise_for_status()
        return f"{config.API_BASE_URL} 응답함 (busy={resp.json().get('busy')})"

    return _try("API 서버", run,
                "스케줄러가 호출할 서버입니다. `python api_server.py` 로 띄우세요.", optional=True)


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def render(results: list[Result]) -> int:
    mark = {OK: "[ OK ]", WARN: "[경고]", FAIL: "[실패]"}
    width = max(len(r.name) for r in results) + 2

    print()
    for r in results:
        print(f"{mark[r.status]} {r.name.ljust(width)} {r.detail}")
        if r.hint and r.status != OK:
            print(f"{' ' * 7}{' ' * width} → {r.hint}")

    fails = [r for r in results if r.status == FAIL]
    warns = [r for r in results if r.status == WARN]

    print("\n" + "-" * 72)
    if fails:
        print(f"실패 {len(fails)}건, 경고 {len(warns)}건 — 위의 → 표시를 따라 고친 뒤 다시 실행하세요.")
    elif warns:
        print(f"경고 {len(warns)}건 — 파이프라인은 돌아가지만 대체 경로가 쓰입니다.")
    else:
        print("모든 검사 통과. `python run_pipeline.py` 로 첫 영상을 만들어 보세요.")
    print("-" * 72)
    return 1 if fails else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="파이프라인 환경 진단")
    parser.add_argument("--online", action="store_true", help="RSS·TTS·Pexels 에 실제 접속해 확인")
    parser.add_argument("--llm", action="store_true", help="LLM 도 실제로 호출 (토큰 소량 소모)")
    args = parser.parse_args()

    print("=" * 72)
    print("유튜브 쇼츠 자동화 파이프라인 — 환경 진단")
    print("=" * 72)

    results: list[Result] = [
        check_ffmpeg(),
        check_font(),
        check_dirs(),
        *check_feeds(args.online),
        check_llm(args.online and args.llm),
        check_history(),
        check_tts(args.online),
        check_pexels(args.online),
        *check_youtube(),
        check_schedule(),
        check_api_server(),
    ]
    return render(results)


if __name__ == "__main__":
    sys.exit(main())
