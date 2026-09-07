"""전체 파이프라인 실행기 — Phase 1 → 2 → 3 (→ 4).

    python run_pipeline.py                 # 대본 → TTS → 렌더링 (업로드 안 함)
    python run_pipeline.py --upload        # 렌더링 후 비공개 업로드까지
    python run_pipeline.py --skip-script   # 기존 script.txt 재사용
"""

from __future__ import annotations

import argparse
import sys
import time

import config


def main() -> int:
    parser = argparse.ArgumentParser(description="유튜브 쇼츠 자동화 파이프라인")
    parser.add_argument("--upload", action="store_true", help="렌더링 후 유튜브에 업로드")
    parser.add_argument("--skip-script", action="store_true", help="Phase 1 건너뛰기")
    parser.add_argument("--skip-tts", action="store_true", help="Phase 2 건너뛰기")
    parser.add_argument("--no-download", action="store_true", help="기존 Pexels 클립 재사용")
    parser.add_argument("--provider", choices=["openai", "gemini", "none"], default=config.LLM_PROVIDER)
    parser.add_argument("--query", default=config.PEXELS_QUERY)
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default=config.YOUTUBE_PRIVACY)
    args = parser.parse_args()

    started = time.time()
    config.ensure_dirs()
    config.log("설정", f"FFmpeg(내장): {config.bootstrap_ffmpeg()}")

    # ---- Phase 1 ----
    if args.skip_script:
        config.log("Phase 1", "건너뜀 — 기존 script.txt 사용")
    else:
        import script_maker

        # collect_articles 는 최근에 다룬 주제를 걸러낸 목록을 돌려준다.
        articles = script_maker.collect_articles(config.MAX_HEADLINES)
        if not articles:
            config.log("Phase 1", "쓸 수 있는 새 주제가 없습니다. 중단합니다.")
            return 1

        provider = args.provider
        if provider == "openai" and not config.OPENAI_API_KEY:
            provider = "none"
        if provider == "gemini" and not config.GEMINI_API_KEY:
            provider = "none"
        script_maker.save(script_maker.make_script(articles, provider), articles)

    # ---- Phase 2 ----
    if args.skip_tts:
        config.log("Phase 2", "건너뜀 — 기존 audio.mp3 사용")
    else:
        import tts_generator

        tts_generator.synthesize()

    # ---- Phase 3 ----
    import video_renderer

    video_renderer.render(query=args.query, download=not args.no_download)

    # ---- Phase 4 ----
    if args.upload:
        import youtube_uploader

        youtube_uploader.upload(privacy=args.privacy)
    else:
        config.log("Phase 4", "업로드 생략 (--upload 를 붙이면 업로드합니다)")

    config.log("완료", f"총 소요 {time.time() - started:.1f}초 → {config.VIDEO_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        sys.exit(130)
    except Exception as exc:
        config.log("실패", f"{type(exc).__name__}: {exc}")
        sys.exit(1)
