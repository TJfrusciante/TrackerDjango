from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from payments.models import MpSubscription
from payments.services import fetch_preapproval, extract_preapproval_fields, cancel_preapproval


class Command(BaseCommand):
    help = "Limpa preapprovals pendentes antigos e sincroniza status com o Mercado Pago."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=int(getattr(settings, "MP_PENDING_CLEANUP_DAYS", 7)),
            help="Dias para considerar um preapproval pendente como antigo.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Cancela preapprovals pendentes no Mercado Pago.",
        )
        parser.add_argument(
            "--status",
            default="pending",
            help="Status local para filtrar (default: pending).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        apply_changes = options["apply"]
        status_filter = options["status"]
        if not settings.MP_ACCESS_TOKEN:
            self.stdout.write(self.style.ERROR("MP_ACCESS_TOKEN nao configurado."))
            return

        cutoff = timezone.now() - timedelta(days=days)
        qs = (
            MpSubscription.objects.filter(status=status_filter, updated_at__lte=cutoff)
            .exclude(preapproval_id="")
            .exclude(preapproval_id__startswith="simulated-")
        )
        total = qs.count()
        updated = 0
        cancelled = 0

        for sub in qs:
            try:
                data = fetch_preapproval(sub.preapproval_id)
            except Exception as exc:
                self.stdout.write(f"{sub.user}: erro ao buscar {sub.preapproval_id}: {exc}")
                continue
            fields = extract_preapproval_fields(data)
            new_status = fields.get("status") or sub.status
            if new_status and new_status != sub.status:
                sub.status = new_status
                sub.last_payment_status = fields.get("last_payment_status") or sub.last_payment_status
                sub.save(update_fields=["status", "last_payment_status", "updated_at"])
                updated += 1

            if new_status == "pending" and apply_changes:
                try:
                    cancel_preapproval(sub.preapproval_id)
                    sub.status = "cancelled"
                    sub.save(update_fields=["status", "updated_at"])
                    cancelled += 1
                except Exception as exc:
                    self.stdout.write(f"{sub.user}: erro ao cancelar {sub.preapproval_id}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Analisados: {total} | Atualizados: {updated} | Cancelados: {cancelled}"
            )
        )
