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
from datetime import datetime

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
# 2) 대본 생성
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = """당신은 한국어 암호화폐 유튜브 쇼츠 채널의 작가입니다.

아래 최신 뉴스를 바탕으로 유튜브 쇼츠(60초 미만) 대본을 작성하세요.

[규칙]
- 전체 분량은 공백 포함 {target_chars}자 내외. 절대 {max_chars}자를 넘기지 마세요.
- 도입 1문장은 스크롤을 멈추게 하는 강한 훅으로 시작합니다.
- 그 뒤 핵심 뉴스 2~3개를 숫자와 함께 짧게 전달합니다.
- 마지막은 구독을 유도하는 한 문장으로 마무리합니다.
- 성우가 소리 내어 읽을 문장만 쓰세요. 화면 지시문, 괄호 설명, 이모지, 마크다운, 해시태그 금지.
- 한 문장은 짧게 끊고, 문장마다 줄바꿈하세요.
- 투자 권유 표현은 쓰지 말고 사실 전달과 시황 설명에 집중하세요.

[오늘 날짜] {today}

[뉴스 원문]
{news}

[출력 형식] 아래 JSON 만 출력하세요. 다른 텍스트를 붙이지 마세요.
{{"title": "유튜브 제목 (40자 이내, 클릭을 부르되 과장 금지)",
  "description": "유튜브 설명란 (2~3문장)",
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
            {"role": "system", "content": "너는 간결하고 정확한 한국어 유튜브 쇼츠 작가다."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )
    return _parse_llm_json(resp.choices[0].message.content or "")


def generate_with_gemini(prompt: str) -> dict:
    from google import genai

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 가 .env 에 없습니다.")

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resp = client.models.generate_content(model=config.GEMINI_MODEL, contents=prompt)
    return _parse_llm_json(resp.text or "")


def generate_offline(articles: list[Article]) -> dict:
    """API 키가 없을 때 쓰는 규칙 기반 폴백.

    LLM 없이도 파이프라인 전체(TTS→렌더링→업로드)를 끝까지 돌려볼 수 있게 한다.
    품질은 LLM 대본보다 떨어지므로 실제 운영에서는 키를 설정하는 편이 좋다.
    """
    today = datetime.now().strftime("%m월 %d일")
    lines = [f"{today} 암호화폐 시장 브리핑입니다."]
    for art in articles[:3]:
        headline = art.title
        if len(headline) > 70:
            headline = headline[:70] + "..."
        lines.append(headline + ".")
    lines.append("자세한 시황은 매일 이 채널에서 확인하세요.")
    script = "\n".join(lines)
    return {
        "title": f"{today} 비트코인 시황 브리핑",
        "description": "오늘의 암호화폐 주요 뉴스를 1분 안에 정리했습니다.",
        "script": script,
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

    meta = {
        "title": title[:100],
        "description": description,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "char_count": len(script),
        "sources": [asdict(a) for a in articles[:5]],
    }
    config.META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    config.log(PHASE, f"저장 완료 → {config.SCRIPT_PATH} ({len(script)}자)")
    config.log(PHASE, f"제목: {title}")
    return script


def main() -> int:
    parser = argparse.ArgumentParser(description="암호화폐 뉴스 → 쇼츠 대본 생성")
    parser.add_argument("--provider", choices=["openai", "gemini", "none"], default=config.LLM_PROVIDER)
    parser.add_argument("--url", help="RSS 대신 크롤링할 단일 기사 URL")
    parser.add_argument("--limit", type=int, default=config.MAX_HEADLINES)
    args = parser.parse_args()

    if args.url:
        articles = fetch_from_url(args.url)
    else:
        articles = fetch_from_feeds(config.FEED_URLS, args.limit)

    if not articles:
        config.log(PHASE, "수집된 기사가 없습니다. FEED_URLS 설정 또는 네트워크를 확인하세요.")
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
