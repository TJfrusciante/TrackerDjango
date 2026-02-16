from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest, HttpResponse


@dataclass
class MediaItem:
    media_id: str = ""
    url: str = ""
    content_type: str = ""
    kind: str = ""


@dataclass
class InboundMessage:
    from_number: str
    to_number: str
    text: str
    media: list[MediaItem] = field(default_factory=list)
    provider_message_id: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)
    message_type: str = "text"
    status_updates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_status_only(self) -> bool:
        return bool(self.status_updates) and not self.text and not self.media


class BaseProvider:
    name = "base"

    def verify_webhook(self, request: HttpRequest) -> bool:
        return True

    def parse_inbound(self, request: HttpRequest) -> InboundMessage:
        raise NotImplementedError

    def send_text(self, to_number: str, text: str) -> str:
        raise NotImplementedError

    def send_template(self, to_number: str, template_name: str, language: str, components: list[dict[str, Any]]):
        raise NotImplementedError

    def build_response(self, text: str) -> HttpResponse:
        return HttpResponse("ok")
