import json
import os
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _fetch_site_id(base_url: str, token: str) -> str | None:
    url = f"{base_url}/users/me"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return body.get("site_id")


class Command(BaseCommand):
    help = "Cria um usuario de teste do Mercado Pago (sandbox)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            dest="site_id",
            default="",
            help="Site ID (ex: MLB). Deixe vazio para detectar automaticamente.",
        )

    def handle(self, *args, **options):
        prod_token = os.getenv("MP_ACCESS_TOKEN_PROD", "").strip()
        token = prod_token or (getattr(settings, "MP_ACCESS_TOKEN", "") or "")
        if not token:
            raise CommandError("MP_ACCESS_TOKEN nao configurado no .env/.settings.")
        if prod_token:
            self.stdout.write(self.style.WARNING("Usando MP_ACCESS_TOKEN_PROD para criar usuario teste."))
        if token.startswith("TEST-"):
            raise CommandError(
                "Para criar usuario de teste e necessario usar o Access Token de PRODUCAO. "
                "Defina MP_ACCESS_TOKEN_PROD no .env e execute novamente."
            )

        site_id = options["site_id"].strip().upper()
        base_url = (getattr(settings, "MP_BASE_URL", "") or "https://api.mercadopago.com").rstrip("/")
        detected_site = _fetch_site_id(base_url, token) or ""
        if detected_site:
            if site_id and site_id != detected_site:
                self.stdout.write(
                    self.style.WARNING(
                        f"Site_id informado ({site_id}) difere do detectado ({detected_site}). Usando {detected_site}."
                    )
                )
            site_id = detected_site
        if not site_id:
            raise CommandError("Nao foi possivel detectar o site_id. Informe --site MLB.")
        url = f"{base_url}/users/test"
        payload = {"site_id": site_id}
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise CommandError(f"Erro ao criar usuario teste: {exc.code} {error_body}") from exc
        except urllib.error.URLError as exc:
            raise CommandError(f"Erro de conexao: {exc}") from exc

        email = body.get("email")
        password = body.get("password")
        nickname = body.get("nickname")
        user_id = body.get("id")
        access_token = body.get("access_token")
        public_key = body.get("public_key")

        self.stdout.write(self.style.SUCCESS("Usuario de teste criado com sucesso:"))
        self.stdout.write(f"- id: {user_id}")
        self.stdout.write(f"- nickname: {nickname}")
        self.stdout.write(f"- email: {email}")
        self.stdout.write(f"- senha: {password}")
        if access_token:
            self.stdout.write(f"- access_token: {access_token}")
        if public_key:
            self.stdout.write(f"- public_key: {public_key}")
