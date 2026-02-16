from __future__ import annotations

from django.http import HttpRequest, HttpResponse

from ..whatsapp import build_twiml, normalize_phone, validate_twilio_request
from .base import BaseProvider, InboundMessage, MediaItem


class TwilioProvider(BaseProvider):
    name = "twilio"

    def verify_webhook(self, request: HttpRequest) -> bool:
        return validate_twilio_request(request)

    def parse_inbound(self, request: HttpRequest) -> InboundMessage:
        payload = request.POST.dict()
        from_number = normalize_phone(payload.get("From", ""))
        to_number = normalize_phone(payload.get("To", ""))
        text = (payload.get("Body") or "").strip()
        num_media = int(payload.get("NumMedia") or 0)
        media_url = payload.get("MediaUrl0", "")
        media_type = payload.get("MediaContentType0", "")
        media_items: list[MediaItem] = []
        if num_media > 0 and media_url:
            media_items.append(MediaItem(url=media_url, content_type=media_type, kind=media_type.split("/")[0]))
        return InboundMessage(
            from_number=from_number,
            to_number=to_number,
            text=text,
            media=media_items,
            provider_message_id=payload.get("MessageSid", ""),
            raw_payload=payload,
            message_type="media" if media_items else "text",
        )

    def send_text(self, to_number: str, text: str) -> str:
        # Twilio replies are sent via TwiML response.
        return ""

    def build_response(self, text: str) -> HttpResponse:
        return HttpResponse(build_twiml(text), content_type="text/xml")
