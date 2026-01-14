from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any

try:
    import requests
except Exception:
    requests = None


def _mp_headers() -> dict:
    token = os.getenv('MP_ACCESS_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }


def _mp_base_url() -> str:
    return os.getenv('MP_BASE_URL', 'https://api.mercadopago.com')


def mp_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{_mp_base_url().rstrip('/')}/{path.lstrip('/')}"
    headers = _mp_headers()
    if requests is not None:
        resp = requests.request(method, url, headers=headers, json=payload, timeout=10)
        if resp.status_code >= 400:
            raise ValueError(f"MP {resp.status_code}: {resp.text}")
        return resp.json()
    data = json.dumps(payload or {}).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='ignore')
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


def extract_preapproval_fields(data: dict[str, Any]) -> dict[str, Any]:
    auto_recurring = data.get('auto_recurring') or {}
    next_payment = auto_recurring.get('next_payment_date')
    status = data.get('status') or ''
    return {
        'preapproval_id': data.get('id', ''),
        'status': status,
        'payer_email': (data.get('payer') or {}).get('email', ''),
        'reason': data.get('reason', ''),
        'auto_recurring': auto_recurring,
        'next_payment_at': next_payment,
        'last_payment_status': (data.get('last_payment') or {}).get('status', ''),
    }
