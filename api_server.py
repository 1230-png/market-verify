"""FastAPI 서버 — 파이프라인을 HTTP 로 트리거한다.

렌더링은 수 분이 걸리므로 요청을 즉시 반환하고 백그라운드 스레드에서 실행한다.
진행 상황은 job id 로 조회한다.

실행::

    uvicorn api_server:app --reload --port 8000
    python api_server.py                     # 위와 동일 (직접 실행용)

문서: http://127.0.0.1:8000/docs

주요 엔드포인트::

    GET  /health              환경 점검 (FFmpeg 경로, 키 설정 여부)
    POST /jobs                파이프라인 실행 시작 → job_id 반환
    GET  /jobs                작업 목록
    GET  /jobs/{job_id}       상태 · 진행 로그
    GET  /jobs/{job_id}/video 완성된 mp4 내려받기
"""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import config

PHASE = "API"

app = FastAPI(
    title="유튜브 쇼츠 자동화 파이프라인",
    description="암호화폐 뉴스 → 대본 → TTS → 영상 렌더링 → 유튜브 업로드",
    version="1.0.0",
)

# 렌더링은 CPU 를 많이 쓰므로 한 번에 하나만 돌린다.
_job_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_guard = threading.Lock()


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
class JobRequest(BaseModel):
    provider: Literal["openai", "gemini", "none"] | None = Field(
        default=None, description="대본 생성 LLM. 생략하면 .env 의 LLM_PROVIDER"
    )
    query: str | None = Field(default=None, description="Pexels 검색 키워드")
    upload: bool = Field(default=False, description="렌더링 후 유튜브 업로드 여부")
    privacy: Literal["private", "unlisted", "public"] = "private"
    skip_script: bool = Field(default=False, description="기존 script.txt 재사용")
    skip_tts: bool = Field(default=False, description="기존 audio.mp3 재사용")
    download: bool = Field(default=True, description="False 면 받아둔 Pexels 클립 재사용")


class JobSummary(BaseModel):
    job_id: str
    status: str
    phase: str
    created_at: str
    finished_at: str | None = None
    video_path: str | None = None
    video_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 작업 관리
# ---------------------------------------------------------------------------
def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_guard:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "phase": "대기",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "video_path": None,
            "video_id": None,
            "error": None,
            "logs": [],
        }
    return job_id


def _update(job_id: str, **fields) -> None:
    with _jobs_guard:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_pipeline(job_id: str, req: JobRequest) -> None:
    """백그라운드 스레드에서 파이프라인 전체를 실행한다."""

    def sink(phase: str, message: str) -> None:
        with _jobs_guard:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["logs"].append(f"[{phase}] {message}")
            # API 자체 로그는 파이프라인 단계가 아니므로 phase 를 덮어쓰지 않는다.
            if phase != PHASE:
                job["phase"] = phase

    config.add_log_sink(sink)
    _update(job_id, status="running", phase="시작")

    try:
        config.ensure_dirs()
        config.bootstrap_ffmpeg()

        # ---- Phase 1 ----
        if req.skip_script:
            config.log("Phase 1", "건너뜀 — 기존 script.txt 사용")
        else:
            import script_maker

            # 최근 3개월 안에 다룬 주제는 collect_articles 가 걸러낸다.
            articles = script_maker.collect_articles(config.MAX_HEADLINES)
            if not articles:
                raise RuntimeError(
                    "쓸 수 있는 새 주제가 없습니다. 피드가 갱신되지 않았거나 최근 주제와 모두 중복입니다."
                )

            provider = req.provider or config.LLM_PROVIDER
            if provider == "openai" and not config.OPENAI_API_KEY:
                provider = "none"
            if provider == "gemini" and not config.GEMINI_API_KEY:
                provider = "none"
            script_maker.save(script_maker.make_script(articles, provider), articles)

        # ---- Phase 2 ----
        if req.skip_tts:
            config.log("Phase 2", "건너뜀 — 기존 audio.mp3 사용")
        else:
            import tts_generator

            tts_generator.synthesize()

        # ---- Phase 3 ----
        import video_renderer

        video_path = video_renderer.render(query=req.query, download=req.download)
        _update(job_id, video_path=video_path)

        # ---- Phase 4 ----
        video_id = None
        if req.upload:
            import youtube_uploader

            video_id = youtube_uploader.upload(privacy=req.privacy)

        _update(
            job_id,
            status="succeeded",
            phase="완료",
            video_id=video_id,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        config.log(PHASE, f"작업 {job_id} 완료")

    except Exception as exc:
        config.log(PHASE, f"작업 {job_id} 실패 ({type(exc).__name__}): {exc}")
        _update(
            job_id,
            status="failed",
            phase="실패",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        # 서버 콘솔에는 전체 트레이스백을 남겨 원인 추적을 돕는다.
        traceback.print_exc()
    finally:
        config.remove_log_sink(sink)
        _job_lock.release()


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------
@app.get("/health", summary="환경 점검")
def health() -> dict:
    """FFmpeg 경로와 키 설정 여부를 확인한다. 설치 직후 가장 먼저 호출해 보세요."""
    try:
        ffmpeg = config.bootstrap_ffmpeg()
    except Exception as exc:
        ffmpeg = f"확인 실패: {exc}"

    try:
        font = config.find_korean_font()
    except Exception as exc:
        font = f"확인 실패: {exc}"

    return {
        "status": "ok",
        "ffmpeg": ffmpeg,
        "subtitle_font": font,
        "keys": {
            "openai": bool(config.OPENAI_API_KEY),
            "gemini": bool(config.GEMINI_API_KEY),
            "pexels": bool(config.PEXELS_API_KEY),
            "youtube_client_secrets": config.CLIENT_SECRETS_PATH.exists(),
            "youtube_token": config.TOKEN_PATH.exists(),
        },
        "busy": _job_lock.locked(),
    }


@app.post("/jobs", response_model=JobSummary, status_code=202, summary="파이프라인 실행")
def create_job(req: JobRequest) -> dict:
    """파이프라인을 백그라운드로 시작하고 job_id 를 즉시 돌려준다."""
    # 렌더링은 동시에 하나만. 이미 돌고 있으면 409 로 거절한다.
    if not _job_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="이미 실행 중인 작업이 있습니다. 완료 후 다시 요청하세요.")

    job_id = _new_job()
    threading.Thread(target=_run_pipeline, args=(job_id, req), daemon=True).start()
    config.log(PHASE, f"작업 {job_id} 시작")

    with _jobs_guard:
        return {k: v for k, v in _jobs[job_id].items() if k != "logs"}


@app.get("/jobs", summary="작업 목록")
def list_jobs() -> list[dict]:
    with _jobs_guard:
        jobs = [{k: v for k, v in j.items() if k != "logs"} for j in _jobs.values()]
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


@app.get("/jobs/{job_id}", summary="작업 상태와 로그")
def get_job(job_id: str) -> dict:
    with _jobs_guard:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"작업 {job_id} 를 찾을 수 없습니다.")
        return dict(job)


@app.get("/jobs/{job_id}/video", summary="완성 영상 내려받기")
def get_job_video(job_id: str):
    with _jobs_guard:
        job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"작업 {job_id} 를 찾을 수 없습니다.")
    if job["status"] != "succeeded" or not job["video_path"]:
        raise HTTPException(status_code=409, detail=f"아직 영상이 없습니다 (상태: {job['status']}).")

    from pathlib import Path

    path = Path(job["video_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="영상 파일이 삭제되었습니다.")

    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


if __name__ == "__main__":
    import uvicorn

    config.log(PHASE, "http://127.0.0.1:8000/docs 에서 API 문서를 볼 수 있습니다.")
    uvicorn.run(app, host="127.0.0.1", port=8000)
