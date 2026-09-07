# 암호화폐 유튜브 쇼츠 자동화 파이프라인

암호화폐 뉴스를 크롤링해 대본을 쓰고 → 한국어 남성 음성으로 읽고 → 세로 영상에 자막을 얹어 렌더링하고 → 유튜브에 비공개로 올리는 로컬 파이프라인입니다. 실행 환경은 **Windows 10/11 로컬 PC** 기준입니다.

**FFmpeg 를 따로 설치하거나 Path 환경변수를 건드릴 필요가 없습니다.** `imageio-ffmpeg` 가 FFmpeg 바이너리를 함께 배포하고, `config.bootstrap_ffmpeg()` 가 MoviePy 를 그 바이너리로 고정합니다. 자막도 Pillow 로 직접 그리므로 ImageMagick 도 필요 없습니다.

---

## 1. 설치 (Windows PowerShell)

PowerShell 을 열고 프로젝트 폴더에서 아래를 순서대로 실행하세요.

```powershell
# 1) 스크립트 실행 권한 (venv 활성화가 막히는 문제를 먼저 풀어둡니다)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Unrestricted

# 2) 가상환경 생성 및 활성화
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3) 패키지 설치 (FFmpeg 포함 — 별도 설치 불필요)
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4) 환경변수 파일 준비
copy .env.example .env
notepad .env
```

`Set-ExecutionPolicy` 실행 시 확인을 물으면 `Y` 를 입력하세요. 권한 문제로 거부되면 PowerShell 을 **관리자 권한**으로 다시 열고 실행합니다.

`Activate.ps1` 이 여전히 막힌다면 그 세션에서만 우회할 수도 있습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\venv\Scripts\Activate.ps1
```

설치가 끝나면 FFmpeg 가 제대로 잡혔는지 확인합니다.

```powershell
python -c "import config; print(config.bootstrap_ffmpeg())"
```

`...\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-....exe` 같은 경로가 찍히면 정상입니다.

---

## 2. 필요한 키

| 키 | 용도 | 발급처 | 없으면 |
|---|---|---|---|
| `OPENAI_API_KEY` 또는 `GEMINI_API_KEY` | 대본 작성 | platform.openai.com / aistudio.google.com | 규칙 기반 폴백 대본으로 자동 전환 |
| `PEXELS_API_KEY` | 배경 영상 | https://www.pexels.com/api/ (무료) | 그라데이션 배경으로 자동 대체 |
| `client_secrets.json` | 유튜브 업로드 | Google Cloud Console | Phase 4 만 실행 불가 |

`.env` 의 `LLM_PROVIDER` 로 `openai` / `gemini` / `none` 중 하나를 고릅니다.

> 모델 이름(`OPENAI_MODEL`, `GEMINI_MODEL`)은 각 제공사 정책에 따라 바뀔 수 있습니다. 호출이 "model not found" 로 실패하면 `.env` 에서 현재 제공되는 모델명으로 바꿔 주세요.

### `client_secrets.json` 만들기

1. [Google Cloud Console](https://console.cloud.google.com/) 에서 프로젝트 생성
2. **API 및 서비스 → 라이브러리** 에서 `YouTube Data API v3` 사용 설정
3. **OAuth 동의 화면** 구성 → 테스트 사용자에 본인 계정 추가
4. **사용자 인증 정보 → OAuth 클라이언트 ID → 애플리케이션 유형: 데스크톱 앱**
5. 내려받은 JSON 을 프로젝트 루트에 `client_secrets.json` 이름으로 저장

첫 업로드 때 브라우저가 한 번 열리고, 승인 후 `token.json` 이 생겨 이후로는 자동 로그인됩니다.

---

## 3. 실행

전체를 한 번에:

```powershell
python run_pipeline.py            # 대본 → 음성 → 영상
python run_pipeline.py --upload   # 업로드까지 (비공개)
```

단계별로 나눠서:

```powershell
python script_maker.py            # Phase 1 → assets/script.txt
python tts_generator.py           # Phase 2 → assets/audio.mp3
python video_renderer.py          # Phase 3 → output/final_video.mp4
python youtube_uploader.py        # Phase 4 → 유튜브 비공개 업로드
```

자주 쓰는 옵션:

```powershell
python script_maker.py --provider gemini
python script_maker.py --url https://example.com/기사주소   # RSS 대신 특정 URL
python tts_generator.py --list-voices                       # 한국어 음성 목록
python video_renderer.py --query ethereum --clips 4
python video_renderer.py --no-download                      # 받아둔 클립 재사용
python youtube_uploader.py --privacy unlisted
```

---

## 4. 모듈 구성

| 파일 | 단계 | 하는 일 |
|---|---|---|
| `config.py` | 공통 | 경로·환경변수, **FFmpeg 부트스트랩**, 한글 폰트 탐색 |
| `script_maker.py` | Phase 1 | RSS/URL 크롤링 → LLM 요약 → `assets/script.txt`, `assets/script_meta.json` |
| `tts_generator.py` | Phase 2 | edge-tts 로 한국어 남성 음성 → `assets/audio.mp3` |
| `video_renderer.py` | Phase 3 | Pexels 세로 영상 3개 다운로드 → 1080x1920 합성 + 하단 자막 → `output/final_video.mp4` |
| `youtube_uploader.py` | Phase 4 | OAuth 2.0 브라우저 인증 → 비공개 업로드 (제목/설명 자동 작성) |
| `run_pipeline.py` | 전체 | 위 단계를 순서대로 실행 |

### 자막이 그려지는 방식

`video_renderer.render_subtitle_image()` 가 Pillow 로 자막 한 장을 직접 그립니다.

- 실제 렌더 폭을 재서 줄바꿈하므로 한글이 화면 밖으로 나가지 않습니다
- 검은 외곽선 + 반투명 밴드를 함께 깔아 밝은 배경 위에서도 읽힙니다
- 폰트는 Windows 기본 탑재 `맑은 고딕`(`C:\Windows\Fonts\malgun.ttf`)을 자동으로 찾습니다. 다른 폰트를 쓰려면 `.env` 에 `SUBTITLE_FONT` 를 지정하세요

자막 길이는 문장별 글자 수에 비례해 음성 길이 안에 자동 배분됩니다.

---

## 5. 문제 해결

**`Activate.ps1 을 로드할 수 없습니다`**
→ 1번 항목의 `Set-ExecutionPolicy` 를 실행하세요.

**`한글 폰트를 찾지 못했습니다`**
→ `.env` 에 `SUBTITLE_FONT=C:\Windows\Fonts\malgun.ttf` 를 추가하세요.

**자막이 네모(□)로 나온다**
→ 지정한 폰트에 한글 글리프가 없습니다. 맑은 고딕이나 나눔고딕으로 바꾸세요.

**Phase 2 에서 TTS 실패**
→ edge-tts 는 Microsoft 서버에 접속해야 합니다. 인터넷 연결과 방화벽/사내 프록시를 확인하세요.

**Phase 3 에서 `세로형 영상을 찾지 못했습니다`**
→ `--query` 를 다른 키워드(`cryptocurrency`, `stock market`, `finance` 등)로 바꿔 보세요.

**업로드 후 쇼츠가 아니라 일반 영상으로 보인다**
→ 세로(9:16) + 60초 미만이면 유튜브가 처리 후 자동으로 쇼츠로 분류합니다. 처리에 몇 분 걸릴 수 있습니다.

**업로드 할당량 초과 (`quotaExceeded`)**
→ YouTube Data API 는 하루 10,000 유닛이 기본이고 업로드 1건이 약 1,600 유닛을 씁니다. 하루 약 6건이 한계입니다.

---

## 6. 렌더링 확인 결과

이 저장소의 코드는 다음을 실제로 실행해 확인했습니다.

- `imageio-ffmpeg` 내장 FFmpeg 로 MoviePy 가 동작 (수동 설치 없음)
- 1080x1920 / 30fps / H.264 + AAC 로 `final_video.mp4` 생성
- 한글 자막이 하단에 정상 렌더링 (Pillow 직접 그리기, ImageMagick 불필요)

Phase 1 의 실제 RSS 수집, Phase 2 의 edge-tts 합성, Phase 3 의 Pexels 다운로드, Phase 4 의 업로드는 개발 환경의 네트워크 제약으로 **외부 API 호출까지는 검증하지 못했습니다.** 코드 경로와 오류 처리는 로컬 픽스처로 확인했습니다.
