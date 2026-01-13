import json
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from tracker.models import UserProfile, Workspace
from tracker.pricing import get_pricing_state
from .models import MpSubscription, MpWebhookEvent
from .services import build_preapproval_payload, create_preapproval, extract_preapproval_fields, fetch_preapproval


def _parse_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _apply_subscription_to_profile(user, sub: MpSubscription, plan_cycle: str, status: str, next_payment_at):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    today = timezone.localdate()
    grace_days = int(getattr(settings, 'SUBSCRIPTION_GRACE_DAYS', 7))

    sub.plan_cycle = plan_cycle
    sub.status = status or sub.status
    if next_payment_at:
        sub.next_payment_at = next_payment_at
        profile.subscription_expires = next_payment_at.date()
    else:
        profile.subscription_expires = today + timedelta(days=365 if plan_cycle == 'annual' else 30)
    sub.grace_until = profile.subscription_expires + timedelta(days=grace_days)
    profile.billing_cycle = plan_cycle
    profile.payment_confirmed = status in ('authorized', 'active')
    if profile.payment_confirmed:
        profile.payment_confirmed_at = timezone.now()
        profile.is_approved = True
        if not profile.approved_at:
            profile.approved_at = timezone.now()
            profile.approved_by = None
    profile.save(update_fields=[
        'subscription_expires',
        'billing_cycle',
        'payment_confirmed',
        'payment_confirmed_at',
        'is_approved',
        'approved_at',
        'approved_by',
    ])
    if profile.payment_confirmed:
        Workspace.objects.filter(owner=user).update(is_active=True)
    sub.save(update_fields=['plan_cycle', 'status', 'next_payment_at', 'grace_until', 'updated_at'])


@login_required
def subscription_start(request):
    profile = getattr(request.user, 'profile', None)
    if profile and profile.is_guest and not request.user.is_superuser:
        messages.error(request, 'Conta convidada n\u00e3o pode assinar.')
        return redirect('tracker:profile_edit')

    plan_cycle = request.GET.get('plan') or getattr(profile, 'billing_cycle', 'monthly') if profile else 'monthly'
    if plan_cycle not in ('monthly', 'annual'):
        plan_cycle = 'monthly'

    pricing_state = get_pricing_state()
    amount = pricing_state.annual_price if plan_cycle == 'annual' else pricing_state.monthly_price

    reason = f'iTracker {plan_cycle}'
    back_urls = {
        'success': request.build_absolute_uri('/pagamentos/sucesso/'),
        'failure': request.build_absolute_uri('/pagamentos/falha/'),
        'pending': request.build_absolute_uri('/pagamentos/pendente/'),
    }
    payload = build_preapproval_payload(
        reason=reason,
        external_reference=str(request.user.id),
        back_urls=back_urls,
        plan_cycle=plan_cycle,
        payer_email=request.user.email,
        amount=float(amount),
    )
    try:
        data = create_preapproval(payload)
    except Exception as exc:
        messages.error(request, f'Falha ao iniciar pagamento: {exc}')
        return redirect('tracker:profile_edit')

    sub, _ = MpSubscription.objects.get_or_create(user=request.user)
    fields = extract_preapproval_fields(data)
    sub.preapproval_id = fields['preapproval_id']
    sub.status = fields['status'] or 'pending'
    sub.payer_email = fields['payer_email'] or request.user.email
    sub.reason = fields['reason'] or reason
    sub.auto_recurring = fields['auto_recurring'] or payload.get('auto_recurring', {})
    sub.last_payment_status = fields['last_payment_status']
    sub.plan_cycle = plan_cycle
    sub.init_point = data.get('init_point') or data.get('sandbox_init_point', '')
    sub.save()
    if sub.init_point:
        return redirect(sub.init_point)
    messages.error(request, 'N\u00e3o foi poss\u00edvel gerar o link de pagamento.')
    return redirect('tracker:profile_edit')


@login_required
def subscription_success(request):
    preapproval_id = request.GET.get('preapproval_id') or request.GET.get('preapproval')
    if not preapproval_id:
        messages.info(request, 'Pagamento em processamento.')
        return render(request, 'payments/success.html')
    try:
        data = fetch_preapproval(preapproval_id)
    except Exception as exc:
        messages.error(request, f'Falha ao confirmar pagamento: {exc}')
        return render(request, 'payments/success.html')

    sub, _ = MpSubscription.objects.get_or_create(user=request.user)
    fields = extract_preapproval_fields(data)
    sub.preapproval_id = fields['preapproval_id']
    sub.payer_email = fields['payer_email']
    sub.reason = fields['reason']
    sub.auto_recurring = fields['auto_recurring']
    sub.last_payment_status = fields['last_payment_status']
    sub.last_event_at = timezone.now()
    next_payment_at = _parse_datetime(fields['next_payment_at'])
    _apply_subscription_to_profile(request.user, sub, sub.plan_cycle, fields['status'], next_payment_at)
    messages.success(request, 'Assinatura confirmada. Bem-vindo(a) ao plano!')
    return render(request, 'payments/success.html')


@login_required
def subscription_failure(request):
    messages.error(request, 'Pagamento n\u00e3o conclu\u00eddo. Tente novamente.')
    return render(request, 'payments/failure.html')


@login_required
def subscription_pending(request):
    messages.info(request, 'Pagamento pendente. Voc\u00ea receber\u00e1 a confirma\u00e7\u00e3o em breve.')
    return render(request, 'payments/pending.html')


@csrf_exempt
def mp_webhook(request):
    try:
        payload = json.loads(request.body.decode('utf-8')) if request.body else {}
    except json.JSONDecodeError:
        payload = {}

    topic = request.GET.get('topic') or payload.get('type') or payload.get('topic', '')
    mp_id = request.GET.get('id') or (payload.get('data') or {}).get('id') or payload.get('id') or ''

    event = MpWebhookEvent.objects.create(topic=topic or '', mp_id=str(mp_id), payload=payload)
    if not mp_id:
        event.status = 'ignored'
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'processed_at'])
        return JsonResponse({'ok': True})

    try:
        data = fetch_preapproval(str(mp_id))
        fields = extract_preapproval_fields(data)
        preapproval_id = fields['preapproval_id']
        sub = MpSubscription.objects.filter(preapproval_id=preapproval_id).first()
        if sub:
            sub.payer_email = fields['payer_email']
            sub.reason = fields['reason']
            sub.auto_recurring = fields['auto_recurring']
            sub.last_payment_status = fields['last_payment_status']
            sub.last_event_at = timezone.now()
            next_payment_at = _parse_datetime(fields['next_payment_at'])
            _apply_subscription_to_profile(sub.user, sub, sub.plan_cycle, fields['status'], next_payment_at)
        event.status = 'processed'
    except Exception as exc:
        event.status = f'error:{exc}'
    event.processed_at = timezone.now()
    event.save(update_fields=['status', 'processed_at'])
    return JsonResponse({'ok': True})

# Create your views here.
