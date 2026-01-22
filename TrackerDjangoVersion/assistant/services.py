from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Optional, Tuple, Dict

logger = logging.getLogger(__name__)

try:
    import requests
except Exception:  # pragma: no cover - fallback se requests nao estiver instalado
    requests = None


def _sleep_backoff(attempt: int, base: float = 0.4, cap: float = 2.0) -> None:
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay)


def _post_with_requests(url: str, headers: dict, payload: dict, timeout: float, retries: int = 2):
    """
    Faz POST com requests (se disponivel) com pequenas tentativas e logs.
    Retorna (reply, usage, model) ou None.
    """
    if requests is None:
        return None

    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RuntimeError(f"LLM retryable status {resp.status_code}")
                resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            model = data.get("model") or payload.get("model")
            logger.info("LLM call success (requests) model=%s tokens=%s", model, usage.get("total_tokens"))
            return reply, usage, model
        except Exception as exc:  # pragma: no cover - mantemos log
            is_retryable = not (requests and isinstance(exc, requests.HTTPError))
            logger.warning("LLM call failed (try %s/%s): %s", attempt + 1, retries + 1, exc)
            if attempt < retries and is_retryable:
                _sleep_backoff(attempt)
                continue
            return None
    return None


def llm_complete(system_prompt: str, user_message: str) -> Tuple[Optional[str], Optional[Dict], Optional[str]]:
    """
    Faz chamada ao OpenAI Chat completions com timeout curto.
    Retorna (reply, usage, model) ou (None, None, None) se falhar.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    if not api_key:
        return None, None, None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = "https://api.openai.com/v1/chat/completions"
    timeout = float(os.getenv("OPENAI_TIMEOUT", "8"))

    result = _post_with_requests(url, headers, payload, timeout=timeout)
    if result is not None:
        reply, usage, model_name = result
        return reply, usage, model_name

    body = json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                reply = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                model_name = data.get("model") or payload.get("model")
                logger.info("LLM call success (urllib) model=%s tokens=%s", model_name, usage.get("total_tokens"))
                return reply, usage, model_name
        except urllib.error.HTTPError as exc:  # pragma: no cover - fallback
            retryable = exc.code == 429 or exc.code >= 500
            logger.warning("LLM call failed (urllib) try %s/%s: %s", attempt + 1, retries + 1, exc)
            if attempt < retries and retryable:
                _sleep_backoff(attempt)
                continue
            return None, None, None
        except Exception as exc:  # pragma: no cover - cobrimos via retorno None
            logger.warning("LLM call failed (urllib) try %s/%s: %s", attempt + 1, retries + 1, exc)
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            return None, None, None


def llm_reply(system_prompt: str, user_message: str) -> Optional[str]:
    """
    Compat: retorna apenas o texto da resposta.
    """
    reply, _usage, _model = llm_complete(system_prompt, user_message)
    return reply


def estimate_costs(usage: Optional[Dict]) -> Tuple[Decimal, Decimal]:
    """
    Calcula custo estimado em USD e BRL (com multiplicador).
    """
    usage = usage or {}
    total_tokens = Decimal(str(usage.get("total_tokens") or 0))
    price_per_1k = Decimal(os.getenv("OPENAI_PRICE_PER_1K_USD", "0.002"))
    usd_brl = Decimal(os.getenv("USD_BRL_RATE", "5.0"))
    multiplier = Decimal(os.getenv("OPENAI_COST_MULTIPLIER", "2"))
    cost_usd = (total_tokens / Decimal("1000")) * price_per_1k
    cost_brl = cost_usd * usd_brl * multiplier
    return cost_usd, cost_brl
