"""Phase 2 — 대본을 한국어 남성 음성 MP3 로 변환.

edge-tts (Microsoft Edge 온라인 TTS) 를 사용한다. API 키가 필요 없고,
윈도우에서 별도 음성 엔진을 설치하지 않아도 된다.

  assets/script.txt  →  assets/audio.mp3

실행::

    python tts_generator.py
    python tts_generator.py --list-voices        # 사용 가능한 한국어 음성 확인
    python tts_generator.py --voice ko-KR-HyunsuMultilingualNeural
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import config

PHASE = "Phase 2"

# 한국어 남성 음성. edge-tts 가 제공하는 음성 목록은 서비스 측 사정으로 바뀔 수 있어
# 실행 시점에 실제 목록과 대조한 뒤, 없으면 같은 조건의 다른 음성으로 대체한다.
DEFAULT_MALE_VOICE = "ko-KR-InJoonNeural"


async def list_korean_voices() -> list[dict]:
    """edge-tts 서비스에서 ko-KR 음성 목록을 받아온다."""
    import edge_tts

    voices = await edge_tts.list_voices()
    return [v for v in voices if v.get("Locale", "").startswith("ko-")]


async def _resolve_voice(preferred: str) -> str:
    """요청한 음성이 실제로 존재하는지 확인하고, 없으면 한국어 남성 음성으로 대체."""
    try:
        korean = await list_korean_voices()
    except Exception as exc:
        # 목록 조회에 실패해도 합성 자체는 될 수 있으므로 요청값을 그대로 쓴다.
        config.log(PHASE, f"음성 목록 조회 실패({type(exc).__name__}) — 요청한 음성을 그대로 사용합니다.")
        return preferred

    names = {v["ShortName"] for v in korean}
    if preferred in names:
        return preferred

    males = [v["ShortName"] for v in korean if v.get("Gender") == "Male"]
    fallback = males[0] if males else (korean[0]["ShortName"] if korean else preferred)
    config.log(PHASE, f"'{preferred}' 음성을 찾을 수 없어 '{fallback}' 로 대체합니다.")
    return fallback


async def _synthesize(text: str, voice: str, out_path, rate: str, volume: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, volume=volume)
    await communicate.save(str(out_path))


def synthesize(text: str | None = None, voice: str | None = None) -> str:
    """대본을 읽어 assets/audio.mp3 를 만든다. 생성된 파일 경로를 돌려준다."""
    config.ensure_dirs()

    if text is None:
        if not config.SCRIPT_PATH.exists():
            raise FileNotFoundError(
                f"{config.SCRIPT_PATH} 가 없습니다. 먼저 `python script_maker.py` 를 실행하세요."
            )
        text = config.SCRIPT_PATH.read_text(encoding="utf-8")

    text = text.strip()
    if not text:
        raise ValueError("대본이 비어 있습니다.")

    voice = voice or config.TTS_VOICE or DEFAULT_MALE_VOICE

    async def run() -> str:
        resolved = await _resolve_voice(voice)
        config.log(PHASE, f"음성 합성 시작 — voice={resolved}, rate={config.TTS_RATE}, {len(text)}자")
        await _synthesize(text, resolved, config.AUDIO_PATH, config.TTS_RATE, config.TTS_VOLUME)
        return resolved

    resolved = _run_async(run())

    if not config.AUDIO_PATH.exists() or config.AUDIO_PATH.stat().st_size == 0:
        raise RuntimeError("MP3 생성에 실패했습니다. 네트워크 연결과 음성 이름을 확인하세요.")

    size_kb = config.AUDIO_PATH.stat().st_size / 1024
    config.log(PHASE, f"저장 완료 → {config.AUDIO_PATH} ({size_kb:.0f} KB, voice={resolved})")
    return str(config.AUDIO_PATH)


def _run_async(coro):
    """Windows 에서 asyncio 이벤트 루프 종료 시 나는 경고를 피해 실행한다."""
    if sys.platform.startswith("win"):
        # ProactorEventLoop 종료 경합으로 'Event loop is closed' 가 뜨는 것을 막는다.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


def main() -> int:
    parser = argparse.ArgumentParser(description="대본 → 한국어 남성 TTS MP3")
    parser.add_argument("--voice", default=None, help=f"기본값: {config.TTS_VOICE}")
    parser.add_argument("--list-voices", action="store_true", help="사용 가능한 한국어 음성 출력")
    parser.add_argument("--text", default=None, help="script.txt 대신 직접 읽을 문장")
    args = parser.parse_args()

    try:
        if args.list_voices:
            voices = _run_async(list_korean_voices())
            if not voices:
                print("한국어 음성 목록이 비어 있습니다.")
                return 1
            for v in voices:
                print(f"  {v['ShortName']:<40} {v.get('Gender','?'):<8} {v.get('FriendlyName','')}")
            return 0

        synthesize(text=args.text, voice=args.voice)
    except (FileNotFoundError, ValueError) as exc:
        config.log(PHASE, f"오류: {exc}")
        return 1
    except Exception as exc:
        config.log(PHASE, f"TTS 실패 ({type(exc).__name__}): {exc}")
        config.log(PHASE, "edge-tts 는 Microsoft 서버에 접속해야 합니다. "
                          "인터넷 연결·방화벽·사내 프록시 설정을 확인하세요.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
