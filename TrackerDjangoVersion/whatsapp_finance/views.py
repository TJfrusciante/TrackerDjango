from __future__ import annotations

import json

from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .media import ocr_image, transcribe_audio
from .models import WhatsAppMessage, WhatsAppProfile
from .services import process_incoming_text
from .whatsapp import build_twiml, normalize_phone, validate_twilio_request


def _reply(message: str) -> HttpResponse:
    return HttpResponse(build_twiml(message), content_type="text/xml")


@csrf_exempt
def webhook(request):
    if request.method != "POST":
        return _reply("Metodo nao permitido.")

    if not validate_twilio_request(request):
        return _reply("Assinatura invalida.")

    payload = request.POST.dict()
    from_number = normalize_phone(payload.get("From", ""))
    to_number = normalize_phone(payload.get("To", ""))
    body = (payload.get("Body") or "").strip()
    num_media = int(payload.get("NumMedia") or 0)
    media_url = payload.get("MediaUrl0", "")
    media_type = payload.get("MediaContentType0", "")

    profile = WhatsAppProfile.objects.select_related("user", "workspace").filter(phone_number=from_number, is_active=True).first()
    if not profile:
        return _reply("Numero nao autorizado. Cadastre seu WhatsApp no perfil.")

    profile.last_seen_at = timezone.now()
    profile.save(update_fields=["last_seen_at"])

    message = WhatsAppMessage.objects.create(
        user=profile.user,
        workspace=profile.workspace,
        message_sid=payload.get("MessageSid", ""),
        direction="in",
        from_number=from_number,
        to_number=to_number,
        body=body,
        media_url=media_url,
        media_type=media_type,
        raw_payload=payload,
    )

    if num_media > 0 and media_type:
        if media_type.startswith("audio"):
            transcript = transcribe_audio(media_url, media_type)
            if transcript:
                body = transcript
                message.body = body
                message.raw_payload = {**message.raw_payload, "transcript": transcript}
                message.save(update_fields=["body", "raw_payload"])
            else:
                return _reply("Nao consegui transcrever o audio. Envie texto ou configure o transcritor.")
        elif media_type.startswith("image"):
            ocr_text = ocr_image(media_url)
            if ocr_text:
                body = ocr_text
                message.body = body
                message.raw_payload = {**message.raw_payload, "ocr": ocr_text}
                message.save(update_fields=["body", "raw_payload"])
            else:
                return _reply("Nao consegui ler o comprovante. Envie uma foto mais nitida ou texto.")

    if not body:
        response_text = "Envie texto, audio ou foto do comprovante para registrar a transacao."
    else:
        response_text = process_incoming_text(profile, message, body)

    WhatsAppMessage.objects.create(
        user=profile.user,
        workspace=profile.workspace,
        direction="out",
        from_number=to_number,
        to_number=from_number,
        body=response_text,
        raw_payload={"reply_to": message.id},
    )

    return _reply(response_text)
