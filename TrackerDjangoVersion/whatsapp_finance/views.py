from __future__ import annotations

import re

from django.http import HttpResponse
from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .media import ocr_image, transcribe_audio
from .models import WhatsAppMessage, WhatsAppProfile
from .services import process_incoming_text
from .forms import WhatsAppProfileForm
from .whatsapp import normalize_phone
from .providers import get_provider


def _reply(provider, message: str) -> HttpResponse:
    return provider.build_response(message)


def _rate_limit(request, key, limit=120, window=60):
    ident = request.META.get("REMOTE_ADDR", "anon")
    cache_key = f"rl:{key}:{ident}"
    count = cache.get(cache_key, 0)
    if count >= limit:
        return True
    cache.set(cache_key, count + 1, window)
    return False


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
    provider = get_provider()
    if request.method != "POST":
        return _reply(provider, "Metodo nao permitido.")
    if _rate_limit(request, "whatsapp_webhook", limit=120, window=60):
        return _reply(provider, "Muitas mensagens em pouco tempo. Tente novamente.")

    if not provider.verify_webhook(request):
        return HttpResponse("forbidden", status=403)

    inbound = provider.parse_inbound(request)
    if inbound.is_status_only:
        for status in inbound.status_updates:
            status_id = status.get("id") or status.get("message_id")
            if not status_id:
                continue
            existing = WhatsAppMessage.objects.filter(
                provider=provider.name,
                provider_message_id=status_id,
            ).first()
            if existing:
                existing.raw_payload = {**existing.raw_payload, "status": status}
                existing.save(update_fields=["raw_payload"])
            else:
                WhatsAppMessage.objects.create(
                    provider=provider.name,
                    provider_message_id=status_id,
                    direction="in",
                    from_number="",
                    to_number="",
                    body="",
                    raw_payload={"status": status},
                )
        return HttpResponse("ok")

    from_number = normalize_phone(inbound.from_number)
    to_number = normalize_phone(inbound.to_number)
    body = (inbound.text or "").strip()
    media_item = inbound.media[0] if inbound.media else None
    media_url = media_item.url if media_item else ""
    media_type = media_item.content_type if media_item else ""
    media_id = media_item.media_id if media_item else ""

    if inbound.provider_message_id:
        if WhatsAppMessage.objects.filter(
            provider=provider.name, provider_message_id=inbound.provider_message_id
        ).exists():
            return HttpResponse("ok")

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
        if from_number:
            provider.send_text(from_number, "Numero nao autorizado. Cadastre seu WhatsApp no perfil.")
        return HttpResponse("ok")
    if profile.phone_number != from_number:
        profile.phone_number = from_number
        profile.save(update_fields=["phone_number"])

    profile.last_seen_at = timezone.now()
    profile.save(update_fields=["last_seen_at"])

    message = WhatsAppMessage.objects.create(
        user=profile.user,
        workspace=profile.workspace,
        provider=provider.name,
        provider_message_id=inbound.provider_message_id,
        message_sid=inbound.provider_message_id if provider.name == "twilio" else "",
        direction="in",
        from_number=from_number,
        to_number=to_number,
        body=body,
        media_url=media_url,
        media_type=media_type,
        raw_payload=inbound.raw_payload,
    )

    if media_item and media_type:
        if media_type.startswith("audio"):
            transcript = transcribe_audio(media_url, media_type, media_id=media_id)
            if transcript:
                body = transcript
                message.body = body
                message.raw_payload = {**message.raw_payload, "transcript": transcript}
                message.save(update_fields=["body", "raw_payload"])
            else:
                if provider.name == "twilio":
                    return _reply(provider, "Nao consegui transcrever o audio. Envie texto ou configure o transcritor.")
                provider.send_text(from_number, "Nao consegui transcrever o audio. Envie texto ou configure o transcritor.")
                return HttpResponse("ok")
        elif media_type.startswith("image"):
            ocr_text = ocr_image(media_url, media_id=media_id)
            if ocr_text:
                body = ocr_text
                message.body = body
                message.raw_payload = {**message.raw_payload, "ocr": ocr_text}
                message.save(update_fields=["body", "raw_payload"])
            else:
                if provider.name == "twilio":
                    return _reply(provider, "Nao consegui ler o comprovante. Envie uma foto mais nitida ou texto.")
                provider.send_text(from_number, "Nao consegui ler o comprovante. Envie uma foto mais nitida ou texto.")
                return HttpResponse("ok")

    if not body:
        response_text = "Envie texto, audio ou foto do comprovante para registrar a transacao."
    else:
        response_text = process_incoming_text(profile, message, body)

    outbound_message_id = ""
    if provider.name != "twilio":
        outbound_message_id = provider.send_text(from_number, response_text)

    WhatsAppMessage.objects.create(
        user=profile.user,
        workspace=profile.workspace,
        provider=provider.name,
        provider_message_id=outbound_message_id,
        direction="out",
        from_number=to_number,
        to_number=from_number,
        body=response_text,
        raw_payload={"reply_to": message.id},
    )

    if provider.name == "twilio":
        return _reply(provider, response_text)
    return HttpResponse("ok")
