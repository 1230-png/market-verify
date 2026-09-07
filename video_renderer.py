"""Phase 3 — Pexels 영상 수집 + MoviePy 렌더링.

  1) Pexels API 로 'bitcoin' 키워드의 9:16 세로형 영상 3개 다운로드
  2) 1080x1920 로 크롭/리사이즈해 audio.mp3 길이에 맞춰 이어붙이기
  3) 하단에 대본 자막을 얹어 output/final_video.mp4 로 렌더링

FFmpeg 는 imageio-ffmpeg 내장 바이너리를 쓰므로 별도 설치가 필요 없다.
자막은 Pillow 로 직접 그리기 때문에 ImageMagick 도 필요 없다.

실행::

    python video_renderer.py
    python video_renderer.py --query ethereum --clips 4
    python video_renderer.py --no-download     # 이미 받아둔 클립 재사용
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap

import config

# ── moviepy 보다 먼저 FFmpeg 경로를 확정한다 (순서 중요) ──────────────────
config.bootstrap_ffmpeg()

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    concatenate_videoclips,
)

PHASE = "Phase 3"

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"


# ---------------------------------------------------------------------------
# 1) Pexels 영상 수집
# ---------------------------------------------------------------------------
def search_pexels_videos(query: str, count: int) -> list[dict]:
    """Pexels 에서 세로형(9:16) 영상을 검색해 다운로드 정보를 돌려준다."""
    import requests

    if not config.PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY 가 .env 에 없습니다. https://www.pexels.com/api/ 에서 무료 발급.")

    resp = requests.get(
        PEXELS_SEARCH_URL,
        headers={"Authorization": config.PEXELS_API_KEY},
        params={
            "query": query,
            "orientation": "portrait",   # 9:16 세로형
            "size": "medium",
            "per_page": max(count * 5, 15),
        },
        timeout=30,
    )
    resp.raise_for_status()
    videos = resp.json().get("videos", [])
    config.log(PHASE, f"Pexels 검색 '{query}' → {len(videos)}건")

    picked: list[dict] = []
    for video in videos:
        best = _best_vertical_file(video)
        if best is None:
            continue
        picked.append(
            {
                "id": video["id"],
                "url": best["link"],
                "width": best["width"],
                "height": best["height"],
                "duration": video.get("duration", 0),
            }
        )
        if len(picked) >= count:
            break

    if not picked:
        raise RuntimeError(f"'{query}' 로 세로형 영상을 찾지 못했습니다. 다른 키워드를 시도하세요.")
    return picked


def _best_vertical_file(video: dict) -> dict | None:
    """한 영상의 파일 목록에서 세로형이면서 해상도가 적당한 것을 고른다."""
    candidates = [
        f
        for f in video.get("video_files", [])
        if f.get("height") and f.get("width") and f["height"] > f["width"]
    ]
    if not candidates:
        return None
    # 1080x1920 을 채울 만큼 크되 지나치게 큰 4K 는 피한다 (다운로드/렌더 시간).
    usable = [f for f in candidates if f["height"] >= config.VIDEO_H] or candidates
    return min(usable, key=lambda f: abs(f["height"] - config.VIDEO_H))


def download_videos(videos: list[dict]) -> list[str]:
    """검색 결과를 assets/clips/ 로 내려받는다. 이미 있으면 재사용."""
    import requests

    config.ensure_dirs()
    paths: list[str] = []
    for i, video in enumerate(videos, 1):
        dest = config.CLIPS_DIR / f"pexels_{video['id']}.mp4"
        if dest.exists() and dest.stat().st_size > 0:
            config.log(PHASE, f"({i}/{len(videos)}) 캐시 사용: {dest.name}")
            paths.append(str(dest))
            continue

        config.log(PHASE, f"({i}/{len(videos)}) 다운로드 {video['width']}x{video['height']} …")
        with requests.get(video["url"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        config.log(PHASE, f"    저장: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        paths.append(str(dest))

    return paths


def make_placeholder_clips(count: int, seconds: float) -> list:
    """PEXELS_API_KEY 가 없을 때 쓰는 그라데이션 배경.

    영상 소스가 없어도 파이프라인 전체를 확인할 수 있게 한다.
    """
    config.log(PHASE, "PEXELS_API_KEY 가 없어 그라데이션 배경으로 대체합니다.")
    palettes = [((11, 17, 32), (32, 63, 110)), ((28, 12, 40), (94, 34, 84)), ((6, 30, 28), (18, 88, 74))]
    clips = []
    for i in range(count):
        top, bottom = palettes[i % len(palettes)]
        gradient = np.zeros((config.VIDEO_H, config.VIDEO_W, 3), dtype=np.uint8)
        ramp = np.linspace(0.0, 1.0, config.VIDEO_H)[:, None]
        for ch in range(3):
            gradient[:, :, ch] = (top[ch] + (bottom[ch] - top[ch]) * ramp).astype(np.uint8)
        clips.append(ImageClip(gradient).with_duration(seconds / count))
    return clips


# ---------------------------------------------------------------------------
# 2) 배경 영상 구성
# ---------------------------------------------------------------------------
def fit_to_shorts(clip):
    """어떤 비율의 클립이든 1080x1920 을 꽉 채우도록 리사이즈 후 중앙 크롭."""
    target_ratio = config.VIDEO_W / config.VIDEO_H
    clip_ratio = clip.w / clip.h

    if clip_ratio > target_ratio:
        # 원본이 더 넓다 → 높이를 맞추고 좌우를 잘라낸다.
        clip = clip.resized(height=config.VIDEO_H)
    else:
        # 원본이 더 좁다 → 너비를 맞추고 위아래를 잘라낸다.
        clip = clip.resized(width=config.VIDEO_W)

    return clip.cropped(
        x_center=clip.w / 2,
        y_center=clip.h / 2,
        width=config.VIDEO_W,
        height=config.VIDEO_H,
    )


def build_background(clip_paths: list[str], total_duration: float):
    """다운로드한 클립들을 total_duration 에 딱 맞게 이어붙인다.

    소스가 짧으면 처음부터 다시 순환해 채운다.
    """
    if not clip_paths:
        return concatenate_videoclips(make_placeholder_clips(3, total_duration))

    sources = [VideoFileClip(p).without_audio() for p in clip_paths]
    per_clip = total_duration / len(sources)

    segments = []
    filled = 0.0
    index = 0
    while filled < total_duration - 0.05:
        src = sources[index % len(sources)]
        want = min(per_clip, total_duration - filled)
        take = min(want, src.duration)
        if take <= 0.1:  # 못 쓸 만큼 짧은 소스는 건너뛴다
            index += 1
            if index > len(sources) * 4:
                break
            continue
        segments.append(fit_to_shorts(src.subclipped(0, take)))
        filled += take
        index += 1

    config.log(PHASE, f"배경 클립 {len(segments)}개 연결 → {filled:.1f}초")
    background = concatenate_videoclips(segments, method="compose")
    return background.with_duration(total_duration)


# ---------------------------------------------------------------------------
# 3) 자막
# ---------------------------------------------------------------------------
def split_subtitles(script: str) -> list[str]:
    """대본을 자막 한 장 분량(짧은 문장)으로 쪼갠다."""
    raw = re.split(r"(?<=[.!?])\s+|\n+", script.strip())
    chunks: list[str] = []
    for sentence in (s.strip() for s in raw):
        if not sentence:
            continue
        # 너무 긴 문장은 두 줄짜리 자막 크기로 다시 나눈다.
        if len(sentence) > 34:
            chunks.extend(textwrap.wrap(sentence, width=30))
        else:
            chunks.append(sentence)
    return chunks or [script.strip()]


def _wrap_korean(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """실제 렌더 폭을 재서 줄바꿈한다. 공백이 없는 한국어 덩어리도 강제 분할."""
    def width_of(s: str) -> int:
        return int(font.getbbox(s)[2] - font.getbbox(s)[0])

    lines: list[str] = []
    for word in text.split():
        if not lines:
            lines.append(word)
            continue
        candidate = f"{lines[-1]} {word}"
        if width_of(candidate) <= max_width:
            lines[-1] = candidate
        else:
            lines.append(word)

    # 단어 하나가 줄 폭을 넘으면 글자 단위로 자른다.
    final: list[str] = []
    for line in lines:
        while width_of(line) > max_width and len(line) > 1:
            cut = len(line)
            while cut > 1 and width_of(line[:cut]) > max_width:
                cut -= 1
            final.append(line[:cut])
            line = line[cut:]
        if line:
            final.append(line)
    return final


def render_subtitle_image(text: str, font_path: str) -> np.ndarray:
    """자막 한 장을 Pillow 로 그려 RGBA 배열로 돌려준다.

    MoviePy 의 TextClip 대신 직접 그리는 이유:
      - ImageMagick 등 외부 프로그램 의존이 전혀 없다
      - 한글 줄바꿈과 외곽선(가독성)을 원하는 대로 제어할 수 있다
    """
    font_size = config.SUBTITLE_FONT_SIZE
    font = ImageFont.truetype(font_path, font_size)

    max_width = int(config.VIDEO_W * 0.86)
    lines = _wrap_korean(text, font, max_width)

    line_height = int(font_size * 1.35)
    pad = int(font_size * 0.55)
    box_w = config.VIDEO_W
    box_h = line_height * len(lines) + pad * 2

    image = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 반투명 밴드 — 밝은 배경 위에서도 글자가 읽히도록
    band_margin = int(config.VIDEO_W * 0.05)
    draw.rounded_rectangle(
        [(band_margin, 0), (box_w - band_margin, box_h)],
        radius=int(font_size * 0.4),
        fill=(0, 0, 0, 165),
    )

    stroke = max(2, font_size // 16)
    for i, line in enumerate(lines):
        draw.text(
            (box_w / 2, pad + i * line_height + line_height / 2),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, 220),
            anchor="mm",
        )

    return np.array(image)


def build_subtitle_clips(script: str, total_duration: float) -> list:
    """자막 클립들을 글자 수에 비례한 길이로 배치한다."""
    chunks = split_subtitles(script)
    font_path = config.find_korean_font()
    config.log(PHASE, f"자막 {len(chunks)}장 생성 (폰트: {font_path})")

    weights = [max(len(c), 4) for c in chunks]
    total_weight = sum(weights)

    clips = []
    start = 0.0
    for chunk, weight in zip(chunks, weights):
        duration = total_duration * weight / total_weight
        frame = render_subtitle_image(chunk, font_path)
        clip = (
            ImageClip(frame, transparent=True)
            .with_start(start)
            .with_duration(duration)
            .with_position(("center", config.VIDEO_H - config.SUBTITLE_BOTTOM_MARGIN - frame.shape[0]))
        )
        clips.append(clip)
        start += duration

    return clips


# ---------------------------------------------------------------------------
# 4) 렌더링
# ---------------------------------------------------------------------------
def render(query: str | None = None, clip_count: int | None = None, download: bool = True) -> str:
    config.ensure_dirs()

    if not config.SCRIPT_PATH.exists():
        raise FileNotFoundError(f"{config.SCRIPT_PATH} 가 없습니다. 먼저 script_maker.py 를 실행하세요.")
    if not config.AUDIO_PATH.exists():
        raise FileNotFoundError(f"{config.AUDIO_PATH} 가 없습니다. 먼저 tts_generator.py 를 실행하세요.")

    script = config.SCRIPT_PATH.read_text(encoding="utf-8").strip()

    audio = AudioFileClip(str(config.AUDIO_PATH))
    duration = min(audio.duration + 0.6, config.MAX_VIDEO_SECONDS)  # 끝을 살짝 여유 있게
    if audio.duration > config.MAX_VIDEO_SECONDS:
        config.log(PHASE, f"음성이 {audio.duration:.1f}초로 길어 {config.MAX_VIDEO_SECONDS}초에서 자릅니다.")
        audio = audio.subclipped(0, config.MAX_VIDEO_SECONDS)
    config.log(PHASE, f"음성 길이 {audio.duration:.1f}초 → 영상 {duration:.1f}초")

    # --- 배경 ---
    clip_paths: list[str] = []
    if download and config.PEXELS_API_KEY:
        videos = search_pexels_videos(query or config.PEXELS_QUERY, clip_count or config.PEXELS_CLIP_COUNT)
        clip_paths = download_videos(videos)
    elif not download:
        clip_paths = sorted(str(p) for p in config.CLIPS_DIR.glob("*.mp4"))
        config.log(PHASE, f"기존 클립 {len(clip_paths)}개 재사용")

    background = build_background(clip_paths, duration)

    # --- 자막 ---
    subtitle_clips = build_subtitle_clips(script, audio.duration)

    # --- 합성 ---
    final = CompositeVideoClip([background, *subtitle_clips], size=(config.VIDEO_W, config.VIDEO_H))
    final = final.with_duration(duration).with_audio(audio)

    config.log(PHASE, f"렌더링 시작 → {config.VIDEO_PATH}")
    final.write_videofile(
        str(config.VIDEO_PATH),
        fps=config.VIDEO_FPS,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        temp_audiofile=str(config.OUTPUT_DIR / "temp-audio.m4a"),
        remove_temp=True,
    )

    final.close()
    audio.close()
    background.close()

    size_mb = config.VIDEO_PATH.stat().st_size / 1e6
    config.log(PHASE, f"완료 → {config.VIDEO_PATH} ({size_mb:.1f} MB, {config.VIDEO_W}x{config.VIDEO_H})")
    return str(config.VIDEO_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pexels 영상 + TTS → 쇼츠 렌더링")
    parser.add_argument("--query", default=config.PEXELS_QUERY)
    parser.add_argument("--clips", type=int, default=config.PEXELS_CLIP_COUNT)
    parser.add_argument("--no-download", action="store_true", help="assets/clips 의 기존 영상 재사용")
    args = parser.parse_args()

    try:
        render(query=args.query, clip_count=args.clips, download=not args.no_download)
    except FileNotFoundError as exc:
        config.log(PHASE, f"오류: {exc}")
        return 1
    except Exception as exc:
        config.log(PHASE, f"렌더링 실패 ({type(exc).__name__}): {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
