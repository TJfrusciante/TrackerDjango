from __future__ import annotations

import datetime
import json
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db.models import Sum
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import (
    BalanceGoal,
    CategoryBudget,
    Notification,
    PushSubscription,
    Transaction,
    UserProfile,
)

User = get_user_model()

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - optional dependency
    WebPushException = Exception
    webpush = None


def _send_email(to_email: str, subject: str, body: str, action_url: str = '') -> bool:
    if not to_email:
        return False
    try:
        body_lines = [line.strip().lstrip('-').strip() for line in body.splitlines() if line.strip()]
        html_body = render_to_string(
            'emails/notification.html',
            {
                'subject': subject,
                'body': body,
                'body_lines': body_lines,
                'action_url': action_url,
            },
        )
        msg = EmailMultiAlternatives(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)
        return True
    except Exception:
        return False


def _get_profile(user):
    if not user:
        return None
    profile = getattr(user, 'profile', None)
    if profile:
        return profile
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def _create_notification(user, title: str, body: str = '', level: str = 'info', workspace=None, action_url: str = ''):
    if not user or not user.is_active:
        return None
    return Notification.objects.create(
        user=user,
        workspace=workspace,
        title=title,
        body=body,
        level=level,
        action_url=action_url or '',
    )


def _send_push(user, title: str, body: str, action_url: str = '') -> None:
    if not webpush:
        return
    vapid_public = getattr(settings, 'VAPID_PUBLIC_KEY', '')
    vapid_private = getattr(settings, 'VAPID_PRIVATE_KEY', '')
    vapid_subject = getattr(settings, 'VAPID_SUBJECT', '')
    if not (vapid_public and vapid_private and vapid_subject):
        return
    payload = json.dumps({'title': title, 'body': body, 'url': action_url})
    subs = PushSubscription.objects.filter(user=user)
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={'sub': vapid_subject},
            )
        except WebPushException:
            sub.delete()


def _notify_superusers(pref_field: str, subject: str, body: str, level: str = 'info', action_url: str = '') -> None:
    for admin in User.objects.filter(is_superuser=True, is_active=True).select_related('profile'):
        profile = _get_profile(admin)
        if profile and getattr(profile, pref_field, False):
            _send_email(admin.email, subject, body, action_url)
            _create_notification(admin, subject, body, level=level, action_url=action_url)
            _send_push(admin, subject, body, action_url)


def notify_new_account(user) -> None:
    subject = 'Novo cadastro pendente de aprova\u00e7\u00e3o'
    body = (
        f'Um novo usu\u00e1rio se cadastrou e est\u00e1 aguardando aprova\u00e7\u00e3o.\n\n'
        f'Usu\u00e1rio: {user.username}\n'
        f'Nome: {user.get_full_name() or "-"}\n'
        f'E-mail: {user.email or "-"}\n'
    )
    _notify_superusers('notify_admin_new_account', subject, body, level='warning', action_url=reverse('tracker:user_admin_list'))


def notify_contact_message(user, topic: str, subject: str, message: str) -> None:
    pref = 'notify_admin_payment_request' if topic == 'payment' else 'notify_admin_contact'
    label = {'general': 'Contato', 'payment': 'Pagamento', 'support': 'Suporte'}.get(topic, 'Contato')
    body = (
        f'Novo contato recebido ({label}).\n\n'
        f'Usu\u00e1rio: {user.username}\n'
        f'Nome: {user.get_full_name() or "-"}\n'
        f'E-mail: {user.email or "-"}\n\n'
        f'Assunto: {subject}\n\n'
        f'Mensagem:\n{message}\n'
    )
    _notify_superusers(pref, f'[iTracker] {label} - {subject}', body, level='info', action_url=reverse('tracker:contact_admin'))


def _workspace_balance(workspace, start_date=None, end_date=None):
    if not workspace:
        return Decimal('0')
    qs = Transaction.objects.filter(workspace=workspace)
    if start_date:
        qs = qs.filter(date__gte=start_date)
    if end_date:
        qs = qs.filter(date__lte=end_date)
    income = qs.filter(type='income').aggregate(total=Sum('value'))['total'] or Decimal('0')
    expense = qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or Decimal('0')
    return income - expense


def _period_range(period: str, reference_date=None):
    ref = reference_date or timezone.localdate()
    if period == 'weekly':
        start = ref - datetime.timedelta(days=ref.weekday())
        return start, ref
    if period == 'monthly':
        return ref.replace(day=1), ref
    return ref.replace(day=1), ref


def notify_balance_threshold(user, workspace) -> None:
    profile = _get_profile(user)
    if not profile or profile.is_guest or not profile.notify_balance_threshold:
        return
    threshold = profile.balance_threshold or Decimal('0')
    if threshold <= 0:
        return
    balance = _workspace_balance(workspace)
    last_value = profile.balance_alert_last_value or Decimal('0')
    if abs(last_value) < threshold <= abs(balance):
        subject = 'Alerta de saldo atingido'
        body = (
            f'Seu saldo acumulado atingiu o limite configurado.\n\n'
            f'Workspace: {workspace.name if workspace else "-"}\n'
            f'Saldo atual: R$ {balance:.2f}\n'
            f'Limite configurado: R$ {threshold:.2f}\n'
        )
        _send_email(user.email, subject, body, reverse('tracker:dashboard'))
        _create_notification(user, subject, body, level='warning', workspace=workspace, action_url=reverse('tracker:dashboard'))
        _send_push(user, subject, body, reverse('tracker:dashboard'))
        profile.balance_alert_last_value = balance
        profile.save(update_fields=['balance_alert_last_value'])
        return
    if abs(balance) < threshold:
        profile.balance_alert_last_value = balance
        profile.save(update_fields=['balance_alert_last_value'])


def check_category_budgets(workspace, reference_date=None):
    if not workspace:
        return []
    budgets = CategoryBudget.objects.select_related('category').filter(workspace=workspace)
    if not budgets.exists():
        return []
    owner = workspace.owner
    profile = _get_profile(owner)
    alerts = []
    for budget in budgets:
        start, end = _period_range(budget.period, reference_date)
        total = (
            Transaction.objects.filter(
                workspace=workspace,
                category=budget.category,
                type='expense',
                date__gte=start,
                date__lte=end,
            ).aggregate(total=Sum('value'))['total']
            or Decimal('0')
        )
        should_notify = total >= budget.limit_value and (budget.last_notified_total or Decimal('0')) < budget.limit_value
        budget.last_notified_total = total
        if should_notify and budget.notify_owner and profile and profile.notify_budget_alerts:
            title = 'Or\u00e7amento atingido'
            body = (
                f'A categoria {budget.category.name} ultrapassou o limite configurado.\n\n'
                f'Per\u00edodo: {budget.get_period_display()}\n'
                f'Total: R$ {total:.2f}\n'
                f'Limite: R$ {budget.limit_value:.2f}\n'
            )
            _send_email(owner.email, title, body, reverse('tracker:dashboard'))
            _create_notification(owner, title, body, level='warning', workspace=workspace, action_url=reverse('tracker:dashboard'))
            _send_push(owner, title, body, reverse('tracker:dashboard'))
            budget.last_notified_at = timezone.now()
            alerts.append((budget, total))
        budget.save(update_fields=['last_notified_total', 'last_notified_at'])
    return alerts


def check_balance_goals(workspace, reference_date=None):
    if not workspace:
        return []
    goals = BalanceGoal.objects.filter(workspace=workspace)
    if not goals.exists():
        return []
    owner = workspace.owner
    profile = _get_profile(owner)
    alerts = []
    for goal in goals:
        start, end = _period_range(goal.period, reference_date)
        balance = _workspace_balance(workspace, start, end)
        meets = balance <= goal.target_value if goal.direction == 'min' else balance >= goal.target_value
        last_value = goal.last_notified_value
        crossed = False
        if meets and last_value is None:
            crossed = True
        elif meets and goal.direction == 'min' and last_value > goal.target_value:
            crossed = True
        elif meets and goal.direction == 'max' and last_value < goal.target_value:
            crossed = True
        goal.last_notified_value = balance
        if crossed and goal.notify_owner and profile and profile.notify_goal_alerts:
            title = 'Meta de saldo atingida'
            body = (
                f'Uma meta de saldo foi atingida no workspace {workspace.name}.\n\n'
                f'Per\u00edodo: {goal.get_period_display()}\n'
                f'Dire\u00e7\u00e3o: {goal.get_direction_display()}\n'
                f'Saldo atual: R$ {balance:.2f}\n'
                f'Meta: R$ {goal.target_value:.2f}\n'
            )
            _send_email(owner.email, title, body, reverse('tracker:dashboard'))
            _create_notification(owner, title, body, level='success', workspace=workspace, action_url=reverse('tracker:dashboard'))
            _send_push(owner, title, body, reverse('tracker:dashboard'))
            goal.last_notified_at = timezone.now()
            alerts.append((goal, balance))
        goal.save(update_fields=['last_notified_value', 'last_notified_at'])
    return alerts


def notify_task_completed(task, actor=None) -> None:
    workspace = getattr(task, 'workspace', None)
    if not workspace:
        return
    owner = workspace.owner
    profile = _get_profile(owner)
    if not profile or profile.is_guest:
        return
    notify_owner = profile.notify_task_completed and not (actor and actor.id == owner.id)
    subject = f'Tarefa conclu\u00edda: {task.title}'
    body = (
        f'Uma tarefa foi conclu\u00edda no workspace {workspace.name}.\n\n'
        f'Tarefa: {task.title}\n'
        f'Conclu\u00edda por: {actor.get_full_name() if actor else "-"} ({actor.username if actor else "-"})\n'
    )
    if notify_owner:
        _send_email(owner.email, subject, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
        _create_notification(owner, subject, body, level='success', workspace=workspace, action_url=reverse('tracker:task_update', args=[task.id]) + '?detail=1')
        _send_push(owner, subject, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
    if task.responsible_email:
        _send_email(task.responsible_email, subject, body)


def notify_step_completed(task, step, actor=None) -> None:
    workspace = getattr(task, 'workspace', None)
    if not workspace:
        return
    owner = workspace.owner
    profile = _get_profile(owner)
    if not profile or profile.is_guest:
        return
    notify_owner = profile.notify_step_completed and not (actor and actor.id == owner.id)
    subject = f'Etapa conclu\u00edda: {step.title}'
    body = (
        f'Uma etapa foi conclu\u00edda no workspace {workspace.name}.\n\n'
        f'Tarefa: {task.title}\n'
        f'Etapa: {step.title}\n'
        f'Conclu\u00edda por: {actor.get_full_name() if actor else "-"} ({actor.username if actor else "-"})\n'
    )
    if notify_owner:
        _send_email(owner.email, subject, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
        _create_notification(owner, subject, body, level='success', workspace=workspace, action_url=reverse('tracker:task_update', args=[task.id]) + '?detail=1')
        _send_push(owner, subject, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
    if step.responsible_email:
        _send_email(step.responsible_email, subject, body)


def notify_task_due(task, status_label: str, days_left: int | None = None):
    workspace = getattr(task, 'workspace', None)
    if not workspace:
        return
    owner = workspace.owner
    profile = _get_profile(owner)
    if not profile or profile.is_guest:
        return
    if status_label == 'overdue' and not profile.notify_task_overdue:
        return
    if status_label == 'soon' and not profile.notify_task_reminder:
        return
    title = 'Tarefa atrasada' if status_label == 'overdue' else 'Tarefa com prazo pr\u00f3ximo'
    if days_left is not None and days_left >= 0:
        title = f'Tarefa vence em {days_left} dia(s)'
    body = (
        f'Tarefa: {task.title}\n'
        f'Prazo: {task.due_date.strftime("%d/%m/%Y")}\n'
        f'Workspace: {workspace.name}\n'
    )
    _send_email(owner.email, title, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
    _create_notification(owner, title, body, level='warning', workspace=workspace, action_url=reverse('tracker:task_update', args=[task.id]) + '?detail=1')
    _send_push(owner, title, body, reverse('tracker:task_update', args=[task.id]) + '?detail=1')
    if task.responsible_email:
        _send_email(task.responsible_email, title, body)


def notify_account_deletion_request(user) -> None:
    subject = 'Solicita\u00e7\u00e3o de exclus\u00e3o de conta'
    body = (
        'Um usu\u00e1rio solicitou a exclus\u00e3o da conta.\n\n'
        f'Usu\u00e1rio: {user.username}\n'
        f'Nome: {user.get_full_name() or "-"}\n'
        f'E-mail: {user.email or "-"}\n'
    )
    _notify_superusers('notify_admin_contact', subject, body, level='warning', action_url=reverse('tracker:user_admin_list'))
