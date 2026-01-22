import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from tracker.models import UserProfile, Workspace
from tracker.pricing import get_pricing_state, plan_label, plan_price
from .models import MpSubscription, MpWebhookEvent
from .services import build_preapproval_payload, create_preapproval, extract_preapproval_fields, fetch_preapproval

User = get_user_model()


def _parse_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _build_pricing_plans(pricing_state):
    plan_features = {
        "essential": [
            "Acessos manuais e ditados",
            "IA básica",
            "Até 2 convidados por workspace",
        ],
        "pro": [
            "Tudo do Essencial",
            "Maior limite de tokens da IA",
            "Até 6 convidados por workspace",
            "ChatAgent no WhatsApp",
        ],
        "master": [
            "Tudo do Pro",
            "Limites máximos de IA",
            "Convidados personalizados",
            "+15% por convidado acima de 6",
        ],
    }
    plans = []
    for tier in ("essential", "pro", "master"):
        for cycle in ("monthly", "annual"):
            price = plan_price(pricing_state, tier, cycle, guest_limit=6)
            regular_price = plan_price(pricing_state, tier, cycle, guest_limit=6, promo=False)
            annual_total = price * Decimal("12") if cycle == "annual" else None
            cycle_label = "Mensal" if cycle == "monthly" else "Anual"
            plans.append({
                "name": plan_label(tier),
                "tier_label": plan_label(tier),
                "cycle_label": cycle_label,
                "is_annual": cycle == "annual",
                "cycle": f"{cycle}:{tier}",
                "tier": tier,
                "price": price,
                "regular_price": regular_price,
                "subtitle": "Valor base do plano" if tier != "master" else "Base com 6 convidados",
                "badge": "Mais escolhido" if tier == "pro" else "Novo" if tier == "master" else "Essencial",
                "annual_total": annual_total,
                "features": plan_features[tier],
            })
    return plans


def _apply_subscription_to_profile(user, sub: MpSubscription, plan_cycle: str, plan_tier: str, status: str, next_payment_at, guest_limit: int = 0):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    today = timezone.localdate()
    grace_days = int(getattr(settings, 'SUBSCRIPTION_GRACE_DAYS', 7))

    sub.plan_cycle = plan_cycle
    sub.plan_tier = plan_tier
    if guest_limit:
        sub.guest_limit = guest_limit
    sub.status = status or sub.status
    if next_payment_at:
        sub.next_payment_at = next_payment_at
        profile.subscription_expires = next_payment_at.date()
    else:
        profile.subscription_expires = today + timedelta(days=365 if plan_cycle == 'annual' else 30)
    sub.grace_until = profile.subscription_expires + timedelta(days=grace_days)
    profile.billing_cycle = plan_cycle
    profile.plan = plan_tier
    if plan_tier == 'master' and guest_limit:
        profile.master_guest_limit = max(guest_limit, 6)
    profile.payment_confirmed = status in ('authorized', 'active')
    if profile.payment_confirmed:
        profile.payment_confirmed_at = timezone.now()
        profile.is_approved = True
        if not profile.approved_at:
            profile.approved_at = timezone.now()
            profile.approved_by = None
    profile.save(update_fields=[
        'billing_cycle',
        'plan',
        'master_guest_limit',
        'payment_confirmed',
        'payment_confirmed_at',
        'subscription_expires',
        'is_approved',
        'approved_at',
        'approved_by',
    ])
    sub.save(update_fields=['plan_cycle', 'plan_tier', 'guest_limit', 'status', 'next_payment_at', 'grace_until', 'updated_at'])


def subscription_start(request):
    profile = getattr(request.user, 'profile', None)
    if not profile:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if profile and profile.is_guest and not request.user.is_superuser:
        messages.error(request, 'Conta convidada não pode assinar.')
        return redirect('tracker:profile_edit')

    plan_param = (request.GET.get('plan') or '').strip()
    force_choose = request.GET.get('choose') == '1'
    pricing_state = get_pricing_state()
    fallback_cycle = getattr(profile, 'billing_cycle', None) if profile else None
    fallback_tier = getattr(profile, 'plan', None) if profile else None
    plan_cycle = None
    plan_tier = None
    if plan_param:
        if ':' in plan_param:
            plan_cycle, plan_tier = plan_param.split(':', 1)
        else:
            plan_cycle = plan_param
    if not plan_cycle and fallback_cycle in ('monthly', 'annual') and not force_choose:
        plan_cycle = fallback_cycle
    if not plan_tier and fallback_tier in ('essential', 'pro', 'master') and not force_choose:
        plan_tier = fallback_tier
    if not plan_cycle or not plan_tier:
        pricing_plans = _build_pricing_plans(pricing_state)
        return render(
            request,
            'payments/choose_plan.html',
            {
                'pricing_plans': pricing_plans,
                'pricing_state': pricing_state,
                'current_cycle': getattr(profile, 'billing_cycle', 'monthly') if profile else 'monthly',
            },
        )

    if plan_cycle not in ('monthly', 'annual'):
        plan_cycle = 'monthly'
    if plan_tier not in ('essential', 'pro', 'master'):
        plan_tier = 'essential'

    guest_limit = 0
    if plan_tier == 'master':
        guest_limit = int(getattr(profile, 'master_guest_limit', 6) or 6)

    unit_price = plan_price(pricing_state, plan_tier, plan_cycle, guest_limit=guest_limit)
    amount = unit_price * (12 if plan_cycle == 'annual' else 1)

    reason = f"iTracker {plan_label(plan_tier)} {('Anual' if plan_cycle == 'annual' else 'Mensal')}"
    if plan_tier == 'master' and guest_limit:
        reason += f" ({guest_limit} convidados)"
    back_url = request.build_absolute_uri('/pagamentos/sucesso/')
    payer_email = getattr(settings, 'MP_PAYER_EMAIL', '') or request.user.email or ''
    if not payer_email:
        messages.error(request, 'Cadastre um e-mail válido para iniciar o pagamento.')
        return redirect('tracker:profile_edit')
    payload = build_preapproval_payload(
        reason=reason,
        external_reference=str(request.user.id),
        back_url=back_url,
        plan_cycle=plan_cycle,
        payer_email=payer_email,
        amount=float(amount),
    )
    if getattr(settings, "MP_SIMULATE_PAYMENTS", False):
        sub, _ = MpSubscription.objects.get_or_create(user=request.user)
        sub.preapproval_id = f"simulated-{request.user.id}"
        sub.status = "active"
        sub.payer_email = payer_email
        sub.reason = reason
        sub.auto_recurring = payload.get("auto_recurring", {})
        sub.last_payment_status = "approved"
        sub.plan_cycle = plan_cycle
        sub.plan_tier = plan_tier
        sub.guest_limit = guest_limit
        sub.init_point = ""
        sub.save()
        _apply_subscription_to_profile(request.user, sub, plan_cycle, plan_tier, "active", timezone.now() + timedelta(days=30), guest_limit)
        messages.success(request, "Assinatura simulada aprovada.")
        return redirect("tracker:dashboard")
    try:
        data = create_preapproval(payload)
    except Exception as exc:
        messages.error(request, f'Falha ao iniciar pagamento: {exc}')
        return redirect(f"{reverse('payments:subscription_start')}?choose=1")

    profile = getattr(request.user, 'profile', None)
    sub, _ = MpSubscription.objects.get_or_create(user=request.user)
    fields = extract_preapproval_fields(data)
    sub.preapproval_id = fields['preapproval_id']
    sub.status = fields['status'] or 'pending'
    sub.payer_email = fields['payer_email'] or request.user.email
    sub.reason = fields['reason'] or reason
    sub.auto_recurring = fields['auto_recurring'] or payload.get('auto_recurring', {})
    sub.last_payment_status = fields['last_payment_status']
    sub.plan_cycle = plan_cycle
    sub.plan_tier = plan_tier
    sub.guest_limit = guest_limit
    sub.init_point = data.get('init_point') or data.get('sandbox_init_point', '')
    sub.save()
    if sub.init_point:
        return redirect(sub.init_point)
    messages.error(request, 'Não foi possível gerar o link de pagamento.')
    return redirect('tracker:profile_edit')


def subscription_success(request):
    preapproval_id = request.GET.get('preapproval_id') or request.GET.get('preapproval')
    if not preapproval_id:
        sub = (
            MpSubscription.objects.filter(user=request.user)
            .exclude(preapproval_id='')
            .order_by('-updated_at', '-id')
            .first()
        )
        if sub and sub.preapproval_id:
            preapproval_id = sub.preapproval_id
            messages.info(request, 'Pagamento em processamento. Verificando sua assinatura...')
        else:
            messages.info(request, 'Pagamento em processamento.')
            return render(request, 'payments/pending.html')
    try:
        data = fetch_preapproval(preapproval_id)
    except Exception as exc:
        messages.error(request, f'Falha ao confirmar pagamento: {exc}')
        return render(request, 'payments/success.html')

    profile = getattr(request.user, 'profile', None)
    sub, _ = MpSubscription.objects.get_or_create(user=request.user)
    fields = extract_preapproval_fields(data)
    sub.preapproval_id = fields['preapproval_id']
    sub.payer_email = fields['payer_email']
    sub.reason = fields['reason']
    sub.auto_recurring = fields['auto_recurring']
    sub.last_payment_status = fields['last_payment_status']
    sub.last_event_at = timezone.now()
    next_payment_at = _parse_datetime(fields['next_payment_at'])
    plan_cycle = sub.plan_cycle or getattr(profile, 'billing_cycle', 'monthly')
    plan_tier = sub.plan_tier or getattr(profile, 'plan', 'essential')
    _apply_subscription_to_profile(
        request.user,
        sub,
        plan_cycle,
        plan_tier,
        fields['status'],
        next_payment_at,
        sub.guest_limit,
    )
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
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed.'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8')) if request.body else {}
    except json.JSONDecodeError:
        payload = {}

    token_expected = (getattr(settings, 'MP_WEBHOOK_TOKEN', '') or '').strip()
    if token_expected:
        token_received = (
            request.GET.get('token')
            or request.headers.get('X-MP-Token')
            or payload.get('token')
            or ''
        )
        if token_received != token_expected:
            return JsonResponse({'detail': 'Unauthorized.'}, status=403)

    topic = request.GET.get('topic') or payload.get('type') or payload.get('topic', '')
    mp_id = request.GET.get('id') or (payload.get('data') or {}).get('id') or payload.get('id') or ''
    mp_id_str = str(mp_id or '')

    if mp_id_str and MpWebhookEvent.objects.filter(topic=topic or '', mp_id=mp_id_str, status='processed').exists():
        return JsonResponse({'ok': True, 'detail': 'duplicate'}, status=200)

    event = MpWebhookEvent.objects.create(topic=topic or '', mp_id=mp_id_str, payload=payload)
    if not mp_id:
        event.status = 'ignored'
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'processed_at'])
        return JsonResponse({'ok': True})

    try:
        data = fetch_preapproval(str(mp_id))
        collector_expected = (getattr(settings, 'MP_COLLECTOR_ID', '') or '').strip()
        app_expected = (getattr(settings, 'MP_APP_ID', '') or '').strip()
        if collector_expected and str(data.get('collector_id', '')) != collector_expected:
            event.status = 'ignored'
            event.processed_at = timezone.now()
            event.save(update_fields=['status', 'processed_at'])
            return JsonResponse({'ok': True})
        if app_expected and str(data.get('application_id', '')) != app_expected:
            event.status = 'ignored'
            event.processed_at = timezone.now()
            event.save(update_fields=['status', 'processed_at'])
            return JsonResponse({'ok': True})
        fields = extract_preapproval_fields(data)
        preapproval_id = fields['preapproval_id']
        sub = MpSubscription.objects.filter(preapproval_id=preapproval_id).first()
        if not sub:
            ext_ref = (fields.get('external_reference') or '').strip()
            if ext_ref:
                user = User.objects.filter(id=ext_ref).first()
                if user:
                    sub, _ = MpSubscription.objects.get_or_create(user=user)
                    sub.preapproval_id = preapproval_id
                    sub.save(update_fields=['preapproval_id', 'updated_at'])
        if not sub:
            event.status = 'ignored'
            event.processed_at = timezone.now()
            event.save(update_fields=['status', 'processed_at'])
            return JsonResponse({'ok': True})
        if sub:
            sub.payer_email = fields['payer_email']
            sub.reason = fields['reason']
            sub.auto_recurring = fields['auto_recurring']
            sub.last_payment_status = fields['last_payment_status']
            sub.last_event_at = timezone.now()
            next_payment_at = _parse_datetime(fields['next_payment_at'])
            plan_cycle = sub.plan_cycle
            auto_recurring = fields.get('auto_recurring') or {}
            if auto_recurring.get('frequency_type') == 'months':
                try:
                    freq = int(auto_recurring.get('frequency') or 0)
                except (TypeError, ValueError):
                    freq = 0
                if freq >= 12:
                    plan_cycle = 'annual'
                elif freq >= 1:
                    plan_cycle = 'monthly'
            _apply_subscription_to_profile(sub.user, sub, plan_cycle, sub.plan_tier or getattr(getattr(sub.user, 'profile', None), 'plan', 'essential'), fields['status'], next_payment_at, sub.guest_limit)
        event.status = 'processed'
    except Exception as exc:
        event.status = 'error'
        if isinstance(event.payload, dict):
            event.payload = {**event.payload, 'error': str(exc)}
    event.processed_at = timezone.now()
    update_fields = ['status', 'processed_at']
    if isinstance(event.payload, dict):
        update_fields.append('payload')
    event.save(update_fields=update_fields)
    return JsonResponse({'ok': True})

# Create your views here.
