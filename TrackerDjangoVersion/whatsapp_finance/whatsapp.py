from __future__ import annotations

import re

from django.conf import settings
from django.http import HttpRequest


def normalize_phone(value: str) -> str:
    if not value:
        return ""
    value = value.replace("whatsapp:", "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits:
        return value
    return f"+{digits}"


def build_twiml(message: str) -> str:
    try:
        from twilio.twiml.messaging_response import MessagingResponse  # type: ignore
    except Exception:
        return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
    response = MessagingResponse()
    response.message(message)
    return str(response)


def validate_twilio_request(request: HttpRequest) -> bool:
    token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    if not token or not getattr(settings, "WHATSAPP_VALIDATE_TWILIO", False):
        return True
    try:
        from twilio.request_validator import RequestValidator  # type: ignore
    except Exception:
        return True
    validator = RequestValidator(token)
    signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    url = request.build_absolute_uri()
    return validator.validate(url, request.POST, signature)
