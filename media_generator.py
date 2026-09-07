import gc
import logging
import os
import uuid

import requests
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    vfx,
)

logger = logging.getLogger(__name__)

TMP_DIR = "/tmp"
DEFAULT_DURATION = 15  # 오디오 없이 호출될 때만 쓰는 fallback
MAX_DURATION = 90  # 안전 상한 (TTS가 비정상적으로 길게 나오는 경우 대비)
CANVAS_SIZE = (1080, 1920)  # 쇼츠 표준 세로 캔버스
CANVAS_W, CANVAS_H = CANVAS_SIZE
# 한글 자막 폰트. 미지정 시 기본 폰트에 한글 글리프가 없어 □□□로 깨진다.
# Bold가 배경 이미지 위에서 훨씬 또렷하다. Dockerfile의 fonts-nanum이 설치하는 경로.
FONT_PATH = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf")

# --- 자막 레이아웃 & 가독성 튜닝 (컨테이너 렌더 기준) ---
SUBTITLE_MARGIN_X = 90        # 좌우 대칭 여백 → 텍스트 박스 폭 = 1080-180 = 900
SUBTITLE_FONT_SIZE = 64
SUBTITLE_INTERLINE = 14       # 행간 (줄 사이 여백)
SUBTITLE_STROKE_WIDTH = 3     # 외곽선 (배경과 대비 확보)
BAND_PAD_X = 46               # 자막 뒤 반투명 박스 좌우 안쪽 여백
BAND_PAD_Y = 34               # 상하 안쪽 여백
BAND_OPACITY = 0.55           # 반투명 검정 박스 불투명도 (가독성 ↑, 배경 완전히 가리지 않음)
# --- 시각적 몰입감 ---
KENBURNS_ZOOM = 0.06          # 배경 슬로우 줌인 비율 (정지 이미지에 움직임 부여)
FADE = 0.4                    # 영상 전체 페이드 인/아웃 (초)
TEXT_FADE = 0.5               # 자막+박스 크로스페이드 등장 (초)

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
    band_clip = None
    final = None
    success = False
    try:
        audio_clip = AudioFileClip(audio_path) if audio_path else None
        duration = min(audio_clip.duration, MAX_DURATION) if audio_clip else DEFAULT_DURATION

        base_clip = _fit_vertical_canvas(ImageClip(bg_image_path)).with_duration(duration)
        # Ken Burns: 배경을 재생 내내 서서히 확대해 정지 이미지에 움직임을 준다.
        # CompositeVideoClip이 캔버스 밖으로 커진 부분을 잘라내므로 중앙 정렬만 하면 된다.
        image_clip = base_clip.resized(lambda t: 1 + KENBURNS_ZOOM * t / duration).with_position("center")

        # method="caption" + text_align="center" 로 여러 줄을 박스 안에서 가운데 정렬한다.
        # (기본값 left라 이전엔 왼쪽으로 쏠렸다.) 폭은 좌우 대칭 여백으로 계산.
        txt_clip = TextClip(
            text=script_text,
            font=FONT_PATH,
            font_size=SUBTITLE_FONT_SIZE,
            color="white",
            stroke_color="black",
            stroke_width=SUBTITLE_STROKE_WIDTH,
            size=(CANVAS_W - 2 * SUBTITLE_MARGIN_X, None),
            method="caption",
            text_align="center",
            interline=SUBTITLE_INTERLINE,
        ).with_duration(duration)

        # 자막 뒤 반투명 검정 박스 — 밝은 배경에서도 글자가 묻히지 않게.
        txt_w, txt_h = txt_clip.size
        band_clip = (
            ColorClip(size=(txt_w + 2 * BAND_PAD_X, txt_h + 2 * BAND_PAD_Y), color=(0, 0, 0))
            .with_opacity(BAND_OPACITY)
            .with_duration(duration)
            .with_position("center")
            .with_effects([vfx.CrossFadeIn(TEXT_FADE)])
        )
        txt_clip = txt_clip.with_position("center").with_effects([vfx.CrossFadeIn(TEXT_FADE)])

        final = CompositeVideoClip([image_clip, band_clip, txt_clip], size=CANVAS_SIZE)
        if audio_clip:
            final = final.with_audio(audio_clip.with_duration(duration))
        # 시작/끝 페이드로 매끄러운 전환.
        final = final.with_effects([vfx.FadeIn(FADE), vfx.FadeOut(FADE)])

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
        for c in (final, txt_clip, band_clip, image_clip, audio_clip):
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
