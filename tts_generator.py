import os
import uuid

import edge_tts

TMP_DIR = "/tmp"
DEFAULT_VOICE = "ko-KR-InJoonNeural"  # 한국어 남성


async def synthesize_speech(script_text: str, voice: str = DEFAULT_VOICE) -> str:
    """대본을 음성으로 합성해 /tmp에 mp3로 저장하고 경로를 반환한다. 실패 시 부분 생성 파일은 즉시 삭제."""
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_tts.mp3")
    success = False
    try:
        communicate = edge_tts.Communicate(script_text, voice)
        await communicate.save(output_path)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("edge-tts produced an empty audio file")
        success = True
        return output_path
    finally:
        if not success and os.path.exists(output_path):
            os.remove(output_path)
