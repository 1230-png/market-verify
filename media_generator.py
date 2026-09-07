import gc
import logging
import os
import uuid

import requests
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    afx,
    vfx,
)

logger = logging.getLogger(__name__)

TMP_DIR = "/tmp"
DEFAULT_DURATION = 15  # 오디오 없이 호출될 때만 쓰는 fallback
MAX_DURATION = 90  # 안전 상한 (TTS가 비정상적으로 길게 나오는 경우 대비)
CANVAS_SIZE = (1080, 1920)  # 쇼츠 표준 세로 캔버스
CANVAS_W, CANVAS_H = CANVAS_SIZE

# 자막 폰트 — 나눔스퀘어 Bold. 배경 위 가독성이 나눔고딕보다 좋고 한글 글리프가 있어 □□□가 안 난다.
FONT_PATH = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf")

# --- 자막 레이아웃 & 가독성 ---
SUBTITLE_MARGIN_X = 90        # 좌우 대칭 여백 → 텍스트 박스 폭 = 1080-180 = 900 (좌측 짤림 방지)
SUBTITLE_FONT_SIZE = 72       # 청크로 짧게 끊으므로 크게 키워 시선 집중
SUBTITLE_INTERLINE = 14
SUBTITLE_STROKE_WIDTH = 3
BAND_PAD_X = 46
BAND_PAD_Y = 34
BAND_OPACITY = 0.55
SUBTITLE_Y = 0.66            # 자막 세로 위치 (화면 높이 비율) — 하단 1/3, 배경 주제 가리지 않게
MIN_SUB_DUR = 0.5           # 너무 짧은 청크도 최소 이만큼은 보이게

# --- 시각적 몰입감 ---
KENBURNS_ZOOM = 0.08         # 컷별 슬로우 줌 비율
CROSSFADE = 0.6             # 컷 전환 크로스페이드 (초)
FADE = 0.4                  # 영상 전체 페이드 인/아웃 (초)
DEFAULT_BG_COUNT = 5        # 배경 이미지 컷 수

# --- 오디오 믹싱 (BGM은 파일이 있을 때만 합쳐진다; 없으면 음성만) ---
ASSETS_DIR = os.environ.get("ASSETS_DIR", "assets")
BGM_PATH = os.environ.get("BGM_PATH", os.path.join(ASSETS_DIR, "bgm.mp3"))
BGM_VOLUME = 0.12           # 음성 밑에 깔리도록 크게 낮춤 (덕킹)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


def fetch_background_images(keywords: list[str], n: int = DEFAULT_BG_COUNT) -> list[str]:
    """Pexels에서 서로 다른 세로형 이미지 n장을 받아 /tmp에 저장하고 경로 리스트를 반환한다.
    keywords를 순환하며 페이지를 넘겨 중복을 피한다. 최소 1장은 확보하지 못하면 RuntimeError."""
    paths: list[str] = []
    seen_urls: set[str] = set()
    page = 1
    # keywords를 여러 바퀴 돌며(페이지 증가) n장을 모은다.
    for attempt in range(n * 3):
        if len(paths) >= n:
            break
        keyword = keywords[attempt % len(keywords)]
        if attempt > 0 and attempt % len(keywords) == 0:
            page += 1
        try:
            path, url = _fetch_one_image(keyword, page, seen_urls)
            seen_urls.add(url)
            paths.append(path)
        except Exception as e:  # noqa: BLE001 - 다음 키워드/페이지로 넘어가기 위해 넓게 잡음
            logger.warning("Pexels fetch failed (kw='%s', page=%d): %s", keyword, page, e)

    if not paths:
        raise RuntimeError(f"No Pexels images found for keywords {keywords}")
    logger.info("Fetched %d/%d background images", len(paths), n)
    return paths


def _fetch_one_image(keyword: str, page: int, seen_urls: set[str]) -> tuple[str, str]:
    api_key = os.environ["PEXELS_API_KEY"]
    resp = requests.get(
        PEXELS_SEARCH_URL,
        headers={"Authorization": api_key},
        params={"query": keyword, "orientation": "portrait", "size": "large", "per_page": 3, "page": page},
        timeout=15,
    )
    resp.raise_for_status()
    photos = resp.json().get("photos", [])
    if not photos:
        raise RuntimeError(f"No results for keyword '{keyword}' page {page}")

    photo = next((p for p in photos if p["src"]["portrait"] not in seen_urls), photos[0])
    image_url = photo["src"]["portrait"]
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}_bg.jpg")
    success = False
    try:
        img_resp = requests.get(image_url, timeout=30)
        img_resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(img_resp.content)
        success = True
        return output_path, image_url
    finally:
        if not success and os.path.exists(output_path):
            os.remove(output_path)


def _fit_vertical_canvas(clip, canvas_size=CANVAS_SIZE):
    """이미지를 세로 캔버스에 꽉 채우도록 리사이즈 후 중앙 크롭한다 (cover 방식)."""
    canvas_w, canvas_h = canvas_size
    scale = max(canvas_w / clip.w, canvas_h / clip.h)
    resized = clip.resized((int(clip.w * scale) + 1, int(clip.h * scale) + 1))
    return resized.cropped(x_center=resized.w / 2, y_center=resized.h / 2, width=canvas_w, height=canvas_h)


def _build_background(bg_image_paths: list[str], duration: float):
    """여러 이미지를 컷으로 이어붙이고, 컷마다 방향을 번갈아가며 Ken Burns 줌을 준다.
    컷 사이는 크로스페이드로 부드럽게 전환한다."""
    n = len(bg_image_paths)
    seg = duration / n
    clips = []
    for i, path in enumerate(bg_image_paths):
        # 마지막 컷을 제외하고 다음 컷과 CROSSFADE 만큼 겹치게 길이를 늘린다.
        d = seg + (CROSSFADE if i < n - 1 else 0)
        base = _fit_vertical_canvas(ImageClip(path)).with_duration(d)
        if i % 2 == 0:
            zoom = lambda t, d=d: 1 + KENBURNS_ZOOM * (t / d)          # 줌 인
        else:
            zoom = lambda t, d=d: (1 + KENBURNS_ZOOM) - KENBURNS_ZOOM * (t / d)  # 줌 아웃
        clip = base.resized(zoom).with_position("center").with_start(i * seg)
        if i > 0:
            clip = clip.with_effects([vfx.CrossFadeIn(CROSSFADE)])
        clips.append(clip)
    return CompositeVideoClip(clips, size=CANVAS_SIZE).with_duration(duration)


def _resolve_times(segments, duration: float):
    """자막 세그먼트의 (start,end)를 실제 초로 확정한다.
    타이밍이 전혀 없는 폴백(all start<0)이면 글자수 비례로 균등 배분한다."""
    if segments and all(s < 0 for _, s, _ in segments):
        total = sum(len(t) for t, _, _ in segments) or 1
        out = []
        cur = 0.0
        for text, _, _ in segments:
            d = duration * len(text) / total
            out.append((text, cur, cur + d))
            cur += d
        return out
    return [(t, max(0.0, s), duration if e < 0 else min(e, duration)) for t, s, e in segments]


def _make_caption(text: str, start: float, dur: float):
    """가운데 정렬 자막 한 청크 + 뒤 반투명 박스를 (band, txt) 튜플로 만든다."""
    txt = TextClip(
        text=text,
        font=FONT_PATH,
        font_size=SUBTITLE_FONT_SIZE,
        color="white",
        stroke_color="black",
        stroke_width=SUBTITLE_STROKE_WIDTH,
        size=(CANVAS_W - 2 * SUBTITLE_MARGIN_X, None),
        method="caption",
        text_align="center",
        interline=SUBTITLE_INTERLINE,
    )
    txt_w, txt_h = txt.size
    pos_y = int(CANVAS_H * SUBTITLE_Y)
    band = (
        ColorClip(size=(txt_w + 2 * BAND_PAD_X, txt_h + 2 * BAND_PAD_Y), color=(0, 0, 0))
        .with_opacity(BAND_OPACITY)
        .with_position(("center", pos_y - BAND_PAD_Y))
    )
    txt = txt.with_position(("center", pos_y))
    fx = [vfx.CrossFadeIn(0.15), vfx.CrossFadeOut(0.1)]
    band = band.with_start(start).with_duration(dur).with_effects(fx)
    txt = txt.with_start(start).with_duration(dur).with_effects(fx)
    return band, txt


def _build_subtitles(subtitle_segments, duration: float):
    """음성에 싱크된 타임드 자막 클립들을 만든다 (한 번에 한 청크씩)."""
    clips = []
    for text, s, e in _resolve_times(subtitle_segments, duration):
        if s >= duration or not text.strip():
            continue
        dur = min(max(e - s, MIN_SUB_DUR), duration - s)
        band, txt = _make_caption(text, s, dur)
        clips.extend([band, txt])
    return clips


def _mix_audio(voice_clip, duration: float, closers: list):
    """음성 + (있으면) BGM을 믹싱한다. BGM은 duration에 맞춰 루프/페이드하고 크게 낮춰 덕킹한다.
    지금은 BGM 파일이 없어 음성만 반환되지만, assets/bgm.mp3만 넣으면 즉시 합쳐진다."""
    voice = voice_clip.with_duration(min(voice_clip.duration, duration))
    tracks = [voice]
    if os.path.exists(BGM_PATH):
        bgm = AudioFileClip(BGM_PATH).with_effects(
            [
                afx.AudioLoop(duration=duration),
                afx.MultiplyVolume(BGM_VOLUME),
                afx.AudioFadeIn(1.0),
                afx.AudioFadeOut(1.5),
            ]
        )
        closers.append(bgm)
        tracks.append(bgm)
        logger.info("Mixed BGM from %s at volume %.2f", BGM_PATH, BGM_VOLUME)
    else:
        logger.info("No BGM file at %s, using voice only", BGM_PATH)
    return CompositeAudioClip(tracks)


def generate_short(
    bg_image_paths: list[str], subtitle_segments, audio_path: str | None = None
) -> str:
    """배경 이미지 컷들 + 타임드 자막(+믹싱된 오디오)으로 쇼츠 mp4를 렌더링해 /tmp에 쓰고 경로를 반환한다.

    bg_image_paths: 배경으로 컷 전환할 이미지 경로 리스트 (Ken Burns + 크로스페이드).
    subtitle_segments: (text, start_sec, end_sec) 리스트. 음성에 싱크된 자막 청크.
    실패/성공과 무관하게 중간 산출물은 이 함수 안에서 삭제된다. 반환된 mp4는 호출부가 삭제 책임.
    """
    job_id = uuid.uuid4().hex
    temp_audiofile = os.path.join(TMP_DIR, f"{job_id}_temp_audio.m4a")
    output_path = os.path.join(TMP_DIR, f"{job_id}_short.mp4")

    if not bg_image_paths:
        raise RuntimeError("generate_short requires at least one background image")

    audio_clip = None
    background = None
    final = None
    closers: list = []
    success = False
    try:
        audio_clip = AudioFileClip(audio_path) if audio_path else None
        duration = min(audio_clip.duration, MAX_DURATION) if audio_clip else DEFAULT_DURATION

        background = _build_background(bg_image_paths, duration)
        sub_clips = _build_subtitles(subtitle_segments, duration)

        final = CompositeVideoClip([background, *sub_clips], size=CANVAS_SIZE).with_duration(duration)
        if audio_clip:
            final = final.with_audio(_mix_audio(audio_clip, duration, closers))
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
        for c in (final, background, audio_clip, *closers):
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
