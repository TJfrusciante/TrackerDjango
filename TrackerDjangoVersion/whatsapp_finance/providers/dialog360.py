from __future__ import annotations

import json
from typing import Any

import requests
from django.conf import settings
from django.http import HttpRequest

from ..whatsapp import normalize_phone
from .base import BaseProvider, InboundMessage, MediaItem


class Dialog360Provider(BaseProvider):
    name = "360dialog"

    def verify_webhook(self, request: HttpRequest) -> bool:
        secret = getattr(settings, "WHATSAPP_WEBHOOK_SECRET", "")
        if not secret:
            return True
        header = request.META.get("HTTP_X_WEBHOOK_SECRET", "")
        return header == secret

    def parse_inbound(self, request: HttpRequest) -> InboundMessage:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        entries = payload.get("entry") or []
        value: dict[str, Any] = {}
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                break
            if value:
                break

        metadata = value.get("metadata") or {}
        messages = value.get("messages") or []
        statuses = value.get("statuses") or []

        if not messages and statuses:
            return InboundMessage(
                from_number="",
                to_number="",
                text="",
                media=[],
                provider_message_id="",
                raw_payload=payload,
                message_type="status",
                status_updates=statuses,
            )

        if not messages:
            return InboundMessage(
                from_number="",
                to_number="",
                text="",
                media=[],
                provider_message_id="",
                raw_payload=payload,
            )

        message = messages[0]
        msg_type = message.get("type") or "text"
        from_number = normalize_phone(message.get("from", ""))
        to_number = normalize_phone(metadata.get("display_phone_number", "") or metadata.get("phone_number_id", ""))
        provider_message_id = message.get("id") or ""

        text = ""
        media_items: list[MediaItem] = []

        if msg_type == "text":
            text = (message.get("text") or {}).get("body", "")
        elif msg_type == "interactive":
            interactive = message.get("interactive") or {}
            interactive_type = interactive.get("type")
            if interactive_type == "button_reply":
                reply = interactive.get("button_reply") or {}
                text = reply.get("title") or reply.get("id") or ""
            elif interactive_type == "list_reply":
                reply = interactive.get("list_reply") or {}
                text = reply.get("title") or reply.get("id") or ""
        elif msg_type == "button":
            text = (message.get("button") or {}).get("text", "")
        else:
            media_obj = message.get(msg_type) or {}
            media_id = media_obj.get("id", "")
            content_type = media_obj.get("mime_type", "")
            media_items.append(
                MediaItem(
                    media_id=media_id,
                    url="",
                    content_type=content_type,
                    kind=msg_type,
                )
            )

        return InboundMessage(
            from_number=from_number,
            to_number=to_number,
            text=(text or "").strip(),
            media=media_items,
            provider_message_id=provider_message_id,
            raw_payload=payload,
            message_type=msg_type,
        )

    def send_text(self, to_number: str, text: str) -> str:
        api_key = getattr(settings, "D360_API_KEY", "")
        base_url = getattr(settings, "D360_BASE_URL", "https://waba-v2.360dialog.io").rstrip("/")
        if not api_key or not base_url:
            return ""

        target = to_number.replace("+", "")
        payload = {
            "messaging_product": "whatsapp",
            "to": target,
            "type": "text",
            "text": {"body": text},
        }
        headers = {"D360-API-KEY": api_key}
        try:
            resp = requests.post(f"{base_url}/messages", json=payload, headers=headers, timeout=20)
            if resp.status_code >= 400:
                return ""
            data = resp.json() or {}
            messages = data.get("messages") or []
            if messages:
                return messages[0].get("id", "") or ""
            return ""
        except Exception:
            return ""
