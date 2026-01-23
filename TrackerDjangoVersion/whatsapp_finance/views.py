from __future__ import annotations

import re

from django.http import HttpResponse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .media import ocr_image, transcribe_audio
from .models import WhatsAppMessage, WhatsAppProfile
from .services import process_incoming_text
from .forms import WhatsAppProfileForm
from .whatsapp import build_twiml, normalize_phone, validate_twilio_request


def _reply(message: str) -> HttpResponse:
    return HttpResponse(build_twiml(message), content_type="text/xml")


@login_required
def agent(request):
    profile = WhatsAppProfile.objects.filter(user=request.user).select_related("workspace").first()
    recent_messages = (
        WhatsAppMessage.objects.filter(user=request.user)
        .order_by("-created_at")[:6]
    )
    return render(
        request,
        "whatsapp_finance/agent.html",
        {
            "profile": profile,
            "recent_messages": list(recent_messages),
            "whatsapp_feature_enabled": getattr(settings, "WHATSAPP_FEATURE_ENABLED", False),
        },
    )


@login_required
def config(request):
    profile = WhatsAppProfile.objects.filter(user=request.user).first()
    form = WhatsAppProfileForm(request.POST or None, instance=profile, user=request.user)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data.get("phone_number"):
            wa_instance = form.save(commit=False)
            wa_instance.user = request.user
            wa_instance.save()
            messages.success(request, "WhatsApp configurado com sucesso.")
            return render(
                request,
                "whatsapp_finance/config.html",
                {"form": form, "profile": wa_instance, "saved": True},
            )
        messages.error(request, "Informe seu número do WhatsApp para ativar.")
    return render(request, "whatsapp_finance/config.html", {"form": form, "profile": profile})


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

    from_digits = re.sub(r"\D", "", from_number or "")
    variants = {from_number}
    if from_digits:
        variants.update({from_digits, f"+{from_digits}", from_digits.lstrip("+")})
    profile = (
        WhatsAppProfile.objects.select_related("user", "workspace")
        .filter(phone_number__in=variants, is_active=True)
        .first()
    )
    if not profile:
        return _reply("Numero nao autorizado. Cadastre seu WhatsApp no perfil.")
    if profile.phone_number != from_number:
        profile.phone_number = from_number
        profile.save(update_fields=["phone_number"])

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
