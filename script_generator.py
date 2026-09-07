import logging
import os
from dataclasses import dataclass

import yfinance as yf
from google import genai

logger = logging.getLogger(__name__)

WATCHLIST = ["AAPL", "TSLA", "NVDA"]
GEMINI_MODEL = "gemini-2.5-flash"


@dataclass
class MarketData:
    spy_close: float
    spy_change_pct: float
    mover_ticker: str
    mover_change_pct: float


def _pct_change(prev_close: float, close: float) -> float:
    return round((close - prev_close) / prev_close * 100, 2)


def fetch_market_data() -> MarketData:
    """전날 SPY 종가/등락률과 AAPL/TSLA/NVDA 중 등락폭이 가장 큰 종목을 가져온다. (동기/블로킹 — 호출부에서 스레드로 돌릴 것)"""
    spy_hist = yf.Ticker("SPY").history(period="5d")
    if len(spy_hist) < 2:
        raise RuntimeError("Not enough SPY price history to compute change")

    spy_close = float(spy_hist["Close"].iloc[-1])
    spy_change_pct = _pct_change(float(spy_hist["Close"].iloc[-2]), spy_close)

    best_ticker = None
    best_change_pct = 0.0
    for ticker in WATCHLIST:
        hist = yf.Ticker(ticker).history(period="5d")
        if len(hist) < 2:
            logger.warning("Not enough history for %s, skipping", ticker)
            continue
        change_pct = _pct_change(float(hist["Close"].iloc[-2]), float(hist["Close"].iloc[-1]))
        if best_ticker is None or abs(change_pct) > abs(best_change_pct):
            best_ticker = ticker
            best_change_pct = change_pct

    if best_ticker is None:
        raise RuntimeError("Could not fetch watchlist mover data")

    return MarketData(
        spy_close=round(spy_close, 2),
        spy_change_pct=spy_change_pct,
        mover_ticker=best_ticker,
        mover_change_pct=best_change_pct,
    )


def generate_script(data: MarketData) -> str:
    """Gemini로 60초 쇼츠 대본 생성. 프롬프트로 '제공된 수치만 사용'을 강제한다. (동기/블로킹 — 호출부에서 스레드로 돌릴 것)"""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    spy_direction = "상승" if data.spy_change_pct >= 0 else "하락"
    mover_direction = "급등" if data.mover_change_pct >= 0 else "급락"

    prompt = f"""너는 유튜브 채널 'MoneyLogic'의 금융 전문가야. 아래 데이터만 사용해서 60초 분량 유튜브 쇼츠 대본을 작성해.

[오늘의 데이터]
- S&P 500(SPY) 종가: {data.spy_close}달러, 전일 대비 {spy_direction} {abs(data.spy_change_pct)}%
- 특징주: {data.mover_ticker}, 전일 대비 {mover_direction} {abs(data.mover_change_pct)}%

[작성 규칙 - 반드시 지킬 것]
1. 첫 문장은 시청자의 이목을 끄는 질문형 후킹(Hook)으로 시작한다.
2. 어려운 금융 용어는 절대 쓰지 않는다. 중학생도 이해할 수 있게 쉬운 말로 설명한다.
3. 위에 제공된 수치 외의 다른 숫자나 사실을 절대 지어내지 않는다.
4. 성우가 읽었을 때 약 60초(200~230자 내외) 분량으로 작성한다.
5. 영상 자막으로 그대로 쓰일 나레이션 문장만 출력하고, 제목/이모지/괄호 설명은 넣지 않는다.
"""
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    script = (response.text or "").strip()
    if not script:
        raise RuntimeError("Gemini returned an empty script")
    return script
