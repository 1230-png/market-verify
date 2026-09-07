"""Phase 1 — 암호화폐 뉴스 수집 + 쇼츠 대본 생성.

  1) RSS(또는 임의 URL)에서 최신 암호화폐 기사 헤드라인/요약을 크롤링
  2) OpenAI 또는 Gemini 를 호출해 1분 미만 분량의 한국어 쇼츠 대본으로 요약
  3) assets/script.txt 로 저장 (제목/설명은 assets/script_meta.json 에 함께 저장)

실행::

    python script_maker.py
    python script_maker.py --provider gemini
    python script_maker.py --url https://example.com/article   # RSS 대신 특정 URL
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import config

PHASE = "Phase 1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Article:
    title: str
    summary: str
    source: str
    link: str


# ---------------------------------------------------------------------------
# 1) 수집
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    """HTML 태그와 잉여 공백 제거."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_from_feeds(feed_urls: list[str], limit: int) -> list[Article]:
    """RSS 피드들에서 최신 기사를 모은다. 실패한 피드는 건너뛴다.

    feedparser 에 URL 을 직접 넘기지 않고 requests 로 받아온 뒤 파싱한다.
    (프록시/UA/리다이렉트 처리가 안정적이고, 실패 원인이 그대로 드러난다.)
    """
    import feedparser
    import requests

    articles: list[Article] = []
    for url in feed_urls:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:  # 네트워크/파싱 오류는 치명적이지 않다
            config.log(PHASE, f"피드 실패 ({url}): {type(exc).__name__}: {exc}")
            continue

        if not parsed.entries:
            reason = getattr(parsed, "bozo_exception", "항목 없음")
            config.log(PHASE, f"피드 비어 있음 ({url}): {reason}")
            continue

        source = _clean(getattr(parsed.feed, "title", "")) or url
        for entry in parsed.entries[:limit]:
            title = _clean(entry.get("title", ""))
            if not title:
                continue
            summary = _clean(entry.get("summary", "") or entry.get("description", ""))
            articles.append(
                Article(title=title, summary=summary[:600], source=source, link=entry.get("link", ""))
            )
        config.log(PHASE, f"수집: {url} → {len(parsed.entries)}건")

    return articles[:limit]


def fetch_from_url(url: str) -> list[Article]:
    """특정 웹페이지 본문을 긁어온다 (RSS 가 아닌 단일 기사용)."""
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    title = _clean(soup.title.get_text() if soup.title else url)
    paragraphs = [_clean(p.get_text()) for p in soup.find_all("p")]
    body = " ".join(p for p in paragraphs if len(p) > 40)[:3000]

    config.log(PHASE, f"URL 크롤링 완료: {len(body)}자")
    return [Article(title=title, summary=body, source=url, link=url)]


# ---------------------------------------------------------------------------
# 1-b) 주제 중복 방지 (assets/history.json)
# ---------------------------------------------------------------------------
# 같은 사건을 몇 주 간격으로 다시 다루면 채널이 반복적으로 보인다.
# 다뤘던 기사 URL 과 키워드를 남겨 두고, 최근 것과 겹치는 소재를 걸러낸다.

# 키워드 비교에서 뺄 흔한 단어들. 이게 없으면 "비트코인"만으로 전부 중복 처리된다.
_STOPWORDS = {
    "비트코인", "이더리움", "암호화폐", "코인", "가상자산", "시황", "전망", "분석",
    "오늘", "속보", "단독", "상승", "하락", "돌파", "급등", "급락", "시장",
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency", "market",
    "price", "the", "a", "an", "of", "to", "in", "on", "for", "as", "is", "at",
    "and", "with", "after", "amid", "says", "will", "new",
}


def _normalize_url(url: str) -> str:
    """추적 파라미터와 프래그먼트를 떼어 같은 기사를 같은 키로 만든다."""
    from urllib.parse import urlsplit, urlunsplit

    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, "", "")).lstrip("/")


def _keywords(text: str) -> set[str]:
    """제목에서 비교용 키워드를 뽑는다. 불용어와 한 글자 토큰은 버린다."""
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", (text or "").lower())
    return {t for t in tokens if len(t) > 1 and t not in _STOPWORDS}


def load_history() -> list[dict]:
    """assets/history.json 을 읽는다. 없거나 깨졌으면 빈 목록."""
    if not config.HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(config.HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        config.log(PHASE, "history.json 이 손상되어 새로 시작합니다.")
        return []
    return data if isinstance(data, list) else []


def _recent(history: list[dict], days: int) -> list[dict]:
    """days 일 이내 항목만 골라낸다. 날짜가 없거나 이상하면 보수적으로 포함시킨다."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for entry in history:
        raw = entry.get("date")
        if not raw:
            recent.append(entry)
            continue
        try:
            if datetime.fromisoformat(raw) >= cutoff:
                recent.append(entry)
        except ValueError:
            recent.append(entry)
    return recent


def filter_seen(articles: list[Article], history: list[dict] | None = None) -> list[Article]:
    """최근 HISTORY_DAYS 일 안에 다룬 주제를 제외한다.

    두 단계로 거른다.
      1. 정규화한 URL 이 같으면 같은 기사 → 제외
      2. 제목 키워드가 자카드 유사도 기준 이상 겹치면 같은 사건 → 제외
    이번 실행 안에서 서로 겹치는 기사들도 함께 정리한다.
    """
    history = load_history() if history is None else history
    recent = _recent(history, config.HISTORY_DAYS)

    seen_urls = {u for e in recent if (u := _normalize_url(e.get("link", "")))}
    seen_keywords = [set(e.get("keywords", [])) for e in recent]
    seen_keywords = [k for k in seen_keywords if k]

    kept: list[Article] = []
    dropped = 0
    for art in articles:
        url = _normalize_url(art.link)
        if url and url in seen_urls:
            dropped += 1
            continue

        keys = _keywords(art.title)
        if keys and any(_similarity(keys, prev) >= config.HISTORY_SIMILARITY for prev in seen_keywords):
            dropped += 1
            continue

        kept.append(art)
        # 이번 목록 안의 중복도 막기 위해 즉시 반영한다.
        if url:
            seen_urls.add(url)
        if keys:
            seen_keywords.append(keys)

    if dropped:
        config.log(PHASE, f"최근 {config.HISTORY_DAYS}일 내 중복 주제 {dropped}건 제외 → {len(kept)}건 남음")
    return kept


def _similarity(a: set[str], b: set[str]) -> float:
    """중복 계수(overlap coefficient): 겹친 수 / 더 짧은 쪽 크기.

    제목은 토큰이 적어서 자카드를 쓰면 길이 차이만으로 점수가 흔들린다.
    같은 사건을 다르게 표현한 제목("A가 B를 돌파" vs "B 돌파한 A")을 잡으려면
    짧은 쪽 기준으로 보는 편이 안정적이다.
    겹친 키워드가 2개 미만이면 우연일 수 있으므로 중복으로 보지 않는다.
    """
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if shared < 2:
        return 0.0
    return shared / min(len(a), len(b))


def record_history(articles: list[Article], main_keyword: str = "", title: str = "") -> None:
    """이번에 다룬 주제를 history.json 에 추가한다."""
    config.ensure_dirs()
    history = load_history()
    now = datetime.now().isoformat(timespec="seconds")

    for art in articles:
        history.append(
            {
                "date": now,
                "title": art.title,
                "link": art.link,
                "source": art.source,
                "keywords": sorted(_keywords(f"{art.title} {main_keyword}")),
                "main_keyword": main_keyword,
                "video_title": title,
            }
        )

    # 파일이 무한정 커지지 않도록 보존 기간을 넘긴 항목은 버린다.
    before = len(history)
    history = _recent(history, config.HISTORY_RETENTION_DAYS)
    if before != len(history):
        config.log(PHASE, f"오래된 히스토리 {before - len(history)}건 정리")

    config.HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    config.log(PHASE, f"히스토리 {len(articles)}건 기록 → {config.HISTORY_PATH} (총 {len(history)}건)")


def collect_articles(limit: int, url: str | None = None) -> list[Article]:
    """기사를 모으고 최근에 다룬 주제를 걸러낸다.

    run_pipeline.py 와 api_server.py 도 이 함수를 쓴다.
    """
    articles = fetch_from_url(url) if url else fetch_from_feeds(config.FEED_URLS, limit)
    if not articles:
        return []

    fresh = filter_seen(articles)
    if not fresh:
        config.log(PHASE, "새로운 주제가 없습니다. 피드가 갱신될 때까지 기다리거나 FEED_URLS 를 늘리세요.")
    return fresh


# ---------------------------------------------------------------------------
# 2) 대본 생성
# ---------------------------------------------------------------------------
# 채널 페르소나. 이 톤이 채널의 정체성이므로 프롬프트에 강하게 고정한다.
SYSTEM_PROMPT = """너는 11년 차 암호화폐 전문 차트 분석가 유튜버야.
시청자에게 인사 없이 결론(상승/하락)부터 단호하게 말해.
대본 구조는 [1. 단기 방향성 결론 -> 2. 트레이딩뷰 차트 기술적 근거(이평선, 파동 등) -> 3. 거시경제 또는 온체인 지표 근거 -> 4. 텔레그램 방 유도 및 구독 요청] 순서로 작성해.
불필요한 미사여구를 빼고 1분 내외의 속도감 있는 스크립트로 써줘."""


PROMPT_TEMPLATE = """아래 최신 뉴스를 근거로 유튜브 쇼츠 대본을 작성해.

[분량]
- 공백 포함 {target_chars}자 내외. 절대 {max_chars}자를 넘기지 마.

[반드시 지킬 구조] — 이 순서를 벗어나지 마
1. 단기 방향성 결론: 인사말 없이 첫 문장부터 상승/하락 결론을 단호하게 못 박아.
2. 차트 기술적 근거: 이평선, 파동, 지지·저항, 거래량 등 트레이딩뷰에서 볼 수 있는 근거를 대.
3. 거시경제 또는 온체인 지표 근거: 금리, 달러, ETF 자금 흐름, 거래소 보유량, 고래 지갑 등에서 골라 대.
4. 마무리: 텔레그램 방 참여 유도와 구독 요청.

[작성 규칙]
- 성우가 소리 내어 읽을 문장만 써. 화면 지시문, 괄호 설명, 이모지, 마크다운, 해시태그 금지.
- 한 문장은 짧게 끊고, 문장마다 줄바꿈해.
- 숫자와 지표를 구체적으로 말해. 뜬구름 잡는 표현은 빼.

[오늘 날짜] {today}

[뉴스 원문]
{news}

[출력 형식] 아래 JSON 만 출력해. 다른 텍스트를 붙이지 마.
{{"title": "유튜브 제목 (40자 이내)",
  "description": "유튜브 설명란 (2~3문장)",
  "main_keyword": "이번 영상의 핵심 주제 키워드 (2~5단어)",
  "script": "성우가 읽을 대본 본문"}}
"""


def _build_prompt(articles: list[Article]) -> str:
    news_lines = []
    for i, art in enumerate(articles, 1):
        news_lines.append(f"{i}. [{art.source}] {art.title}\n   {art.summary[:300]}")
    return PROMPT_TEMPLATE.format(
        target_chars=config.TARGET_CHARS,
        max_chars=int(config.TARGET_CHARS * 1.2),
        today=datetime.now().strftime("%Y년 %m월 %d일"),
        news="\n".join(news_lines) if news_lines else "(수집된 뉴스 없음)",
    )


def _parse_llm_json(raw: str) -> dict:
    """LLM 응답에서 JSON 객체를 최대한 관대하게 추출한다."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # JSON 파싱에 실패하면 응답 전체를 대본으로 취급한다.
    config.log(PHASE, "LLM 응답이 JSON 이 아니라 본문 전체를 대본으로 사용합니다.")
    return {"title": "", "description": "", "script": raw}


def generate_with_openai(prompt: str) -> dict:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 가 .env 에 없습니다.")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )
    return _parse_llm_json(resp.choices[0].message.content or "")


def generate_with_gemini(prompt: str) -> dict:
    from google import genai

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 가 .env 에 없습니다.")

    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        # OpenAI 쪽 system 메시지와 같은 페르소나를 적용한다.
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.7),
    )
    return _parse_llm_json(resp.text or "")


def generate_offline(articles: list[Article]) -> dict:
    """API 키가 없을 때 쓰는 규칙 기반 폴백.

    LLM 없이도 파이프라인 전체(TTS→렌더링→업로드)를 끝까지 돌려볼 수 있게 한다.
    품질은 LLM 대본보다 떨어지므로 실제 운영에서는 키를 설정하는 편이 좋다.
    """
    today = datetime.now().strftime("%m월 %d일")
    lines = ["결론부터 말합니다. 단기 방향은 아래 뉴스 흐름에 달렸습니다."]
    for art in articles[:3]:
        headline = art.title
        if len(headline) > 70:
            headline = headline[:70] + "..."
        lines.append(headline + ".")
    lines.append("자세한 근거는 텔레그램 방에서 차트로 짚어드립니다.")
    lines.append("구독 눌러두세요.")
    return {
        "title": f"{today} 비트코인 단기 방향성",
        "description": "오늘의 암호화폐 주요 뉴스를 1분 안에 정리했습니다.",
        # 단어 중간에서 자르면 "acce" 같은 조각이 키워드에 섞인다. 단어 단위로 끊는다.
        "main_keyword": " ".join(articles[0].title.split()[:5]) if articles else "",
        "script": "\n".join(lines),
    }


def make_script(articles: list[Article], provider: str) -> dict:
    prompt = _build_prompt(articles)

    if provider == "openai":
        config.log(PHASE, f"OpenAI 호출 ({config.OPENAI_MODEL})")
        return generate_with_openai(prompt)
    if provider == "gemini":
        config.log(PHASE, f"Gemini 호출 ({config.GEMINI_MODEL})")
        return generate_with_gemini(prompt)

    config.log(PHASE, "LLM 없이 규칙 기반 대본을 생성합니다 (LLM_PROVIDER=none)")
    return generate_offline(articles)


# ---------------------------------------------------------------------------
# 3) 저장
# ---------------------------------------------------------------------------
def _sanitize_script(text: str) -> str:
    """TTS 가 읽으면 어색한 요소를 걷어낸다."""
    text = re.sub(r"[*#`>]", "", text or "")
    text = re.sub(r"\([^)]*\)", "", text)      # 괄호 지시문
    text = re.sub(r"\[[^\]]*\]", "", text)     # 대괄호 지시문
    text = re.sub(r"#\S+", "", text)           # 해시태그
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def save(result: dict, articles: list[Article]) -> str:
    config.ensure_dirs()

    script = _sanitize_script(result.get("script", ""))
    if not script:
        raise RuntimeError("대본이 비어 있습니다. LLM 응답을 확인하세요.")

    max_chars = int(config.TARGET_CHARS * 1.35)
    if len(script) > max_chars:
        config.log(PHASE, f"대본이 길어 {max_chars}자로 자릅니다 (원본 {len(script)}자)")
        cut = script[:max_chars]
        # 문장 중간에서 끊기지 않게 마지막 문장 경계까지만 남긴다.
        boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("\n"))
        script = cut[: boundary + 1] if boundary > max_chars * 0.5 else cut

    title = (result.get("title") or "").strip() or f"{datetime.now():%m월 %d일} 암호화폐 시황"
    description = (result.get("description") or "").strip() or script.split("\n")[0]

    config.SCRIPT_PATH.write_text(script, encoding="utf-8")

    main_keyword = (result.get("main_keyword") or "").strip()

    meta = {
        "title": title[:100],
        "description": description,
        "main_keyword": main_keyword,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "char_count": len(script),
        "sources": [asdict(a) for a in articles[:5]],
    }
    config.META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 다음 실행에서 같은 소재를 다시 고르지 않도록 기록한다.
    record_history(articles, main_keyword=main_keyword, title=title)

    config.log(PHASE, f"저장 완료 → {config.SCRIPT_PATH} ({len(script)}자)")
    config.log(PHASE, f"제목: {title}")
    if main_keyword:
        config.log(PHASE, f"핵심 키워드: {main_keyword}")
    return script


def main() -> int:
    parser = argparse.ArgumentParser(description="암호화폐 뉴스 → 쇼츠 대본 생성")
    parser.add_argument("--provider", choices=["openai", "gemini", "none"], default=config.LLM_PROVIDER)
    parser.add_argument("--url", help="RSS 대신 크롤링할 단일 기사 URL")
    parser.add_argument("--limit", type=int, default=config.MAX_HEADLINES)
    args = parser.parse_args()

    articles = collect_articles(args.limit, url=args.url)

    if not articles:
        config.log(PHASE, "쓸 수 있는 새 기사가 없습니다. FEED_URLS·네트워크 또는 중복 필터를 확인하세요.")
        return 1

    config.log(PHASE, f"총 {len(articles)}건의 기사로 대본을 만듭니다.")

    provider = args.provider
    if provider == "openai" and not config.OPENAI_API_KEY:
        config.log(PHASE, "OPENAI_API_KEY 가 없어 규칙 기반 폴백으로 전환합니다.")
        provider = "none"
    if provider == "gemini" and not config.GEMINI_API_KEY:
        config.log(PHASE, "GEMINI_API_KEY 가 없어 규칙 기반 폴백으로 전환합니다.")
        provider = "none"

    result = make_script(articles, provider)
    script = save(result, articles)

    print("\n----- 생성된 대본 -----")
    print(script)
    print("----------------------\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
