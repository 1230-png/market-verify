import gc
import logging
import os
import uuid

import requests
from moviepy import (
    AudioFileClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
)

logger = logging.getLogger(__name__)

TMP_DIR = "/tmp"
DEFAULT_DURATION = 15  # 오디오 없이 호출될 때만 쓰는 fallback
MAX_DURATION = 90  # 안전 상한 (TTS가 비정상적으로 길게 나오는 경우 대비)
CANVAS_SIZE = (1080, 1920)  # 쇼츠 표준 세로 캔버스

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


def fetch_background_image(keywords: list[str]) -> str:
    """
    Pexels에서 세로형 고화질 이미지 1장을 받아 /tmp에 저장한다.
    keywords를 순서대로 시도하고, 전부 실패하면 RuntimeError.
    """
    last_error: Exception | None = None
    for keyword in keywords:
        try:
            return _fetch_one_image(keyword)
        except Exception as e:  # noqa: BLE001 - 다음 키워드로 넘어가기 위해 넓게 잡음
            logger.warning("Pexels fetch failed for keyword '%s': %s", keyword, e)
            last_error = e
    raise RuntimeError(f"No Pexels image found for keywords {keywords}: {last_error}")


def _fetch_one_image(keyword: str) -> str:
    api_key = os.environ["PEXELS_API_KEY"]
    resp = requests.get(
        PEXELS_SEARCH_URL,
        headers={"Authorization": api_key},
        params={"query": keyword, "orientation": "portrait", "size": "large", "per_page": 1},
        timeout=15,
    )
    resp.raise_for_status()
    photos = resp.json().get("photos", [])
    if not photos:
        raise RuntimeError(f"No results for keyword '{keyword}'")

    image_url = photos[0]["src"]["portrait"]
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_bg.jpg")
    success = False
    try:
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(img_resp.content)
        success = True
        return output_path
    finally:
        if not success and os.path.exists(output_path):
            os.remove(output_path)


def _fit_vertical_canvas(clip, canvas_size=CANVAS_SIZE):
    """이미지를 세로 캔버스에 꽉 채우도록 리사이즈 후 중앙 크롭한다 (cover 방식)."""
    canvas_w, canvas_h = canvas_size
    scale = max(canvas_w / clip.w, canvas_h / clip.h)
    resized = clip.resized((int(clip.w * scale) + 1, int(clip.h * scale) + 1))
    return resized.cropped(x_center=resized.w / 2, y_center=resized.h / 2, width=canvas_w, height=canvas_h)


def generate_short(bg_image_path: str, script_text: str, audio_path: str | None = None) -> str:
    """
    배경 이미지 + 자막(+나레이션 오디오)으로 쇼츠 mp4를 렌더링해 /tmp에 쓰고 최종 경로를 반환한다.
    실패/성공 여부와 무관하게 중간 산출물(임시 오디오 등)은 이 함수 안에서 즉시 삭제된다.
    반환된 최종 mp4는 업로드 후 '호출한 쪽'에서 삭제할 책임이 있다.
    """
    job_id = uuid.uuid4().hex
    temp_audiofile = os.path.join(TMP_DIR, f"{job_id}_temp_audio.m4a")
    output_path = os.path.join(TMP_DIR, f"{job_id}_short.mp4")

    image_clip = None
    audio_clip = None
    txt_clip = None
    final = None
    success = False
    try:
        audio_clip = AudioFileClip(audio_path) if audio_path else None
        duration = min(audio_clip.duration, MAX_DURATION) if audio_clip else DEFAULT_DURATION

        image_clip = _fit_vertical_canvas(ImageClip(bg_image_path)).with_duration(duration)

        txt_clip = (
            TextClip(
                text=script_text,
                font_size=60,
                color="white",
                stroke_color="black",
                stroke_width=2,
                size=(image_clip.w - 120, None),
                method="caption",
            )
            .with_duration(duration)
            .with_position("center")
        )

        final = CompositeVideoClip([image_clip, txt_clip], size=CANVAS_SIZE)
        if audio_clip:
            final = final.with_audio(audio_clip.with_duration(duration))

        final.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=temp_audiofile,
            remove_temp=True,
            fps=30,
            threads=2,
            logger=None,
        )
        success = True
        return output_path
    finally:
        for c in (final, txt_clip, image_clip, audio_clip):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    logger.debug("clip close failed", exc_info=True)

        if os.path.exists(temp_audiofile):
            os.remove(temp_audiofile)

        if not success and os.path.exists(output_path):
            os.remove(output_path)

        gc.collect()
