from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any

try:
    import requests
except Exception:
    requests = None

logger = logging.getLogger(__name__)


def _sleep_backoff(attempt: int, base: float = 0.4, cap: float = 2.0) -> None:
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay)


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _mp_headers() -> dict:
    token = os.getenv('MP_ACCESS_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }


def _mp_base_url() -> str:
    return os.getenv('MP_BASE_URL', 'https://api.mercadopago.com')


def mp_request(method: str, path: str, payload: dict | None = None, retries: int = 2) -> dict:
    url = f"{_mp_base_url().rstrip('/')}/{path.lstrip('/')}"
    headers = _mp_headers()
    if requests is not None:
        for attempt in range(retries + 1):
            resp = requests.request(method, url, headers=headers, json=payload, timeout=10)
            if resp.status_code >= 400:
                if _is_retryable_status(resp.status_code) and attempt < retries:
                    logger.warning("MP retryable status %s (try %s/%s)", resp.status_code, attempt + 1, retries + 1)
                    _sleep_backoff(attempt)
                    continue
                raise ValueError(f"MP {resp.status_code}: {resp.text}")
            return resp.json()
    data = json.dumps(payload or {}).encode('utf-8') if payload is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore')
            if _is_retryable_status(exc.code) and attempt < retries:
                logger.warning("MP retryable status %s (try %s/%s)", exc.code, attempt + 1, retries + 1)
                _sleep_backoff(attempt)
                continue
            raise ValueError(f"MP {exc.code}: {body}") from exc


def build_preapproval_payload(*, reason: str, external_reference: str, back_url: str, plan_cycle: str, payer_email: str | None, amount: float) -> dict:
    if plan_cycle == 'annual':
        frequency = 12
        frequency_type = 'months'
    else:
        frequency = 1
        frequency_type = 'months'
    payload = {
        'reason': reason,
        'external_reference': external_reference,
        'auto_recurring': {
            'frequency': frequency,
            'frequency_type': frequency_type,
            'transaction_amount': float(amount),
            'currency_id': os.getenv('MP_CURRENCY', 'BRL'),
        },
        'back_url': back_url,
    }
    notification_url = os.getenv('MP_NOTIFICATION_URL', '').strip()
    if notification_url:
        payload['notification_url'] = notification_url
    if payer_email:
        payload['payer_email'] = payer_email
    return payload


def create_preapproval(payload: dict) -> dict:
    return mp_request('POST', '/preapproval', payload)


def fetch_preapproval(preapproval_id: str) -> dict:
    return mp_request('GET', f'/preapproval/{preapproval_id}')


def cancel_preapproval(preapproval_id: str) -> dict:
    return mp_request('PUT', f'/preapproval/{preapproval_id}', {'status': 'cancelled'})


def extract_preapproval_fields(data: dict[str, Any]) -> dict[str, Any]:
    auto_recurring = data.get('auto_recurring') or {}
    next_payment = auto_recurring.get('next_payment_date')
    status = data.get('status') or ''
    return {
        'preapproval_id': data.get('id', ''),
        'status': status,
        'external_reference': data.get('external_reference', ''),
        'payer_email': (data.get('payer') or {}).get('email', ''),
        'reason': data.get('reason', ''),
        'auto_recurring': auto_recurring,
        'next_payment_at': next_payment,
        'last_payment_status': (data.get('last_payment') or {}).get('status', ''),
    }
