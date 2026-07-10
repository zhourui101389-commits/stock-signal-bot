"""
影子模式：用免费模型（Gemini）在正式研判之外单独跑一份独立判断，
和 Claude 的正式结果并排记录到 predictions.json，只用于日后对比校准。

关键约束：
- 与 run_ai_analysis 共用 build_prompt() 拿到完全相同的输入数据，
  否则两边判断不一致时分不清是"模型不同"还是"看到的数据不同"
- 任何失败（未配置key/网络错误/JSON解析失败）都吞掉返回{}，绝不抛出，
  绝不能因为一个免费模型的调用失败拖垮主流程（Claude研判/实际下单）
- 只记录，不触发任何交易——调用方不得把这里的返回值传给执行层
"""
import json
import logging

import requests

from src.analysis.ai_analyst import SYSTEM_PROMPT, build_prompt

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-flash-latest"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)
_TIMEOUT_SEC = 30


def run_shadow_analysis(
    result,
    finnhub,
    gemini_key: str,
    macro_context: str = "",
    symbol_history: list[dict] = None,
) -> dict:
    """跑一次影子研判，仅用于记录对比。失败时返回 {}，不影响调用方。"""
    if not gemini_key:
        return {}

    symbol = result.symbol
    try:
        prompt = build_prompt(result, finnhub, macro_context, symbol_history)
    except Exception as e:
        logger.debug("影子模式(Gemini) %s 构建prompt失败: %s", symbol, e)
        return {}

    try:
        resp = requests.post(
            _GEMINI_URL,
            params={"key": gemini_key},
            json={
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
            },
            timeout=_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.info("影子模式(Gemini) %s 请求失败，跳过（不影响正式研判）: %s", symbol, e)
        return {}

    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.info("影子模式(Gemini) %s JSON解析失败，跳过: %s", symbol, e)
        return {}

    analysis["symbol"] = symbol
    analysis["_shadow_model"] = _GEMINI_MODEL
    logger.info(
        "影子研判(Gemini) %s: %s 置信度%s（仅记录，不触发交易）",
        symbol, analysis.get("final_direction"), analysis.get("conviction"),
    )
    return analysis
