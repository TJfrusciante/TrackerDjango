from __future__ import annotations

import io
import os
import tempfile
import urllib.request
import base64

from django.conf import settings


def _download_media(url: str) -> bytes | None:
    if not url:
        return None
    req = urllib.request.Request(url)
    meta_token = getattr(settings, "META_WA_ACCESS_TOKEN", "")
    if meta_token and any(host in url for host in ("facebook.com", "fbcdn.net", "fbsbx.com", "lookaside")):
        req.add_header("Authorization", f"Bearer {meta_token}")
    else:
        account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
        auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
        if account_sid and auth_token:
            auth = f"{account_sid}:{auth_token}".encode("utf-8")
            auth_header = base64.b64encode(auth).decode("utf-8")
            req.add_header("Authorization", f"Basic {auth_header}")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read()
    except Exception:
        return None


def transcribe_audio(url: str, content_type: str) -> str | None:
    provider = getattr(settings, "WHATSAPP_TRANSCRIBE_PROVIDER", "none")
    if provider == "none":
        return None
    blob = _download_media(url)
    if not blob:
        return None
    suffix = ".ogg" if "ogg" in content_type else ".mp3"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(blob)
        tmp_path = tmp.name
    try:
        if provider == "whisper":
            try:
                import whisper  # type: ignore
            except Exception:
                return None
            model_name = getattr(settings, "WHATSAPP_WHISPER_MODEL", "base")
            model = whisper.load_model(model_name)
            result = model.transcribe(tmp_path)
            return (result or {}).get("text", "").strip() or None
        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            if not api_key:
                return None
            model = getattr(settings, "WHATSAPP_OPENAI_MODEL", "whisper-1")
            try:
                import requests  # type: ignore
            except Exception:
                return None
            headers = {"Authorization": f"Bearer {api_key}"}
            with open(tmp_path, "rb") as audio_file:
                files = {"file": (os.path.basename(tmp_path), audio_file, content_type or "audio/mpeg")}
                data = {"model": model, "language": "pt"}
                try:
                    resp = requests.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers=headers,
                        files=files,
                        data=data,
                        timeout=30,
                    )
                    if resp.status_code >= 400:
                        return None
                    payload = resp.json()
                    text = payload.get("text") or ""
                    return text.strip() or None
                except Exception:
                    return None
        if provider == "google":
            try:
                from google.cloud import speech  # type: ignore
            except Exception:
                return None
            client = speech.SpeechClient()
            audio = speech.RecognitionAudio(content=blob)
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
                language_code="pt-BR",
                enable_automatic_punctuation=True,
            )
            response = client.recognize(config=config, audio=audio)
            if response.results:
                return response.results[0].alternatives[0].transcript.strip()
            return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return None


def ocr_image(url: str) -> str | None:
    if not getattr(settings, "WHATSAPP_OCR_ENABLED", True):
        return None
    blob = _download_media(url)
    if not blob:
        return None
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return None
    try:
        image = Image.open(io.BytesIO(blob))
    except Exception:
        return None
    try:
        return pytesseract.image_to_string(image, lang="por").strip()
    except Exception:
        return None
