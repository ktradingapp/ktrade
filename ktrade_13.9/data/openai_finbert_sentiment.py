"""
KTrade v10.3 - OpenAI + FinBERT Sentiment Engine
=================================================

Purpose:
- Takes company news from Finnhub.
- Scores raw financial sentiment with FinBERT when available.
- Uses OpenAI for a simple trader-friendly explanation when OPENAI_API_KEY is configured.
- Falls back to lightweight finance keywords if FinBERT/OpenAI packages are not installed.

Important:
Sentiment is confirmation only. It should not place trades by itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import dotenv_values

    project_dir = Path(__file__).resolve().parent.parent
    for env_key, env_value in dotenv_values(project_dir / ".env", encoding="utf-8-sig").items():
        if env_value is not None:
            os.environ.setdefault(env_key, str(env_value).strip())
except Exception:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_SENTIMENT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
ENABLE_OPENAI = os.environ.get("KTRADE_ENABLE_OPENAI_SENTIMENT", "true").lower() == "true"
ENABLE_FINBERT = os.environ.get("KTRADE_ENABLE_FINBERT", "true").lower() == "true"

_FINBERT_PIPELINE = None
_FINBERT_LOAD_ERROR = None

POSITIVE_WORDS = {
    "beat", "beats", "growth", "upgrade", "upgraded", "raises", "raised", "strong",
    "record", "surge", "surges", "bullish", "profit", "profitable", "outperform",
    "buy rating", "positive", "demand", "partnership", "contract", "expands", "approval",
    "launch", "accelerates", "higher", "guidance raised", "revenue growth",
}
NEGATIVE_WORDS = {
    "miss", "misses", "downgrade", "downgraded", "lawsuit", "probe", "investigation",
    "weak", "loss", "losses", "bearish", "underperform", "sell rating", "cuts",
    "cut", "lower", "decline", "falls", "fell", "slump", "warning", "recall",
    "fraud", "delay", "delayed", "guidance cut", "revenue decline",
}


@dataclass
class SentimentResult:
    ticker: str
    sentiment: str
    score: int
    confidence: str
    finbert_label: str
    finbert_confidence: float
    openai_used: bool
    finbert_used: bool
    trading_view: str
    reason: str
    warning: str
    news_count: int
    source: str = "finnhub+finbert+openai"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _news_text(news_items: List[Dict[str, Any]], limit: int = 8) -> str:
    chunks = []
    for item in (news_items or [])[:limit]:
        headline = str(item.get("headline") or "").strip()
        summary = str(item.get("summary") or "").strip()
        source = str(item.get("source") or "").strip()
        if headline or summary:
            chunks.append(f"Source: {source}\nHeadline: {headline}\nSummary: {summary}")
    return "\n\n".join(chunks).strip()


def _load_finbert():
    global _FINBERT_PIPELINE, _FINBERT_LOAD_ERROR
    if _FINBERT_PIPELINE is not None or _FINBERT_LOAD_ERROR is not None:
        return _FINBERT_PIPELINE
    if not ENABLE_FINBERT:
        _FINBERT_LOAD_ERROR = "disabled"
        return None
    try:
        from transformers import pipeline

        # Dashboard requests should not hang while downloading a 400MB+ model.
        # First download it with DOWNLOAD_FINBERT_MODEL.cmd, then backend will load from cache.
        local_only = os.environ.get("KTRADE_FINBERT_LOCAL_ONLY", "true").lower() == "true"
        _FINBERT_PIPELINE = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            local_files_only=local_only,
        )
        return _FINBERT_PIPELINE
    except Exception as exc:
        _FINBERT_LOAD_ERROR = str(exc)
        return None


def _score_with_finbert(text: str) -> Dict[str, Any]:
    pipe = _load_finbert()
    if not pipe or not text:
        return _score_with_keywords(text)
    try:
        # FinBERT has token limits. Score headline-sized chunks, then average.
        chunks = [part[:900] for part in text.split("\n\n") if part.strip()][:8]
        results = pipe(chunks)
        totals = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        for result in results:
            label = str(result.get("label", "neutral")).lower()
            score = float(result.get("score", 0.0))
            if label not in totals:
                label = "neutral"
            totals[label] += score
        label = max(totals, key=totals.get)
        confidence = totals[label] / max(1, len(results))
        sentiment_score = 50
        if label == "positive":
            sentiment_score = int(55 + min(confidence, 1.0) * 40)
        elif label == "negative":
            sentiment_score = int(45 - min(confidence, 1.0) * 40)
        return {
            "label": label,
            "confidence": round(confidence, 3),
            "score": max(0, min(100, sentiment_score)),
            "finbert_used": True,
        }
    except Exception:
        return _score_with_keywords(text)


def _score_with_keywords(text: str) -> Dict[str, Any]:
    lower = (text or "").lower()
    positive = sum(1 for word in POSITIVE_WORDS if word in lower)
    negative = sum(1 for word in NEGATIVE_WORDS if word in lower)
    raw = positive - negative
    score = max(0, min(100, 50 + raw * 10))
    if score >= 60:
        label = "positive"
    elif score <= 40:
        label = "negative"
    else:
        label = "neutral"
    confidence = min(0.85, 0.45 + abs(raw) * 0.12) if raw else 0.35
    return {
        "label": label,
        "confidence": round(confidence, 3),
        "score": score,
        "finbert_used": False,
    }


def _confidence_word(confidence: float) -> str:
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _trading_view(strategy_signal: str, sentiment: str) -> str:
    signal = (strategy_signal or "WATCH").upper()
    if signal == "BUY" and sentiment == "positive":
        return "supports_buy"
    if signal == "BUY" and sentiment == "negative":
        return "buy_with_warning"
    if signal == "WATCH" and sentiment == "positive":
        return "watch_improving"
    if sentiment == "negative":
        return "risk_warning"
    return "neutral_confirmation"


def _openai_explanation(ticker: str, text: str, base: Dict[str, Any], strategy_signal: str) -> Optional[str]:
    if not ENABLE_OPENAI or not OPENAI_API_KEY or not text:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = {
            "ticker": ticker,
            "strategy_signal": strategy_signal,
            "finbert_or_keyword_sentiment": base.get("label"),
            "sentiment_score": base.get("score"),
            "news": text[:5000],
        }
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.1,
            timeout=20,  # v10.5: bound the backend call so /ai/advisor can't hang
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You explain financial news sentiment for a paper-trading dashboard. "
                        "Do not give financial advice. Do not say to buy or sell. "
                        "Explain whether the news supports, conflicts with, or is neutral to the existing strategy signal. "
                        "Keep it under 45 words."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt)},
            ],
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return None


def analyze_ticker_sentiment(
    ticker: str,
    news_items: List[Dict[str, Any]],
    strategy_signal: str = "WATCH",
) -> Dict[str, Any]:
    ticker = (ticker or "UNK").upper()
    text = _news_text(news_items)
    base = _score_with_finbert(text)
    label = base["label"]
    score = int(base["score"])
    confidence_value = float(base.get("confidence", 0.0))

    ai_explanation = _openai_explanation(ticker, text, base, strategy_signal)
    openai_used = bool(ai_explanation)  # v10.5: True only when OpenAI actually returned
    explanation = ai_explanation
    if not explanation:
        if not news_items:
            explanation = "No recent Finnhub news was available for this ticker. Sentiment should not affect the signal."
        elif label == "positive":
            explanation = "Recent financial news appears positive overall. Treat this as confirmation only, not a trade trigger."
        elif label == "negative":
            explanation = "Recent financial news appears negative overall. Review risk before acting on any strategy signal."
        else:
            explanation = "Recent financial news is mixed or neutral. Strategy and risk rules should remain primary."

    result = SentimentResult(
        ticker=ticker,
        sentiment=label,
        score=score,
        confidence=_confidence_word(confidence_value),
        finbert_label=label,
        finbert_confidence=round(confidence_value, 3),
        openai_used=openai_used,
        finbert_used=bool(base.get("finbert_used")),
        trading_view=_trading_view(strategy_signal, label),
        reason=explanation,
        warning="Sentiment is confirmation only. It must not place trades by itself.",
        news_count=len(news_items or []),
    )
    return result.to_dict()
