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

## 4. API 서버로 실행하기 (선택)

파이프라인을 HTTP 로 트리거하고 싶을 때 씁니다. 렌더링은 수 분이 걸리므로 요청은 즉시 반환되고, 작업은 백그라운드에서 돌아갑니다.

```powershell
uvicorn api_server:app --reload --port 8000
```

브라우저에서 <http://127.0.0.1:8000/docs> 를 열면 대화형 API 문서가 나옵니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/health` | FFmpeg 경로·자막 폰트·키 설정 여부 점검 |
| `POST` | `/jobs` | 파이프라인 시작 → `job_id` 즉시 반환 (202) |
| `GET` | `/jobs` | 작업 목록 |
| `GET` | `/jobs/{job_id}` | 상태와 단계별 진행 로그 |
| `GET` | `/jobs/{job_id}/video` | 완성된 mp4 내려받기 |

설치 직후 환경 점검부터 해보세요.

```powershell
curl http://127.0.0.1:8000/health
```

작업 시작과 진행 확인:

```powershell
curl -X POST http://127.0.0.1:8000/jobs -H "Content-Type: application/json" -d "{\"upload\": false}"
curl http://127.0.0.1:8000/jobs/<job_id>
```

`POST /jobs` 로 넘길 수 있는 값은 CLI 옵션과 같습니다: `provider`, `query`, `upload`, `privacy`, `skip_script`, `skip_tts`, `download`.

**렌더링은 CPU 를 많이 쓰므로 한 번에 하나만 실행됩니다.** 이미 작업이 돌고 있으면 `409` 를 돌려주니, `GET /health` 의 `busy` 를 먼저 확인하세요.

> 이 서버는 `127.0.0.1` 로컬 전용입니다. 인증이 없으므로 외부에 그대로 노출하지 마세요.

---

## 5. 매주 자동 실행 (완전 자동화)

매주 일요일 20:00 에 새 소재로 영상을 만들어 **비공개**로 업로드합니다. 서버와 스케줄러 두 개가 함께 떠 있어야 합니다.

```powershell
# 터미널 1 — API 서버
uvicorn api_server:app --port 8000

# 터미널 2 — 스케줄러
python scheduler.py
```

스케줄을 기다리지 않고 지금 한 번 돌려보려면:

```powershell
python scheduler.py --run-now              # 즉시 1회 실행 후 종료
python scheduler.py --run-now --no-upload  # 업로드 없이 렌더링만
python scheduler.py --day saturday --time 21:30
```

스케줄러가 하는 일:

1. `GET /health` 로 서버가 살아있는지 확인 (부팅 직후를 대비해 최대 60초 대기)
2. `POST /jobs` 호출 — 실패하면 **5 → 10 → 20 → 40초** 간격으로 최대 4회 재시도
3. 작업이 끝날 때까지 상태를 지켜보며 단계 변화를 로그에 기록
4. 결과(영상 경로, 업로드된 videoId 또는 실패 원인)를 남김

이미 다른 작업이 돌고 있으면 서버가 `409` 를 주고, 스케줄러는 재시도 없이 그 주를 건너뜁니다.

로그는 콘솔과 `logs/scheduler.log` 에 함께 쌓입니다 (5MB씩 3개 회전).

### 백그라운드로 계속 돌리기 (Windows)

**방법 A — `pythonw` 로 콘솔 없이 실행 (가장 간단)**

`pythonw.exe` 는 콘솔 창 없이 실행됩니다. 창을 닫아도 계속 돕니다.

```powershell
Start-Process -WindowStyle Hidden .env\Scripts\pythonw.exe -ArgumentList "scheduler.py","--quiet"
```

콘솔이 없으므로 상태는 로그 파일로 확인하세요.

```powershell
Get-Content .\logs\scheduler.log -Tail 30 -Wait
```

종료할 때:

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*scheduler.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

**방법 B — 작업 스케줄러에 등록 (재부팅 후 자동 시작, 권장)**

PC 를 껐다 켜도 자동으로 살아나게 하려면 이 방법을 쓰세요. 경로는 실제 프로젝트 위치로 바꾸세요.

```powershell
$dir = "C:\path\to\market-verify"

# API 서버 — 로그온 시 자동 시작
schtasks /create /tn "ShortsPipeline-API" /sc onlogon /rl highest /f `
  /tr "$dir\venv\Scripts\pythonw.exe -m uvicorn api_server:app --port 8000"

# 스케줄러 — 로그온 시 자동 시작
schtasks /create /tn "ShortsPipeline-Scheduler" /sc onlogon /rl highest /f `
  /tr "$dir\venv\Scripts\pythonw.exe $dir\scheduler.py --quiet"
```

등록 확인과 해제:

```powershell
schtasks /query /tn "ShortsPipeline-Scheduler"
schtasks /run   /tn "ShortsPipeline-Scheduler"   # 즉시 실행 테스트
schtasks /delete /tn "ShortsPipeline-Scheduler" /f
```

> 작업 스케줄러는 `시작 위치`를 지정하지 않으면 상대 경로를 못 찾습니다. 위처럼 **절대 경로**를 쓰거나, 작업 스케줄러 GUI 에서 `시작 위치(디렉터리)`를 프로젝트 폴더로 설정하세요.

> **절전 주의:** 일요일 20:00 에 PC 가 절전/최대 절전 상태면 실행되지 않습니다. 작업 스케줄러 GUI 의 `조건` 탭에서 `작업을 실행하기 위해 절전 모드 해제`를 켜두면 안전합니다.

**방법 C — 작업 스케줄러만으로 주간 실행**

`scheduler.py` 를 상주시키지 않고 작업 스케줄러가 직접 매주 실행하게 할 수도 있습니다. 이때 `--run-now` 를 붙이면 1회 실행 후 종료합니다.

```powershell
schtasks /create /tn "ShortsPipeline-Weekly" /sc weekly /d SUN /st 20:00 /f `
  /tr "$dir\venv\Scripts\pythonw.exe $dir\scheduler.py --run-now --quiet"
```

---

## 6. 주제 중복 방지

같은 사건을 몇 주 간격으로 다시 다루면 채널이 반복적으로 보입니다. Phase 1 이 다룬 주제를 `assets/history.json` 에 기록하고, 다음 실행 때 최근 것과 겹치는 소재를 걸러냅니다.

거르는 기준은 두 가지입니다.

1. **URL** — 추적 파라미터·`www`·끝 슬래시를 정규화해 비교하므로 같은 기사를 다른 링크로 받아도 걸러집니다.
2. **제목 키워드** — 겹친 키워드 수를 짧은 쪽 크기로 나눈 값(중복 계수)이 `HISTORY_SIMILARITY` 이상이면 같은 사건으로 봅니다. 어순만 바꾼 제목도 잡힙니다.

`비트코인`·`상승` 같은 흔한 단어는 불용어로 빠지고, 겹친 키워드가 2개 미만이면 우연으로 보아 중복 처리하지 않습니다. 같은 실행 안에서 서로 겹치는 기사들도 함께 정리됩니다.

```bash
HISTORY_DAYS=90            # 최근 3개월 내 주제 제외
HISTORY_SIMILARITY=0.6     # 낮출수록 엄격하게 걸러냄
HISTORY_RETENTION_DAYS=365 # 이보다 오래된 기록은 정리
```

**모든 기사가 걸러져 소재가 없으면 파이프라인은 영상을 만들지 않고 중단합니다.** `FEED_URLS` 에 매체를 더 추가하거나 `HISTORY_DAYS` 를 줄이세요.

기록을 초기화하려면 `assets/history.json` 을 지우면 됩니다.

---

## 7. 모듈 구성

| 파일 | 단계 | 하는 일 |
|---|---|---|
| `config.py` | 공통 | 경로·환경변수, **FFmpeg 부트스트랩**, 한글 폰트 탐색 |
| `script_maker.py` | Phase 1 | RSS/URL 크롤링 → LLM 요약 → `assets/script.txt`, `assets/script_meta.json` |
| `tts_generator.py` | Phase 2 | edge-tts 로 한국어 남성 음성 → `assets/audio.mp3` |
| `video_renderer.py` | Phase 3 | Pexels 세로 영상 3개 다운로드 → 1080x1920 합성 + 하단 자막 → `output/final_video.mp4` |
| `youtube_uploader.py` | Phase 4 | OAuth 2.0 브라우저 인증 → 비공개 업로드 (제목/설명 자동 작성) |
| `run_pipeline.py` | 전체 | 위 단계를 순서대로 실행 |
| `api_server.py` | API | 파이프라인을 HTTP 로 트리거하는 FastAPI 서버 |
| `scheduler.py` | 자동화 | 매주 1회 `POST /jobs` 를 호출하는 스케줄러 데몬 |

### 자막이 그려지는 방식

`video_renderer.render_subtitle_image()` 가 Pillow 로 자막 한 장을 직접 그립니다.

- 실제 렌더 폭을 재서 줄바꿈하므로 한글이 화면 밖으로 나가지 않습니다
- 검은 외곽선 + 반투명 밴드를 함께 깔아 밝은 배경 위에서도 읽힙니다
- 폰트는 Windows 기본 탑재 `맑은 고딕`(`C:\Windows\Fonts\malgun.ttf`)을 자동으로 찾습니다. 다른 폰트를 쓰려면 `.env` 에 `SUBTITLE_FONT` 를 지정하세요

자막 길이는 문장별 글자 수에 비례해 음성 길이 안에 자동 배분됩니다.

---

## 8. 문제 해결

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

## 9. 렌더링 확인 결과

이 저장소의 코드는 다음을 실제로 실행해 확인했습니다.

- `imageio-ffmpeg` 내장 FFmpeg 로 MoviePy 가 동작 (수동 설치 없음)
- 1080x1920 / 30fps / H.264 + AAC 로 `final_video.mp4` 생성
- 한글 자막이 하단에 정상 렌더링 (Pillow 직접 그리기, ImageMagick 불필요)
- 가로(1920x1080)·세로(720x1600) 소스 양쪽의 9:16 크롭 변환
- API 서버: `/health` 점검, `POST /jobs` 로 렌더링 완주, `/jobs/{id}/video` 다운로드,
  동시 실행 시 409, 실패 시 오류 기록과 락 해제까지 확인
- 중복 방지: 동일 URL·어순만 바꾼 제목은 제외, 반대 사건(유입/유출)과 다른 주제는 통과,
  3개월 경과분은 다시 허용되는 것까지 확인
- 스케줄러: 서버 미기동 시 재시도 후 실패 보고, 매주 일요일 20:00 다음 실행 시각 계산,
  실제 서버에 `POST /jobs` → 완료까지 추적, 409 시 그 주 건너뛰기 확인

Phase 1 의 실제 RSS 수집, Phase 2 의 edge-tts 합성, Phase 3 의 Pexels 다운로드,
Phase 4 의 업로드는 개발 환경의 네트워크 제약으로 **외부 API 호출까지는 검증하지 못했습니다.**
따라서 LLM 이 실제로 페르소나 지시를 얼마나 잘 따르는지도 확인되지 않았습니다.
