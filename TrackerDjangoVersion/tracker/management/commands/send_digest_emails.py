import datetime
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone

from tracker.models import Notification, Task, Transaction, UserProfile, Workspace, WorkspaceMembership


def _period_range(kind: str, today):
    if kind == 'weekly':
        start = today - datetime.timedelta(days=7)
        return start, today
    if kind == 'monthly':
        return today.replace(day=1), today
    return today - datetime.timedelta(days=7), today


def _digest_context(user, start, end):
    owned_workspaces = list(Workspace.objects.filter(owner=user, is_active=True))
    has_finance = bool(owned_workspaces)
    income = Decimal('0')
    expense = Decimal('0')
    balance = Decimal('0')
    if has_finance:
        tx_qs = Transaction.objects.filter(workspace__in=owned_workspaces, date__gte=start, date__lte=end)
        income = tx_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or Decimal('0')
        expense = tx_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or Decimal('0')
        balance = income - expense

    membership_ws_ids = list(
        WorkspaceMembership.objects.filter(user=user, workspace__is_active=True).values_list('workspace_id', flat=True)
    )
    task_ws_ids = set(membership_ws_ids) | {ws.id for ws in owned_workspaces}
    has_tasks = bool(task_ws_ids)
    open_count = 0
    done_count = 0
    if has_tasks:
        task_qs = Task.objects.filter(
            workspace_id__in=task_ws_ids,
            updated_at__date__gte=start,
            updated_at__date__lte=end,
        )
        open_count = task_qs.filter(status='ongoing').count()
        done_count = task_qs.filter(status='done').count()

    return {
        'start': start,
        'end': end,
        'has_finance': has_finance,
        'income': income,
        'expense': expense,
        'balance': balance,
        'has_tasks': has_tasks,
        'open_count': open_count,
        'done_count': done_count,
    }


class Command(BaseCommand):
    help = 'Send weekly/monthly digest emails based on user preferences.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--kind',
            choices=('weekly', 'monthly'),
            help='Envie apenas o resumo semanal ou mensal.',
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        kind_filter = options.get('kind')
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
                if kind_filter and kind != kind_filter:
                    continue
                if not flag:
                    continue
                last_sent = getattr(profile, last_field)
                start, end = _period_range(kind, today)
                if last_sent and last_sent >= start:
                    continue
                context = _digest_context(user, start, end)
                context['label'] = "semanal" if kind == "weekly" else "mensal"
                context['user'] = user
                text_body = render_to_string('emails/digest.txt', context).strip()
                html_body = render_to_string('emails/digest.html', context)
                label = "semanal" if kind == "weekly" else "mensal"
                subject = f"Resumo {label}"
                msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [user.email])
                msg.attach_alternative(html_body, "text/html")
                msg.send(fail_silently=True)
                Notification.objects.create(
                    user=user,
                    title=subject,
                    body=text_body,
                    level='info',
                )
                setattr(profile, last_field, today)
                profile.save(update_fields=[last_field])
                total_sent += 1

        if kind_filter:
            label = "semanal" if kind_filter == "weekly" else "mensal"
            self.stdout.write(self.style.SUCCESS(f'Resumos {label} enviados: {total_sent}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Resumos enviados: {total_sent}'))
