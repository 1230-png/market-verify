"""매주 1회 파이프라인을 자동 실행하는 스케줄러 데몬.

기본값은 **매주 일요일 21:00** 에 api_server.py 의 ``POST /jobs`` 를 호출하고,
작업이 끝날 때까지 상태를 지켜본 뒤 결과를 로그에 남긴다.

전제: api_server.py 가 먼저 떠 있어야 한다.

    uvicorn api_server:app --port 8000     # 터미널 1
    python scheduler.py                    # 터미널 2

옵션::

    python scheduler.py --run-now          # 스케줄 무시하고 즉시 1회 실행 후 종료
    python scheduler.py --day saturday --time 21:30
    python scheduler.py --no-upload        # 렌더링만 하고 업로드는 생략

로그는 콘솔과 ``logs/scheduler.log`` 에 함께 쌓인다.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "scheduler.log"

# API 호출 재시도
MAX_RETRIES = 4
RETRY_BACKOFF = 5          # 초. 시도마다 2배씩 늘어난다 (5 → 10 → 20 → 40)

# 작업 완료 대기
POLL_INTERVAL = 20         # 초
MAX_WAIT = 60 * 60         # 렌더링 + 업로드에 한 시간이면 충분하다

VALID_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

log = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = True) -> None:
    """콘솔과 회전 파일에 동시에 기록한다.

    pythonw 로 띄우면 콘솔이 없으므로 파일 로그가 유일한 단서가 된다.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # 5MB 씩 3개까지 보관 — 몇 년을 돌려도 디스크를 잠식하지 않는다.
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    if verbose and sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        log.addHandler(stream)


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------
def _api(path: str) -> str:
    return f"{config.API_BASE_URL.rstrip('/')}{path}"


def wait_for_server(timeout: int = 60) -> bool:
    """서버가 응답할 때까지 기다린다. PC 부팅 직후 동시에 뜨는 경우를 위해."""
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(_api("/health"), timeout=10, proxies={"http": "", "https": ""})
            if resp.ok:
                body = resp.json()
                log.info("API 서버 확인 — busy=%s, ffmpeg=%s", body.get("busy"), body.get("ffmpeg"))
                return True
        except Exception as exc:
            log.debug("서버 대기 중: %s", exc)
        time.sleep(5)

    log.error("API 서버(%s)에 연결할 수 없습니다. uvicorn 이 떠 있는지 확인하세요.", config.API_BASE_URL)
    return False


def start_job(payload: dict) -> str | None:
    """POST /jobs 를 재시도와 함께 호출하고 job_id 를 돌려준다."""
    import requests

    delay = RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                _api("/jobs"), json=payload, timeout=30, proxies={"http": "", "https": ""}
            )

            if resp.status_code == 202:
                job_id = resp.json()["job_id"]
                log.info("작업 시작됨 — job_id=%s", job_id)
                return job_id

            if resp.status_code == 409:
                # 이미 다른 작업이 도는 중. 재시도해도 같은 결과이므로 포기한다.
                log.warning("이미 실행 중인 작업이 있어 이번 주는 건너뜁니다. (409)")
                return None

            log.error("예상 못 한 응답 %s: %s", resp.status_code, resp.text[:300])

        except Exception as exc:
            log.error("API 호출 실패 (%s/%s) %s: %s", attempt, MAX_RETRIES, type(exc).__name__, exc)

        if attempt < MAX_RETRIES:
            log.info("%d초 후 재시도합니다.", delay)
            time.sleep(delay)
            delay *= 2

    log.error("재시도 %d회를 모두 실패했습니다. 이번 회차를 포기합니다.", MAX_RETRIES)
    return None


def wait_for_job(job_id: str) -> bool:
    """작업이 끝날 때까지 상태를 지켜본다. 성공하면 True."""
    import requests

    deadline = time.time() + MAX_WAIT
    last_phase = ""

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp = requests.get(
                _api(f"/jobs/{job_id}"), timeout=30, proxies={"http": "", "https": ""}
            )
            resp.raise_for_status()
            job = resp.json()
        except Exception as exc:
            # 폴링 실패는 치명적이지 않다. 작업은 서버에서 계속 돌고 있다.
            log.warning("상태 조회 실패 (%s). 계속 기다립니다.", exc)
            continue

        if job["phase"] != last_phase:
            last_phase = job["phase"]
            log.info("진행 상황: %s", last_phase)

        if job["status"] == "succeeded":
            log.info("작업 완료 — 영상: %s", job.get("video_path"))
            if job.get("video_id"):
                log.info("업로드됨 → https://www.youtube.com/watch?v=%s", job["video_id"])
            else:
                log.info("업로드는 수행되지 않았습니다 (upload=false 이거나 인증 없음).")
            return True

        if job["status"] == "failed":
            log.error("작업 실패: %s", job.get("error"))
            for line in job.get("logs", [])[-10:]:
                log.error("  %s", line)
            return False

    log.error("작업이 %d분 안에 끝나지 않아 대기를 중단합니다. (job_id=%s)", MAX_WAIT // 60, job_id)
    return False


# ---------------------------------------------------------------------------
# 주간 실행
# ---------------------------------------------------------------------------
def run_weekly(payload: dict) -> bool:
    """스케줄이 발동할 때 실행되는 본체."""
    log.info("=" * 70)
    log.info("주간 자동 실행 시작")

    if not wait_for_server():
        return False

    job_id = start_job(payload)
    if not job_id:
        return False

    ok = wait_for_job(job_id)
    log.info("주간 자동 실행 %s", "성공" if ok else "실패")
    log.info("=" * 70)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="매주 1회 파이프라인 자동 실행")
    parser.add_argument("--day", choices=VALID_DAYS, default=config.SCHEDULE_DAY, help="실행 요일")
    parser.add_argument("--time", dest="at", default=config.SCHEDULE_TIME, help="실행 시각 HH:MM (24시간)")
    parser.add_argument("--run-now", action="store_true", help="스케줄 무시하고 즉시 1회 실행 후 종료")
    parser.add_argument("--no-upload", action="store_true", help="렌더링만 하고 업로드 생략")
    parser.add_argument("--query", default=config.PEXELS_QUERY, help="Pexels 검색 키워드")
    parser.add_argument("--quiet", action="store_true", help="콘솔 출력 없이 파일에만 기록")
    args = parser.parse_args()

    setup_logging(verbose=not args.quiet)

    payload = {
        "upload": not args.no_upload,
        "privacy": config.YOUTUBE_PRIVACY,
        "query": args.query,
    }

    if args.run_now:
        log.info("--run-now: 즉시 1회 실행합니다.")
        return 0 if run_weekly(payload) else 1

    import schedule

    if not _valid_time(args.at):
        log.error("시각 형식이 잘못되었습니다: %s (예: 21:00)", args.at)
        return 1

    getattr(schedule.every(), args.day).at(args.at).do(run_weekly, payload)

    log.info("스케줄러 시작 — 매주 %s %s 에 실행합니다.", args.day, args.at)
    log.info("대상 API: %s", config.API_BASE_URL)
    log.info("업로드: %s (%s)", payload["upload"], payload["privacy"])
    log.info("로그 파일: %s", LOG_PATH)

    next_run = schedule.next_run()
    if next_run:
        log.info("다음 실행 예정: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("스케줄러를 종료합니다.")
            return 0
        except Exception as exc:
            # 루프가 죽으면 자동화 전체가 멈추므로 어떤 예외도 삼키고 계속 돈다.
            log.exception("스케줄 루프 오류 (%s). 계속 실행합니다.", type(exc).__name__)
            time.sleep(60)


def _valid_time(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


if __name__ == "__main__":
    sys.exit(main())
