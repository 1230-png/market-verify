import os
import re
import uuid

import edge_tts

TMP_DIR = "/tmp"
DEFAULT_VOICE = "ko-KR-InJoonNeural"  # 한국어 남성
# 자막 한 청크 최대 글자수 — 문장을 통째로 띄우지 않고 짧게 끊어 시선을 집중시킨다.
CHUNK_MAX_CHARS = 18
_SENTENCE_END = tuple(".!?…")

# (text, start_sec, end_sec) 튜플의 리스트로 자막 세그먼트를 표현한다.
Segment = tuple[str, float, float]


def _chunk_segments(words: list[tuple[str, float, float]]) -> list[Segment]:
    """단어별 (텍스트, 시작초, 길이초)를 짧은 자막 청크로 묶는다.
    문장부호에서 끊고, CHUNK_MAX_CHARS를 넘으면 강제로 끊는다."""
    segments: list[Segment] = []
    buf: list[str] = []
    start = 0.0
    end = 0.0
    for text, offset, dur in words:
        if not buf:
            start = offset
        buf.append(text)
        end = offset + dur
        joined = " ".join(buf)
        if len(joined) >= CHUNK_MAX_CHARS or text.endswith(_SENTENCE_END):
            segments.append((joined, start, end))
            buf = []
    if buf:
        segments.append((" ".join(buf), start, end))
    return segments


def _fallback_segments(script_text: str) -> list[Segment]:
    """WordBoundary가 하나도 안 나온 경우 — 문장 단위로만 쪼갠다.
    타이밍은 알 수 없으므로 end=-1(=오디오 끝까지)로 두고, media_generator가
    오디오 길이에 맞춰 비례 배분한다."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script_text.strip()) if s.strip()]
    if not sentences:
        return [(script_text.strip(), 0.0, -1.0)]
    return [(s, -1.0, -1.0) for s in sentences]


async def synthesize_speech(
    script_text: str, voice: str = DEFAULT_VOICE
) -> tuple[str, list[Segment]]:
    """대본을 음성으로 합성해 /tmp에 mp3로 저장하고 (오디오경로, 자막세그먼트)를 반환한다.

    stream()으로 오디오와 단어 타임스탬프(WordBoundary)를 동시에 받아, 음성에 싱크된
    짧은 자막 청크를 만든다. 실패 시 부분 생성 파일은 즉시 삭제.
    """
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_tts.mp3")
    words: list[tuple[str, float, float]] = []
    success = False
    try:
        communicate = edge_tts.Communicate(script_text, voice)
        with open(output_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offset/duration 단위는 100ns → 초로 환산
                    words.append((chunk["text"], chunk["offset"] / 1e7, chunk["duration"] / 1e7))

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("edge-tts produced an empty audio file")

        segments = _chunk_segments(words) if words else _fallback_segments(script_text)
        success = True
        return output_path, segments
    finally:
        if not success and os.path.exists(output_path):
            os.remove(output_path)
