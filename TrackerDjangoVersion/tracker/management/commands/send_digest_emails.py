import datetime
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from tracker.models import Notification, Task, Transaction, UserProfile, Workspace, WorkspaceMembership


def _period_range(kind: str, today):
    if kind == 'weekly':
        start = today - datetime.timedelta(days=7)
        return start, today
    if kind == 'monthly':
        return today.replace(day=1), today
    return today - datetime.timedelta(days=7), today


def _format_currency(value):
    return f'R$ {value:.2f}'


def _build_digest(user, start, end):
    lines = [f'Resumo de {start:%d/%m/%Y} a {end:%d/%m/%Y}']
    owned_workspaces = list(Workspace.objects.filter(owner=user, is_active=True))
    if owned_workspaces:
        tx_qs = Transaction.objects.filter(workspace__in=owned_workspaces, date__gte=start, date__lte=end)
        income = tx_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or Decimal('0')
        expense = tx_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or Decimal('0')
        balance = income - expense
        lines.append('')
        lines.append('Financeiro (workspaces do owner):')
        lines.append(f'- Entradas: {_format_currency(income)}')
        lines.append(f'- Sa\u00eddas: {_format_currency(expense)}')
        lines.append(f'- Saldo: {_format_currency(balance)}')
    else:
        lines.append('')
        lines.append('Financeiro: voc\u00ea n\u00e3o possui workspaces como owner.')

    membership_ws_ids = list(
        WorkspaceMembership.objects.filter(user=user, workspace__is_active=True).values_list('workspace_id', flat=True)
    )
    task_ws_ids = set(membership_ws_ids) | {ws.id for ws in owned_workspaces}
    if task_ws_ids:
        task_qs = Task.objects.filter(workspace_id__in=task_ws_ids, updated_at__date__gte=start, updated_at__date__lte=end)
        open_count = task_qs.filter(status='ongoing').count()
        done_count = task_qs.filter(status='done').count()
        lines.append('')
        lines.append('Tarefas (workspaces com acesso):')
        lines.append(f'- Em andamento: {open_count}')
        lines.append(f'- Conclu\u00eddas: {done_count}')
    else:
        lines.append('')
        lines.append('Tarefas: nenhuma atividade registrada no per\u00edodo.')
    return '\n'.join(lines)


class Command(BaseCommand):
    help = 'Send weekly/monthly digest emails based on user preferences.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        profiles = UserProfile.objects.select_related('user').filter(user__is_active=True, is_guest=False)
        total_sent = 0

        for profile in profiles:
            user = profile.user
            if not user.email:
                continue
            for kind, flag, last_field in (
                ('weekly', profile.digest_weekly, 'digest_weekly_last_sent'),
                ('monthly', profile.digest_monthly, 'digest_monthly_last_sent'),
            ):
                if not flag:
                    continue
                last_sent = getattr(profile, last_field)
                start, end = _period_range(kind, today)
                if last_sent and last_sent >= start:
                    continue
                body = _build_digest(user, start, end)
                label = "semanal" if kind == "weekly" else "mensal"
                subject = f"Resumo {label}"
                send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
                Notification.objects.create(
                    user=user,
                    title=subject,
                    body=body,
                    level='info',
                )
                setattr(profile, last_field, today)
                profile.save(update_fields=[last_field])
                total_sent += 1

        self.stdout.write(self.style.SUCCESS(f'Resumos enviados: {total_sent}'))
