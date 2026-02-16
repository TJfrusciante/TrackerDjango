from __future__ import annotations

from django.conf import settings

from .base import BaseProvider
from .dialog360 import Dialog360Provider
from .twilio import TwilioProvider


def get_provider() -> BaseProvider:
    name = (getattr(settings, "WHATSAPP_PROVIDER", "twilio") or "twilio").lower()
    if name in ("360dialog", "360", "dialog360"):
        return Dialog360Provider()
    if name in ("meta", "cloud"):
        return Dialog360Provider()
    return TwilioProvider()
