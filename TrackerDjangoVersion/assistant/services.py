import json
import logging
import os
import time
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import requests
except Exception:  # pragma: no cover - fallback se requests não estiver instalado
    requests = None


def _post_with_requests(url: str, headers: dict, payload: dict, timeout: float, retries: int = 2) -> Optional[str]:
    """
    Faz POST com requests (se disponível) com pequenas tentativas e logs.
    """
    if requests is None:
        return None

    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            logger.info("LLM call success (requests) model=%s tokens=%s", payload.get("model"), data.get("usage", {}).get("total_tokens"))
            return reply
        except Exception as exc:  # pragma: no cover - mantemos log
            logger.warning("LLM call failed (try %s/%s): %s", attempt, retries + 1, exc)
            if attempt <= retries:
                time.sleep(0.5)
    return None


def llm_reply(system_prompt: str, user_message: str) -> Optional[str]:
    """
    Faz chamada ao OpenAI Chat completions com timeout curto.
    Retorna None se API key não estiver configurada ou se a chamada falhar.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    if not api_key:
        return None

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

    reply = _post_with_requests(url, headers, payload, timeout=timeout)
    if reply is not None:
        return reply

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"]
            logger.info("LLM call success (urllib) model=%s tokens=%s", payload.get("model"), data.get("usage", {}).get("total_tokens"))
            return reply
    except Exception as exc:  # pragma: no cover - cobrimos via retorno None
        logger.warning("LLM call failed (urllib): %s", exc)
        return None
