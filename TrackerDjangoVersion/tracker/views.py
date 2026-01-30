import csv
import datetime
import calendar
import io
import json
import os
import re
import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.views import PasswordResetView
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import (
    Case,
    Count,
    CharField,
    DateTimeField,
    DecimalField,
    ExpressionWrapper,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils import timezone
from django.templatetags.static import static
from django.utils.text import slugify
from django.views.decorators.cache import cache_control

from .forms import (
    CategoryForm,
    TaskCategoryForm,
    LoginForm,
    SignupForm,
    GuestSignupForm,
    TaskForm,
    TaskStepForm,
    TransactionForm,
    WorkspaceForm,
    WorkspaceMemberInviteForm,
    WorkspaceSlugForm,
    UserAdminForm,
    UserProfileAdminForm,
    ProfileForm,
    ProfileAvatarForm,
    MasterGuestLimitRequestForm,
    NotificationPreferencesForm,
    ContactAdminForm,
    InviteByUsernameForm,
    AccessRequestForm,
    StatementUploadForm,
    CategoryBudgetForm,
    BalanceGoalForm,
    SubscriptionInviteForm,
    PasswordResetRequestForm,
    PricingConfigForm,
    TransactionBulkUpdateForm,
    TaskBulkUpdateForm,
    AdminBroadcastForm,
)
from .models import (
    Category,
    TaskCategory,
    Task,
    TaskStep,
    Transaction,
    Workspace,
    WorkspaceMembership,
    WorkspaceAccessRequest,
    UserProfile,
    WorkspaceInvite,
    Notification,
    CategoryBudget,
    BalanceGoal,
    PushSubscription,
    SubscriptionInvite,
    SubscriptionInviteUse,
    MetricEvent,
    PricingConfig,
)
from .notifications import (
    notify_balance_threshold,
    notify_contact_message,
    notify_new_account,
    notify_account_deletion_request,
    notify_step_completed,
    notify_task_completed,
    check_category_budgets,
    check_balance_goals,
    broadcast_message,
)
from .metrics import record_metric
from .pricing import get_pricing_state, plan_label, plan_price
from assistant.services import llm_complete, estimate_costs
from assistant.models import AiUsage, ChatMessage
from assistant.limits import get_ai_quota
from payments.models import MpSubscription, MpWebhookEvent
from payments.services import update_preapproval
from whatsapp_finance.forms import WhatsAppProfileForm
from whatsapp_finance.models import WhatsAppProfile

User = get_user_model()


# -------- Helpers --------

def _user_can_view_finance(request, workspace) -> bool:
    """Only superuser or owners can ver/editar financas."""
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    if getattr(request, "subscription_grace", False):
        return False
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        return False
    if workspace.owner_id == request.user.id:
        return True
    role = getattr(request, "workspace_role", None)
    return role == 'owner'


def _user_can_create_transaction(request, workspace) -> bool:
    """Guests podem lançar transações, mas não ver/editar o painel financeiro."""
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    if getattr(request, "subscription_grace", False):
        return False
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        return True
    return _user_can_view_finance(request, workspace)


def _finance_access_denied_redirect(request):
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        return redirect('tracker:tasks_list')
    if getattr(request, "subscription_grace", False):
        return redirect('tracker:tasks_list')
    return redirect('tracker:dashboard')

def _is_paid_user(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    if not profile:
        return False
    if profile.is_guest:
        return False
    if profile.trial_active():
        return True
    return bool(profile.payment_confirmed)


def _workspace_unpaid_count(workspace) -> int:
    now = timezone.now()
    return WorkspaceMembership.objects.filter(workspace=workspace).exclude(
        role='owner'
    ).exclude(
        user__is_superuser=True
    ).filter(
        Q(user__profile__isnull=True)
        | Q(user__profile__is_guest=True)
        | (
            Q(user__profile__payment_confirmed=False)
            & (Q(user__profile__trial_expires_at__isnull=True) | Q(user__profile__trial_expires_at__lt=now))
        )
    ).count()


def _workspace_unpaid_invite_count(workspace) -> int:
    now = timezone.now()
    return WorkspaceInvite.objects.filter(workspace=workspace, status='pending').exclude(
        invited_user__is_superuser=True
    ).filter(
        Q(invited_user__profile__isnull=True)
        | Q(invited_user__profile__is_guest=True)
        | (
            Q(invited_user__profile__payment_confirmed=False)
            & (
                Q(invited_user__profile__trial_expires_at__isnull=True)
                | Q(invited_user__profile__trial_expires_at__lt=now)
            )
        )
    ).count()


def _workspace_guest_limit(workspace) -> int:
    if not workspace:
        return 0
    owner_profile = getattr(workspace.owner, 'profile', None)
    if owner_profile and owner_profile.plan == 'pro':
        return 6
    if owner_profile and owner_profile.plan == 'master':
        return max(owner_profile.master_guest_limit or 6, 6)
    return 0


def _apply_workspace_filter(queryset, workspace, user):
    if workspace:
        return queryset.filter(workspace=workspace)
    if not user.is_superuser:
        return queryset.none()
    return queryset


def _tasks_queryset(request, workspace):
    qs = Task.objects.all()
    if workspace:
        return qs.filter(workspace=workspace)
    if request.user.is_superuser:
        return qs
    return qs.none()


def _cache_delete_pattern(pattern: str) -> bool:
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        delete_pattern(pattern)
        return True
    return False


def _invalidate_workspace_caches(workspace):
    if not workspace:
        cache.clear()
        return
    user_ids = set(
        WorkspaceMembership.objects.filter(workspace=workspace).values_list('user_id', flat=True)
    )
    if workspace.owner_id:
        user_ids.add(workspace.owner_id)
    if not user_ids:
        return
    if not _cache_delete_pattern("dash:*"):
        cache.clear()
        return
    ws_key = workspace.id
    for uid in user_ids:
        _cache_delete_pattern(f"dash:{uid}:{ws_key}:*")
        _cache_delete_pattern(f"chart:{uid}:{ws_key}:*")


def _build_filter_chips(request, items):
    chips = []
    for key, label, value in items:
        if value in (None, '', []):
            continue
        query = request.GET.copy()
        query.pop(key, None)
        query.pop('page', None)
        url = f"?{query.urlencode()}" if query else ""
        chips.append({'label': f"{label}: {value}", 'url': url})
    return chips


def _ensure_unique_slug(base: str) -> str:
    slug = slugify(base) or "workspace"
    base_slug = slug
    counter = 1
    while Workspace.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


def _get_workspace_members(workspace):
    if not workspace:
        return []
    people_map = {}

    def add_user(user):
        if not user:
            return
        if user.id in people_map:
            return
        name = user.get_full_name() or user.username
        people_map[user.id] = {
            'name': name,
            'email': user.email or '',
        }

    add_user(getattr(workspace, 'owner', None))
    members = WorkspaceMembership.objects.filter(workspace=workspace).select_related('user')
    for membership in members:
        add_user(membership.user)

    return sorted(people_map.values(), key=lambda item: item['name'].lower())


def _record_ai_usage(user, workspace, feature, usage, model_name):
    if not user or not usage:
        return
    total = int(usage.get('total_tokens') or 0)
    prompt_tokens = int(usage.get('prompt_tokens') or 0)
    completion_tokens = int(usage.get('completion_tokens') or 0)
    if total <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return
    cost_usd, cost_brl = estimate_costs(usage)
    AiUsage.objects.create(
        user=user,
        workspace=workspace,
        feature=feature,
        model_name=model_name or '',
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cost_usd=cost_usd,
        cost_brl=cost_brl,
    )

def _format_int(value: int | float | None) -> str:
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _login_rate_limit_key(request) -> str:
    ip = request.META.get('REMOTE_ADDR', 'unknown')
    username = (request.POST.get('username') or '').strip().lower()
    return f'login_attempts:{ip}:{username}'


def _is_login_blocked(request) -> bool:
    max_attempts = int(getattr(settings, 'LOGIN_RATE_LIMIT_ATTEMPTS', 5))
    window_seconds = int(getattr(settings, 'LOGIN_RATE_LIMIT_WINDOW', 900))
    key = _login_rate_limit_key(request)
    payload = cache.get(key)
    if not payload:
        return False
    count = payload.get('count', 0)
    first_at = payload.get('first_at')
    if not first_at:
        return False
    elapsed = (timezone.now() - first_at).total_seconds()
    if elapsed > window_seconds:
        cache.delete(key)
        return False
    return count >= max_attempts


def _register_login_failure(request) -> None:
    max_attempts = int(getattr(settings, 'LOGIN_RATE_LIMIT_ATTEMPTS', 5))
    window_seconds = int(getattr(settings, 'LOGIN_RATE_LIMIT_WINDOW', 900))
    key = _login_rate_limit_key(request)
    payload = cache.get(key)
    if payload:
        payload['count'] = payload.get('count', 0) + 1
    else:
        payload = {'count': 1, 'first_at': timezone.now()}
    cache.set(key, payload, window_seconds)
    if payload['count'] >= max_attempts:
        record_metric('login_blocked', metadata={'ip': request.META.get('REMOTE_ADDR')})


def _clear_login_failures(request) -> None:
    cache.delete(_login_rate_limit_key(request))


def _generate_invite_code() -> str:
    return secrets.token_urlsafe(8).replace('-', '').replace('_', '').upper()


def _pretty_screenshot_label(filename: str) -> str:
    base = os.path.splitext(filename)[0]
    base = re.sub(r"^\d{1,3}[ _-]+", "", base)
    base = base.replace("_", " ").replace("-", " ")
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)
    base = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", base)
    base = re.sub(r"\s+", " ", base).strip()

    mode = None
    if re.search(r"\bdark mode\b", base, re.IGNORECASE):
        mode = "Modo escuro"
        base = re.sub(r"(?i)\bdark mode\b", "", base)
    if re.search(r"\blight mode\b", base, re.IGNORECASE):
        mode = "Modo claro"
        base = re.sub(r"(?i)\blight mode\b", "", base)

    base = re.sub(r"\s+", " ", base).strip()
    base = base.title()

    replacements = {
        "Ai Agent": "Agente de IA",
        "Help Page": "P\u00e1gina de ajuda",
        "Transactions": "Transa\u00e7\u00f5es",
        "Dashboard": "Dashboard",
        "Tarefas": "Tarefas",
        "Embeded Button": "Bot\u00e3o embutido",
        "Embedded Button": "Bot\u00e3o embutido",
    }
    for key, value in replacements.items():
        base = re.sub(r"\b" + re.escape(key) + r"\b", value, base)
    base = base.replace("Agente de IA Bot\u00e3o embutido", "Agente de IA - Bot\u00e3o embutido")
    base = re.sub(r"\s+", " ", base).strip()

    if mode:
        if base:
            return f"{base} - {mode}"
        return mode
    return base or "Screenshot"


def _send_email_verification(request, user) -> None:
    if not user.email:
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    verify_url = request.build_absolute_uri(
        reverse('tracker:verify_email', args=[uid, token])
    )
    subject = 'Confirme seu e-mail no iTracker'
    body = (
        'Para ativar sua conta, confirme seu e-mail no link abaixo:\n\n'
        f'{verify_url}\n\n'
        'Se você não solicitou este cadastro, ignore esta mensagem.'
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)


class TrackerPasswordResetView(PasswordResetView):
    form_class = PasswordResetRequestForm

    def form_valid(self, form):
        record_metric('password_reset', metadata={'email': form.cleaned_data.get('email')})
        return super().form_valid(form)


# -------- Landing --------

def home(request):
    if request.user.is_authenticated:
        return redirect('tracker:dashboard')

    pricing_state = get_pricing_state()
    annual_total = pricing_state.annual_price * Decimal("12")
    workspace_count = Workspace.objects.filter(is_active=True).count()
    transaction_count = Transaction.objects.count()
    task_count = Task.objects.count() + TaskStep.objects.count()
    alert_count = CategoryBudget.objects.count() + BalanceGoal.objects.count()
    landing_stats = [
        {"label": "Workspaces ativos", "value": workspace_count, "suffix": ""},
        {"label": "Transa\u00e7\u00f5es registradas", "value": transaction_count, "suffix": ""},
        {"label": "Tarefas e etapas", "value": task_count, "suffix": ""},
        {"label": "Alertas inteligentes", "value": alert_count, "suffix": "ativos"},
    ]
    landing_screenshots = []
    media_root = getattr(settings, "MEDIA_ROOT", "")
    if media_root:
        screenshot_dir = os.path.join(media_root, "systemPrints")
        if os.path.isdir(screenshot_dir):
            all_shots = []
            prefixed_shots = []
            for filename in sorted(os.listdir(screenshot_dir)):
                ext = os.path.splitext(filename)[1].lower()
                if ext in {".png", ".jpg", ".jpeg", ".webp"}:
                    label = _pretty_screenshot_label(filename)
                    shot = {"url": f"{settings.MEDIA_URL}systemPrints/{filename}", "label": label}
                    all_shots.append(shot)
                    prefix_match = re.match(r"^(\d{1,3})[ _-]", filename)
                    if prefix_match:
                        prefixed_shots.append((int(prefix_match.group(1)), shot))
            if prefixed_shots:
                landing_screenshots = [shot for _, shot in sorted(prefixed_shots, key=lambda item: item[0])]
            else:
                landing_screenshots = all_shots
    if not landing_screenshots:
        landing_screenshots = [
            {"url": static("help/dashboard.png"), "label": "Dashboard"},
            {"url": static("help/transaction_form.png"), "label": "Nova transa\u00e7\u00e3o"},
        ]
    plan_features = {
        "essential": [
            "Acessos manuais e ditados",
            "IA b\u00e1sica no painel",
            "Plano individual (sem convidados)",
            "Dashboards financeiros e tarefas em etapas",
        ],
        "pro": [
            "Tudo do Essencial",
            "Maior limite de tokens da IA",
            "At\u00e9 6 convidados por workspace",
            "ChatAgent no WhatsApp (texto, \u00e1udio e foto)",
        ],
        "master": [
            "Tudo do Pro",
            "Limites m\u00e1ximos de IA",
            "Convidados personalizados (a partir de 6)",
            "ChatAgent no WhatsApp + prioridade",
        ],
    }
    plan_ideal_for = {
        "essential": "Ideal para gest\u00e3o pessoal.",
        "pro": "Ideal para fam\u00edlias ou grupos pequenos.",
        "master": "Ideal para empresas e grupos grandes; voc\u00ea define a propor\u00e7\u00e3o do seu workspace.",
    }
    discount_pct_map = {
        "essential": 38,
        "pro": 45,
        "master": 63,
    }
    pricing_plans = []
    for tier in ("essential", "pro", "master"):
        monthly_price = plan_price(pricing_state, tier, "monthly", guest_limit=6)
        annual_price = plan_price(pricing_state, tier, "annual", guest_limit=6)
        pricing_plans.append({
            "tier": tier,
            "name": plan_label(tier),
            "badge": "Mais escolhido" if tier == "pro" else "Novo" if tier == "master" else "Essencial",
            "monthly_price": monthly_price,
            "annual_price": annual_price,
            "monthly_regular": plan_price(pricing_state, tier, "monthly", guest_limit=6, promo=False),
            "annual_regular": plan_price(pricing_state, tier, "annual", guest_limit=6, promo=False),
            "annual_total": annual_price * Decimal("12"),
            "features": plan_features[tier],
            "note": "Master: +15% por convidado acima de 6." if tier == "master" else "",
            "discount_pct": discount_pct_map.get(tier, 0),
            "ideal_for": plan_ideal_for.get(tier, ""),
        })

    feature_cards = [
        {
            "icon": "fa-solid fa-wallet",
            "title": "Financeiro inteligente",
            "description": "Dashboards em tempo real, entradas/sa\u00eddas, or\u00e7amentos e metas de saldo.",
        },
        {
            "icon": "fa-solid fa-list-check",
            "title": "Tarefas em etapas",
            "description": "Etapas com respons\u00e1veis, progresso autom\u00e1tico e prazos claros.",
        },
        {
            "icon": "fa-solid fa-bell",
            "title": "Notifica\u00e7\u00f5es",
            "description": "Central in-app, e-mails, lembretes e resumos semanais/mensais.",
        },
        {
            "icon": "fa-solid fa-robot",
            "title": "Agente de IA",
            "description": "Resumos, d\u00favidas e sugest\u00f5es dentro do seu workspace.",
        },
        {
            "icon": "fa-brands fa-whatsapp",
            "title": "ChatAgent no WhatsApp",
            "description": "Lance transa\u00e7\u00f5es e tarefas direto do WhatsApp com texto, \u00e1udio ou foto.",
        },
        {
            "icon": "fa-solid fa-cloud-arrow-down",
            "title": "Importa\u00e7\u00e3o e exporta\u00e7\u00e3o",
            "description": "CSV/PDF com preview e integra\u00e7\u00e3o via endpoint JSON.",
        },
        {
            "icon": "fa-solid fa-briefcase",
            "title": "Painel de workspaces",
            "description": "Troca r\u00e1pida de ambiente, convites e controle de membros.",
        },
    ]

    carousel_slides = [feature_cards[i:i + 3] for i in range(0, len(feature_cards), 3)]

    steps = [
        {"title": "Crie sua conta", "text": "Escolha plano e ciclo (Mensal ou Anual) para seu workspace."},
        {"title": "Lance com agilidade", "text": "CSV/PDF, Falar por voz e lan\u00e7amento r\u00e1pido."},
        {"title": "Conecte o WhatsApp", "text": "No Pro/Master, lance transa\u00e7\u00f5es e tarefas pelo ChatAgent."},
        {"title": "Acompanhe no painel", "text": "Dashboards, alertas e convites em poucos cliques."},
    ]

    faq_items = [
        {"question": "Quem paga o plano?", "answer": "Apenas o dono do workspace. O limite de convidados depende do plano."},
        {"question": "Posso escolher mensal ou anual?", "answer": "Sim. A escolha do ciclo \u00e9 feita no cadastro e pode ser revisada pelo admin."},
        {"question": "Tenho per\u00edodo de teste?", "answer": "Sim, novas contas iniciam com trial de 7 dias. Depois \u00e9 preciso ativar a assinatura."},
        {"question": "O que acontece se o pagamento vencer?", "answer": "Voc\u00ea entra em car\u00eancia: tarefas continuam dispon\u00edveis, mas finan\u00e7as e IA ficam bloqueadas at\u00e9 regularizar."},
        {"question": "Meu financeiro \u00e9 privado?", "answer": "Sim. Cada workspace isola finan\u00e7as; o owner controla permiss\u00f5es."},
        {"question": "O agente de IA usa meus dados?", "answer": "Sim, ele responde dentro do contexto do seu workspace e respeita permiss\u00f5es."},
        {"question": "Como funciona o WhatsApp?", "answer": "No Pro/Master, o ChatAgent recebe texto, \u00e1udio ou foto e lan\u00e7a no seu workspace."},
        {"question": "Posso falar por voz?", "answer": "Sim, use o bot\u00e3o Falar em transa\u00e7\u00f5es, tarefas e no agente de IA."},
        {"question": "Existe limite de IA?", "answer": "Cada plano tem um limite mensal de tokens; o painel mostra consumo e data de renova\u00e7\u00e3o."},
        {"question": "Posso exportar meus dados?", "answer": "Sim. Existem exports CSV e o bot\u00e3o de exportar dados no perfil (LGPD)."},
    ]

    context = {
        "pricing_plans": pricing_plans,
        "pricing_state": pricing_state,
        "landing_stats": landing_stats,
        "landing_screenshots": landing_screenshots,
        "carousel_slides": carousel_slides,
        "feature_cards": feature_cards,
        "steps": steps,
        "faq_items": faq_items,
    }
    return render(request, 'landing.html', context)


def help_page(request):
    quick_links = [
        {"id": "transacoes", "label": "Transa\u00e7\u00f5es", "icon": "fa-solid fa-coins"},
        {"id": "tarefas", "label": "Tarefas", "icon": "fa-solid fa-list-check"},
        {"id": "workspaces", "label": "Workspaces", "icon": "fa-solid fa-users"},
        {"id": "notificacoes", "label": "Notifica\u00e7\u00f5es", "icon": "fa-solid fa-bell"},
        {"id": "alertas", "label": "Or\u00e7amentos e metas", "icon": "fa-solid fa-bullseye"},
        {"id": "voz", "label": "Falar por voz", "icon": "fa-solid fa-microphone"},
        {"id": "ia", "label": "Agente de IA", "icon": "fa-solid fa-robot"},
        {"id": "whatsapp", "label": "WhatsApp", "icon": "fa-brands fa-whatsapp"},
        {"id": "filtros", "label": "Filtros e exporta\u00e7\u00e3o", "icon": "fa-solid fa-filter"},
        {"id": "faq", "label": "FAQ", "icon": "fa-solid fa-circle-question"},
    ]

    faq_items = [
        {
            "question": "Como pedir acesso a um workspace?",
            "answer": "Abra Perfil \u2192 Pedir acesso, informe o slug do workspace e aguarde a aprova\u00e7\u00e3o do owner.",
        },
        {
            "question": "Quem pode ver finan\u00e7as?",
            "answer": "Apenas o dono do workspace (owner) e o superuser. Membros comuns veem tarefas, mas n\u00e3o finan\u00e7as.",
        },
        {
            "question": "Como funcionam alertas?",
            "answer": "O owner configura or\u00e7amentos e metas em Notifica\u00e7\u00f5es. Alertas chegam por e-mail e in-app.",
        },
        {
            "question": "Onde configuro or\u00e7amentos e metas?",
            "answer": "Acesse Notifica\u00e7\u00f5es e use os formul\u00e1rios de Or\u00e7amentos por categoria e Metas de saldo.",
        },
        {
            "question": "Como ativar notificac\u00f5es por e-mail?",
            "answer": "No Perfil, marque as prefer\u00eancias desejadas (saldo, tarefas, resumos, alertas).",
        },
        {
            "question": "Como funciona o ditado por voz?",
            "answer": "Use o bot\u00e3o Falar em tarefas ou transa\u00e7\u00f5es. O sistema tenta extrair descri\u00e7\u00e3o, data, valor e etapas.",
        },
        {
            "question": "Como funciona o agente de IA?",
            "answer": "Ele responde com base nos dados do seu workspace e respeita permiss\u00f5es.",
        },
        {
            "question": "Como ativar o WhatsApp?",
            "answer": "Cadastre seu n\u00famero em Perfil \u2192 Agente do WhatsApp e escolha o workspace. Depois envie mensagens ao n\u00famero do iTracker.",
        },
        {
            "question": "Posso exportar dados?",
            "answer": "Sim, h\u00e1 exporta\u00e7\u00e3o CSV nas listas e endpoint JSON para gr\u00e1ficos.",
        },
        {
            "question": "Como trocar de workspace?",
            "answer": "Use o seletor de workspace no topo da tela para alternar entre ambientes.",
        },
        {
            "question": "Convidados acessam o financeiro?",
            "answer": "N\u00e3o. Convidados acessam tarefas do workspace, mas n\u00e3o veem finan\u00e7as nem IA.",
        },
        {
            "question": "Como ativar push no navegador?",
            "answer": "Na tela de Notifica\u00e7\u00f5es, clique em Ativar push. Dispon\u00edvel quando VAPID estiver configurado.",
        },
        {
            "question": "Como ver resumos r\u00e1pidos?",
            "answer": "Use o agente de IA com perguntas como: \"Resumo do m\u00eas\" ou \"Maior gasto do per\u00edodo\".",
        },
    ]

    return render(request, 'tracker/help.html', {"quick_links": quick_links, "faq_items": faq_items})


def terms_page(request):
    return render(request, 'tracker/terms.html')


def privacy_page(request):
    return render(request, 'tracker/privacy.html')


# -------- Utils --------

def _parse_date_input(value):
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# -------- Dashboard --------

@login_required
def dashboard(request):
    today = timezone.now().date()
    workspace = getattr(request, "workspace", None)
    can_finance = _user_can_view_finance(request, workspace)
    dash_ttl = int(getattr(settings, "DASHBOARD_CACHE_TTL", 300))

    last30_start = today - datetime.timedelta(days=30)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        messages.info(request, 'Contas convidadas podem apenas lan\u00e7ar transa\u00e7\u00f5es e gerenciar tarefas.')
        return redirect('tracker:tasks_list')
    trial_active = bool(profile and profile.trial_active())
    trial_expires_at = profile.trial_expires_at if profile else None
    trial_remaining = None
    if trial_expires_at:
        trial_remaining = max((trial_expires_at.date() - today).days, 0)

    def shift_months(date_obj: datetime.date, months: int) -> datetime.date:
        year = date_obj.year + ((date_obj.month - 1 + months) // 12)
        month = (date_obj.month - 1 + months) % 12 + 1
        days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        day = min(date_obj.day, days_in_month)
        return datetime.date(year, month, day)

    reference_date = today

    def range_from_period(period: str):
        if period == 'week':
            return reference_date - datetime.timedelta(days=7), reference_date
        if period == 'month':
            return shift_months(reference_date, -1), reference_date
        if period == 'quarter':
            return shift_months(reference_date, -3), reference_date
        if period == 'semester':
            return shift_months(reference_date, -6), reference_date
        if period == 'year':
            return shift_months(reference_date, -12), reference_date
        if period == 'all' and can_finance:
            scoped = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user)
            first_tx = scoped.order_by('date').first()
            last_tx = scoped.order_by('-date').first()
            if first_tx and last_tx:
                return first_tx.date, last_tx.date
        return reference_date.replace(day=1), reference_date

    period = request.GET.get('period', '') or 'all'
    month_param = request.GET.get('month') or ''
    start_param = request.GET.get('start')
    end_param = request.GET.get('end')
    responsible_param = request.GET.get('responsible') or ''

    start_date = None
    end_date = None
    try:
        start_date = datetime.date.fromisoformat(start_param) if start_param else None
    except ValueError:
        start_date = None
    try:
        end_date = datetime.date.fromisoformat(end_param) if end_param else None
    except ValueError:
        end_date = None

    if month_param:
        try:
            year_str, month_str = month_param.split('-')
            year = int(year_str)
            month = int(month_str)
            start_date = datetime.date(year, month, 1)
            last_day = calendar.monthrange(year, month)[1]
            end_date = datetime.date(year, month, last_day)
        except (ValueError, TypeError):
            month_param = ''

    if not start_date or not end_date:
        start_date, end_date = range_from_period(period)

    base_qs = _apply_workspace_filter(
        Transaction.objects.select_related('category', 'responsible').filter(date__gte=start_date, date__lte=end_date),
        workspace,
        request.user,
    ) if can_finance else Transaction.objects.none()
    if responsible_param.isdigit():
        base_qs = base_qs.filter(responsible_id=int(responsible_param))
    dash_cache_key = f"dash:{request.user.id}:{workspace.id if workspace else 'global'}:{start_date}:{end_date}:{responsible_param}"
    dash_cached = cache.get(dash_cache_key)
    if dash_cached:
        period_has_data = dash_cached['period_has_data']
        income_total = dash_cached['income_total']
        expense_total = dash_cached['expense_total']
        tx_count_period = dash_cached['tx_count_period']
        chart_labels = dash_cached['chart_labels']
        chart_values = dash_cached['chart_values']
        top_income = dash_cached['top_income']
        top_expense = dash_cached['top_expense']
    else:
        period_has_data = base_qs.exists()
        income_total = base_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
        expense_total = base_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
        tx_count_period = base_qs.count()

    last30_qs = _apply_workspace_filter(
        Transaction.objects.select_related('category', 'responsible').filter(date__gte=last30_start, date__lte=today),
        workspace,
        request.user,
    ) if can_finance else Transaction.objects.none()
    if responsible_param.isdigit():
        last30_qs = last30_qs.filter(responsible_id=int(responsible_param))
    last30_income = last30_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    last30_expense = last30_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    net_30 = last30_income - last30_expense
    avg_daily_expense = (last30_expense / 30) if last30_expense else 0

    if not dash_cached:
        category_net = base_qs.values('category__name').annotate(
            total=Sum(
                Case(
                    When(type='income', then=F('value')),
                    When(type='expense', then=F('value') * -1),
                    default=0,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )
        chart_labels = [item['category__name'] for item in category_net]
        chart_values = [float(item['total'] or 0) for item in category_net]

        top_income = list(
            base_qs.filter(type='income')
            .values('category__name')
            .annotate(total=Sum('value'))
            .order_by('-total')[:5]
        )
        top_expense = list(
            base_qs.filter(type='expense')
            .values('category__name')
            .annotate(total=Sum('value'))
            .order_by('-total')[:5]
        )
        cache.set(
            dash_cache_key,
            {
                'period_has_data': period_has_data,
                'income_total': income_total,
                'expense_total': expense_total,
                'tx_count_period': tx_count_period,
                'chart_labels': chart_labels,
                'chart_values': chart_values,
                'top_income': top_income,
                'top_expense': top_expense,
            },
            dash_ttl,
        )
    top_income_item = top_income[0] if top_income else None
    top_expense_item = top_expense[0] if top_expense else None
    top_net_label = None
    top_net_value = None
    if chart_labels and chart_values:
        idx = max(range(len(chart_values)), key=lambda i: abs(chart_values[i]))
        top_net_label = chart_labels[idx]
        top_net_value = chart_values[idx]
    quick_ranges = [
        ('week', 'Ultima semana'),
        ('month', 'Ultimo mes'),
        ('quarter', 'Ultimo trimestre'),
        ('semester', 'Ultimo semestre'),
        ('year', 'Ultimo ano'),
        ('all', 'Tudo'),
    ]
    month_labels = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
    month_options = []
    base_month = today.replace(day=1)
    for idx in range(0, 24):
        month_date = shift_months(base_month, -idx)
        start_month = month_date.replace(day=1)
        last_day = calendar.monthrange(month_date.year, month_date.month)[1]
        end_month = datetime.date(month_date.year, month_date.month, last_day)
        month_options.append({
            'value': f'{month_date.year:04d}-{month_date.month:02d}',
            'label': f'{month_labels[month_date.month - 1]}/{str(month_date.year)[2:]}',
            'start': start_month,
            'end': end_month,
        })

    tasks_qs = _apply_workspace_filter(
        Task.objects.select_related('responsible_user').prefetch_related('steps'),
        workspace,
        request.user,
    )
    if responsible_param.isdigit():
        tasks_qs = tasks_qs.filter(responsible_user_id=int(responsible_param))
    tasks_total = tasks_qs.count()
    tasks_done = tasks_qs.filter(status='done').count()
    tasks_overdue = tasks_qs.filter(status='ongoing', due_date__lt=today).count()
    tasks_progress_pct = round((tasks_done / tasks_total) * 100, 1) if tasks_total else 100
    categories_qs = _apply_workspace_filter(Category.objects.all(), workspace, request.user)
    category_count = categories_qs.count()
    total_transactions = 0
    if can_finance:
        total_transactions = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user).count()
    budget_count = 0
    goal_count = 0
    if workspace:
        budget_count = CategoryBudget.objects.filter(workspace=workspace).count()
        goal_count = BalanceGoal.objects.filter(workspace=workspace).count()
    ai_message_count = 0
    if not (profile and profile.is_guest):
        ai_message_count = ChatMessage.objects.filter(user=request.user).count()
    profile_has_avatar = bool(profile and profile.avatar)
    show_responsible_column = bool(request.user.is_superuser or (workspace and workspace.owner_id == request.user.id))

    onboarding_items = []
    onboarding_items.append({
        'label': 'Adicionar foto ao perfil (opcional)',
        'done': profile_has_avatar,
        'url': reverse('tracker:profile_edit'),
        'optional': True,
    })
    if can_finance:
        onboarding_items.append({
            'label': 'Criar categoria',
            'done': category_count > 0,
            'url': reverse('tracker:categories_list'),
        })
        onboarding_items.append({
            'label': 'Primeira transa\u00e7\u00e3o',
            'done': total_transactions > 0,
            'url': reverse('tracker:transaction_create'),
        })
        onboarding_items.append({
            'label': 'Importar CSV/PDF (opcional)',
            'done': total_transactions >= 5,
            'url': reverse('tracker:transaction_import'),
            'optional': True,
        })
        onboarding_items.append({
            'label': 'Definir or\u00e7amento',
            'done': budget_count > 0,
            'url': reverse('tracker:category_budget_create'),
        })
        onboarding_items.append({
            'label': 'Criar meta de saldo',
            'done': goal_count > 0,
            'url': reverse('tracker:balance_goal_create'),
        })
    onboarding_items.append({
        'label': 'Criar tarefa',
        'done': tasks_total > 0,
        'url': reverse('tracker:task_create'),
    })
    onboarding_items.append({
        'label': 'Concluir tarefa',
        'done': tasks_done > 0,
        'url': reverse('tracker:tasks_list'),
    })
    if not (profile and profile.is_guest):
        onboarding_items.append({
            'label': 'Usar agente de IA',
            'done': ai_message_count > 0,
            'url': reverse('assistant:chat'),
        })
    if workspace and (request.user.is_superuser or workspace.owner_id == request.user.id):
        has_members = WorkspaceMembership.objects.filter(workspace=workspace).exclude(role='owner').exists()
        onboarding_items.append({
            'label': 'Convidar membro para seu workspace (opcional)',
            'done': has_members,
            'url': reverse('tracker:workspace_invite', args=[workspace.slug]),
            'optional': True,
        })
    show_onboarding = any(not item['done'] and not item.get('optional') for item in onboarding_items)
    latest_tasks = list(tasks_qs.order_by('-created_at')[:5])
    for task in latest_tasks:
        completed_late = False
        if task.status == 'done' and task.due_date:
            completed_at = task.completed_at or task.updated_at
            completed_date = timezone.localdate(completed_at) if completed_at else None
            if completed_date and completed_date > task.due_date:
                completed_late = True
        task.completed_late = completed_late

    responsible_users = []
    if workspace:
        member_ids = set(
            WorkspaceMembership.objects.filter(workspace=workspace)
            .values_list('user_id', flat=True)
        )
        if workspace.owner_id:
            member_ids.add(workspace.owner_id)
        responsible_users = list(User.objects.filter(id__in=member_ids).order_by('first_name', 'username'))

    period_days = max(1, (end_date - start_date).days + 1)
    period_income = income_total
    period_expense = expense_total
    net_period = period_income - period_expense
    avg_daily_expense_period = (period_expense / period_days) if period_expense else 0

    tasks_period_qs = tasks_qs.filter(
        Q(due_date__gte=start_date, due_date__lte=end_date) |
        Q(created_at__date__gte=start_date, created_at__date__lte=end_date)
    )
    tasks_period_total = tasks_period_qs.count()
    tasks_period_done = tasks_period_qs.filter(status='done').count()
    tasks_period_overdue = tasks_period_qs.filter(status='ongoing', due_date__lt=today).count()

    finance_eval = {
        'class': 'text-secondary',
        'title': 'Sem dados recentes',
        'message': 'Sem movimentações no período selecionado. Registre entradas e saídas para desbloquear insights.',
    }
    if net_period > 0:
        finance_eval = {
            'class': 'value-positive',
            'title': 'Saldo positivo',
            'message': 'Boa! Seu saldo no período selecionado está positivo. Considere reservar parte para metas ou emergências.',
        }
    elif net_period < 0:
        finance_eval = {
            'class': 'value-negative',
            'title': 'Saldo negativo',
            'message': 'Atenção: seu saldo no período selecionado ficou negativo. Revise categorias críticas e ajuste limites.',
        }

    task_eval = {
        'class': 'text-secondary',
        'title': 'Sem tarefas',
        'message': 'Sem tarefas no período selecionado. Crie tarefas para acompanhar prazos e evolução.',
    }
    if tasks_period_total:
        overdue_ratio = (tasks_period_overdue / tasks_period_total) if tasks_period_total else 0
        done_ratio = (tasks_period_done / tasks_period_total) if tasks_period_total else 0
        if overdue_ratio >= 0.5:
            task_eval = {
                'class': 'value-negative',
                'title': 'Muitas tarefas atrasadas',
                'message': 'Priorize as tarefas vencidas e reavalie prazos. O foco agora é reduzir atrasos.',
            }
        elif overdue_ratio > 0:
            task_eval = {
                'class': 'text-warning',
                'title': 'Algumas tarefas atrasadas',
                'message': 'Organize prazos e finalize as pendentes para evitar acúmulo.',
            }
        elif done_ratio >= 0.7:
            task_eval = {
                'class': 'value-positive',
                'title': 'Tarefas em dia',
                'message': 'Boa! A maioria das tarefas está concluída. Continue mantendo o ritmo.',
            }
        else:
            task_eval = {
                'class': 'text-info',
                'title': 'Progresso em andamento',
                'message': 'Continue avançando e revise prioridades quando precisar.',
            }

    finance_items = [
        {
            'label': 'Saldo do período',
            'value': net_period,
            'class': 'value-positive' if net_period >= 0 else 'value-negative',
            'icon': 'fa-solid fa-scale-balanced',
        },
        {
            'label': 'Entradas do período',
            'value': period_income,
            'class': 'value-positive' if period_income > 0 else 'text-secondary',
            'icon': 'fa-solid fa-arrow-trend-up',
        },
        {
            'label': 'Saídas do período',
            'value': period_expense,
            'class': 'value-negative' if period_expense > 0 else 'text-secondary',
            'icon': 'fa-solid fa-arrow-trend-down',
        },
        {
            'label': 'Despesa média/dia',
            'value': avg_daily_expense_period,
            'class': 'text-danger' if avg_daily_expense_period > 0 else 'text-secondary',
            'icon': 'fa-solid fa-calendar-day',
        },
    ]
    if top_expense_item:
        finance_items.append({
            'label': f"Maior gasto: {top_expense_item['category__name']}",
            'value': top_expense_item['total'],
            'class': 'value-negative' if top_expense_item['total'] else 'text-secondary',
            'icon': 'fa-solid fa-money-bill-trend-down',
        })

    task_items = [
        {
            'label': 'Abertas no período',
            'value': tasks_period_qs.filter(status='ongoing').count(),
            'class': 'text-warning' if tasks_period_overdue else 'text-info',
            'icon': 'fa-regular fa-circle-dot',
        },
        {
            'label': 'Concluídas no período',
            'value': tasks_period_done,
            'class': 'value-positive' if tasks_period_done else 'text-secondary',
            'icon': 'fa-solid fa-circle-check',
        },
        {
            'label': 'Atrasadas no período',
            'value': tasks_period_overdue,
            'class': 'value-negative' if tasks_period_overdue else 'text-secondary',
            'icon': 'fa-solid fa-triangle-exclamation',
        },
        {
            'label': 'Progresso',
            'value': round((tasks_period_done / tasks_period_total) * 100, 1) if tasks_period_total else 0,
            'suffix': '%',
            'class': 'value-positive' if (tasks_period_total and (tasks_period_done / tasks_period_total) >= 0.7) else ('text-warning' if (tasks_period_total and (tasks_period_done / tasks_period_total) >= 0.4) else 'value-negative'),
            'icon': 'fa-solid fa-gauge-high',
        },
    ]

    context = {
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': income_total - expense_total,
        'tasks_open': tasks_qs.filter(status='ongoing').count(),
        'tasks_done': tasks_done,
        'tasks_overdue': tasks_overdue,
        'tasks_progress_pct': tasks_progress_pct,
        'latest_transactions': base_qs.order_by('-date')[:5],
        'latest_tasks': latest_tasks,
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'start_date': start_date,
        'end_date': end_date,
        'period': period,
        'responsible_filter': responsible_param,
        'responsible_users': responsible_users,
        'top_income': top_income,
        'top_expense': top_expense,
        'insights': {
            'top_income_label': top_income_item['category__name'] if top_income_item else None,
            'top_income_value': top_income_item['total'] if top_income_item else 0,
            'top_expense_label': top_expense_item['category__name'] if top_expense_item else None,
            'top_expense_value': top_expense_item['total'] if top_expense_item else 0,
            'top_net_label': top_net_label,
            'top_net_value': top_net_value or 0,
        },
        'quick_ranges': quick_ranges,
        'month_options': month_options,
        'selected_month': month_param,
        'period_has_data': period_has_data,
        'workspace_mode': workspace,
        'can_finance': can_finance,
        'tx_count_period': tx_count_period,
        'net_30': net_30,
        'avg_daily_expense': avg_daily_expense,
        'agent_eval': {
            'finance': {**finance_eval, 'items': finance_items},
            'tasks': {**task_eval, 'items': task_items},
        },
        'category_count': category_count,
        'trial': {
            'active': trial_active,
            'expires_at': trial_expires_at,
            'remaining': trial_remaining,
        },
        'onboarding': {
            'show': show_onboarding,
            'items': onboarding_items,
        },
        'show_responsible_column': show_responsible_column,
    }
    return render(request, 'tracker/dashboard.html', context)


# -------- Transactions --------

@login_required
def transactions_list(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode acessar finan\u00e7as deste workspace.')
        return _finance_access_denied_redirect(request)

    transactions_qs = _apply_workspace_filter(Transaction.objects.select_related('category', 'responsible'), workspace, request.user)
    search = request.GET.get('q', '').strip()
    tx_type = request.GET.get('type', '')
    category_id = request.GET.get('category', '')
    start_raw = request.GET.get('start', '')
    end_raw = request.GET.get('end', '')
    start = _parse_date_input(start_raw)
    end = _parse_date_input(end_raw)

    if search:
        transactions_qs = transactions_qs.filter(Q(description__icontains=search) | Q(category__name__icontains=search))
    if tx_type in ('income', 'expense'):
        transactions_qs = transactions_qs.filter(type=tx_type)
    if category_id:
        transactions_qs = transactions_qs.filter(category_id=category_id)
    if start:
        transactions_qs = transactions_qs.filter(date__gte=start)
    if end:
        transactions_qs = transactions_qs.filter(date__lte=end)

    transactions = transactions_qs.order_by('-date', '-created_at')

    if request.GET.get('export') == 'csv':
        return _export_transactions_csv(transactions)

    income_total = transactions.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense_total = transactions.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    paginator = Paginator(transactions, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    def fmt(val):
        try:
            return datetime.date.fromisoformat(val).strftime('%d/%m/%Y')
        except Exception:
            return val or ''

    category_label = ''
    if category_id:
        category_obj = Category.objects.filter(id=category_id).first()
        category_label = category_obj.name if category_obj else str(category_id)
    type_label = {'income': 'Entrada', 'expense': 'Sa\u00edda'}.get(tx_type, '')
    filter_chips = _build_filter_chips(
        request,
        [
            ('q', 'Busca', search),
            ('type', 'Tipo', type_label),
            ('category', 'Categoria', category_label),
            ('start', 'De', fmt(start_raw)),
            ('end', 'At\u00e9', fmt(end_raw)),
        ],
    )

    context = {
        'transactions_page': page_obj,
        'transactions_total': paginator.count,
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': income_total - expense_total,
        'categories': Category.objects.filter(workspace=workspace) if workspace else Category.objects.all(),
        'bulk_form': TransactionBulkUpdateForm(workspace=workspace),
        'query_string': query_string,
        'today': timezone.now().date(),
        'filters': {
            'q': search,
            'type': tx_type,
            'category': category_id,
            'start': start.isoformat() if start else '',
            'end': end.isoformat() if end else '',
        },
        'filters_display': {
            'start': fmt(start_raw),
            'end': fmt(end_raw),
        },
        'filter_chips': filter_chips,
    }
    return render(request, 'tracker/transactions_list.html', context)


@login_required
def transactions_bulk_update(request):
    if request.method != 'POST':
        return redirect('tracker:transactions_list')
    workspace = getattr(request, "workspace", None)
    if workspace and not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode editar transa\u00e7\u00f5es neste workspace.')
        return _finance_access_denied_redirect(request)
    select_all = request.POST.get('select_all') == '1'
    ids_raw = (request.POST.get('ids') or '').strip()
    ids = [int(val) for val in ids_raw.split(',') if val.isdigit()]
    if not select_all and not ids:
        messages.warning(request, 'Selecione ao menos uma transa\u00e7\u00e3o.')
        return redirect('tracker:transactions_list')
    form = TransactionBulkUpdateForm(request.POST, workspace=workspace)
    if not form.is_valid():
        messages.error(request, 'N\u00e3o foi poss\u00edvel aplicar as altera\u00e7\u00f5es.')
        return redirect('tracker:transactions_list')
    if select_all:
        qs = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user)
        search = (request.POST.get('q') or '').strip()
        tx_type = request.POST.get('type', '')
        category_id = request.POST.get('category', '')
        start = request.POST.get('start', '')
        end = request.POST.get('end', '')
        start_date = _parse_date_input(start)
        end_date = _parse_date_input(end)
        if search:
            qs = qs.filter(Q(description__icontains=search) | Q(category__name__icontains=search))
        if tx_type in ('income', 'expense'):
            qs = qs.filter(type=tx_type)
        if category_id:
            qs = qs.filter(category_id=category_id)
        if start_date:
            qs = qs.filter(date__gte=start_date)
        if end_date:
            qs = qs.filter(date__lte=end_date)
    else:
        qs = Transaction.objects.filter(id__in=ids)
        if workspace:
            qs = qs.filter(workspace=workspace)
        elif not request.user.is_superuser:
            qs = qs.none()
    updates = {}
    category = form.cleaned_data.get('category')
    responsible = form.cleaned_data.get('responsible')
    new_date = form.cleaned_data.get('date')
    tx_type = form.cleaned_data.get('type')
    icon = form.cleaned_data.get('icon')
    selected_action = form.cleaned_data.get('selected_action')
    if request.POST.get('bulk_delete') == '1':
        deleted_count = qs.count()
        snapshot_limit = 200
        payload = []
        if deleted_count:
            for item in list(qs.values(
                'description',
                'date',
                'value',
                'type',
                'category_id',
                'responsible_id',
                'workspace_id',
                'selected',
                'icon',
            )[:snapshot_limit]):
                payload.append({
                    'description': item.get('description', ''),
                    'date': item.get('date').isoformat() if item.get('date') else '',
                    'value': str(item.get('value')) if item.get('value') is not None else '0',
                    'type': item.get('type', 'expense'),
                    'category_id': item.get('category_id'),
                    'responsible_id': item.get('responsible_id'),
                    'workspace_id': item.get('workspace_id'),
                    'selected': bool(item.get('selected')),
                    'icon': item.get('icon') or '',
                })
        if payload:
            request.session['undo_tx_bulk'] = payload
            request.session['undo_tx_bulk_expires'] = (timezone.now() + datetime.timedelta(minutes=10)).isoformat()
        qs.delete()
        _invalidate_workspace_caches(workspace)
        messages.success(request, f'{deleted_count} transa\u00e7\u00f5es exclu\u00eddas. Voc\u00ea pode desfazer a a\u00e7\u00e3o.')
        return redirect('tracker:transactions_list')
    if request.POST.get('bulk_mark') == '1':
        qs.update(selected=True)
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Transa\u00e7\u00f5es destacadas.')
        return redirect('tracker:transactions_list')
    if request.POST.get('bulk_convert') == '1':
        qs.update(
            type=Case(
                When(type='income', then=Value('expense')),
                When(type='expense', then=Value('income')),
                default=Value('expense'),
                output_field=CharField(),
            )
        )
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Transa\u00e7\u00f5es convertidas.')
        return redirect('tracker:transactions_list')
    if category:
        updates['category'] = category
    if responsible:
        updates['responsible'] = responsible
    if new_date:
        updates['date'] = new_date
    if tx_type:
        updates['type'] = tx_type
    if icon:
        updates['icon'] = icon
    if selected_action == 'mark':
        updates['selected'] = True
    elif selected_action == 'unmark':
        updates['selected'] = False
    if not updates:
        messages.warning(request, 'Nenhuma altera\u00e7\u00e3o escolhida.')
        return redirect('tracker:transactions_list')
    qs.update(**updates)
    _invalidate_workspace_caches(workspace)
    messages.success(request, 'Transa\u00e7\u00f5es atualizadas.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_create(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if not _user_can_create_transaction(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode lan\u00e7ar finan\u00e7as neste workspace.')
        return _finance_access_denied_redirect(request)
    initial = {'date': timezone.now().date()}
    if workspace and workspace.owner_id:
        initial['responsible'] = workspace.owner_id
    form = TransactionForm(request.POST or None, initial=initial, workspace=workspace)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if workspace:
            obj.workspace = workspace
        obj.save()
        _invalidate_workspace_caches(workspace)
        if workspace:
            notify_balance_threshold(workspace.owner, workspace)
            check_category_budgets(workspace)
            check_balance_goals(workspace)
        messages.success(request, 'Transação criada com sucesso.')
        if request.POST.get('save_new') or (profile and profile.is_guest):
            return redirect('tracker:transaction_create')
        return redirect('tracker:transactions_list')
    return render(request, 'tracker/transaction_form.html', {'form': form, 'is_edit': False})


@login_required
def transaction_update(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode editar finanças deste workspace.')
        return _finance_access_denied_redirect(request)
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    transaction = get_object_or_404(qs, pk=pk)
    form = TransactionForm(request.POST or None, instance=transaction, workspace=workspace)
    is_detail = request.GET.get('detail') == '1'
    if request.method == 'POST' and form.is_valid():
        form.save()
        _invalidate_workspace_caches(workspace)
        if workspace:
            check_category_budgets(workspace)
            check_balance_goals(workspace)
        messages.success(request, 'Transação atualizada.')
        return redirect('tracker:transactions_list')
    return render(request, 'tracker/transaction_form.html', {'form': form, 'is_edit': True, 'object': transaction, 'is_detail': is_detail})


@login_required

@login_required
def transaction_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover finan\u00e7as deste workspace.')
        return _finance_access_denied_redirect(request)
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    transaction = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        request.session['undo_tx'] = {
            'description': transaction.description,
            'date': transaction.date.isoformat(),
            'value': str(transaction.value),
            'type': transaction.type,
            'category_id': transaction.category_id,
            'responsible_id': transaction.responsible_id,
            'workspace_id': transaction.workspace_id,
            'selected': transaction.selected,
        }
        request.session['undo_tx_expires'] = (timezone.now() + datetime.timedelta(minutes=10)).isoformat()
        transaction.delete()
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Transa\u00e7\u00e3o removida. Voc\u00ea pode desfazer a a\u00e7\u00e3o.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_undo_delete(request):
    if request.method != 'POST':
        return redirect('tracker:transactions_list')
    payload = request.session.get('undo_tx')
    expires = request.session.get('undo_tx_expires')
    if not payload:
        messages.error(request, 'Nada para desfazer.')
        return redirect('tracker:transactions_list')
    if expires:
        try:
            expires_at = datetime.datetime.fromisoformat(expires)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
            if timezone.now() > expires_at:
                request.session.pop('undo_tx', None)
                request.session.pop('undo_tx_expires', None)
                messages.error(request, 'O tempo para desfazer expirou.')
                return redirect('tracker:transactions_list')
        except Exception:
            pass
    category = Category.objects.filter(id=payload.get('category_id')).first()
    if not category:
        messages.error(request, 'Categoria da transa\u00e7\u00e3o n\u00e3o est\u00e1 mais dispon\u00edvel.')
        return redirect('tracker:transactions_list')
    date_val = payload.get('date')
    value_val = payload.get('value')
    if date_val:
        try:
            date_val = datetime.date.fromisoformat(date_val)
        except Exception:
            pass
    if value_val is not None:
        try:
            value_val = Decimal(str(value_val))
        except Exception:
            value_val = 0
    tx = Transaction(
        description=payload.get('description', ''),
        date=date_val,
        value=value_val,
        type=payload.get('type', 'expense'),
        category=category,
        responsible_id=payload.get('responsible_id'),
        workspace_id=payload.get('workspace_id'),
        selected=bool(payload.get('selected')),
    )
    tx.save()
    _invalidate_workspace_caches(tx.workspace)
    request.session.pop('undo_tx', None)
    request.session.pop('undo_tx_expires', None)
    messages.success(request, 'Transa\u00e7\u00e3o restaurada.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_undo_clear(request):
    if request.method == 'POST':
        request.session.pop('undo_tx', None)
        request.session.pop('undo_tx_expires', None)
    return JsonResponse({'ok': True})


@login_required
def transaction_bulk_undo_delete(request):
    if request.method != 'POST':
        return redirect('tracker:transactions_list')
    payload = request.session.get('undo_tx_bulk')
    expires = request.session.get('undo_tx_bulk_expires')
    if not payload:
        messages.error(request, 'Nada para desfazer.')
        return redirect('tracker:transactions_list')
    if expires:
        try:
            expires_at = datetime.datetime.fromisoformat(expires)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
            if timezone.now() > expires_at:
                request.session.pop('undo_tx_bulk', None)
                request.session.pop('undo_tx_bulk_expires', None)
                messages.error(request, 'O tempo para desfazer expirou.')
                return redirect('tracker:transactions_list')
        except Exception:
            pass
    restored = 0
    for item in payload:
        category = Category.objects.filter(id=item.get('category_id')).first()
        if not category:
            continue
        date_val = item.get('date')
        value_val = item.get('value')
        if date_val:
            try:
                date_val = datetime.date.fromisoformat(date_val)
            except Exception:
                date_val = None
        try:
            value_val = Decimal(str(value_val))
        except Exception:
            value_val = Decimal('0')
        Transaction.objects.create(
            description=item.get('description', ''),
            date=date_val or timezone.localdate(),
            value=value_val,
            type=item.get('type', 'expense'),
            category=category,
            responsible_id=item.get('responsible_id'),
            workspace_id=item.get('workspace_id'),
            selected=bool(item.get('selected')),
            icon=item.get('icon') or '',
        )
        restored += 1
    request.session.pop('undo_tx_bulk', None)
    request.session.pop('undo_tx_bulk_expires', None)
    messages.success(request, f'{restored} transa\u00e7\u00f5es restauradas.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_bulk_undo_clear(request):
    if request.method == 'POST':
        request.session.pop('undo_tx_bulk', None)
        request.session.pop('undo_tx_bulk_expires', None)
    return JsonResponse({'ok': True})


def transaction_toggle_selected(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover finan\u00e7as deste workspace.')
        return _finance_access_denied_redirect(request)

    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    tx = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        tx.selected = not tx.selected
        tx.save(update_fields=['selected', 'updated_at'])
        _invalidate_workspace_caches(workspace)
        if tx.selected:
            messages.success(request, 'Transação destacada.')
        else:
            messages.success(request, 'Transação removida dos destaques.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_toggle_type(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover finan\u00e7as deste workspace.')
        return _finance_access_denied_redirect(request)

    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    tx = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        tx.type = 'expense' if tx.type == 'income' else 'income'
        tx.save(update_fields=['type', 'updated_at'])
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Transação convertida com sucesso.')
    return redirect('tracker:transactions_list')


def _parse_statement_rows(text: str, categories_qs):
    sample = text[:2048]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=',;')
        delimiter = sniffed.delimiter
    except Exception:
        delimiter = ','

    try:
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
        header = next(reader, None)
    except csv.Error:
        header = None
        reader = None
    if not header:
        lines = [
            row
            for row in (line.split(delimiter) for line in text.splitlines())
            if any(cell.strip() for cell in row)
        ]
        if not lines:
            return []
        header = lines[0]
        reader = iter(lines[1:])

    if not header:
        return []

    return _parse_statement_rows_from_reader(header, reader)


def _parse_statement_rows_from_reader(header, reader):
    header = [c.strip().lower() for c in header]
    has_keywords = any(k in header for k in ('description', 'descrição', 'descrição', 'valor', 'value', 'data', 'date'))

    if has_keywords:
        key_map = {
            'descrição': 'description', 'descrição': 'description', 'description': 'description',
            'data': 'date', 'date': 'date',
            'valor': 'value', 'value': 'value',
            'tipo': 'type', 'type': 'type',
            'categoria': 'category', 'category': 'category',
        }
        mapped_rows = []
        for row in reader:
            if not row:
                continue
            mapped = {}
            for idx, key in enumerate(header):
                if idx >= len(row):
                    continue
                norm = key.strip().lower()
                target = key_map.get(norm, norm)
                mapped[target] = row[idx]
            mapped_rows.append(mapped)
        return mapped_rows

    data_rows = []
    for row in ([header] + list(reader)):
        if len(row) < 2:
            continue
        raw_date = (row[0] or '').strip()
        desc = (row[1] or '').strip()
        val = row[2] if len(row) > 2 else ''
        t_type = row[3] if len(row) > 3 else ''
        cat = row[4] if len(row) > 4 else ''
        date_part = raw_date
        for fmt in ('%d/%m/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y'):
            try:
                dt = datetime.datetime.strptime(raw_date, fmt)
                date_part = dt.date().isoformat()
                break
            except Exception:
                continue
        data_rows.append({
            'date': date_part,
            'description': desc,
            'value': val,
            'type': t_type,
            'category': cat,
        })
    return data_rows


def _parse_statement_rows_file(file_obj):
    try:
        file_obj.seek(0)
        sample_bytes = file_obj.read(4096)
        file_obj.seek(0)
    except Exception:
        sample_bytes = b''
    sample_text = ''
    if isinstance(sample_bytes, bytes):
        sample_text = sample_bytes.decode('utf-8', errors='ignore')
    else:
        sample_text = str(sample_bytes)
    try:
        sniffed = csv.Sniffer().sniff(sample_text, delimiters=',;')
        delimiter = sniffed.delimiter
    except Exception:
        delimiter = ','
    file_obj.seek(0)
    text_stream = io.TextIOWrapper(file_obj, encoding='utf-8', errors='ignore', newline='')
    reader = csv.reader(text_stream, delimiter=delimiter)
    header = next(reader, None)
    if not header:
        return []
    return _parse_statement_rows_from_reader(header, reader)


def _parse_pdf_statement(file_bytes: bytes):
    """
    Parser específico para extrato PicPay (data em uma linha, hora/descrição/valor na linha seguinte).
    """
    text = ""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(io.BytesIO(file_bytes)) or ""
    except Exception:
        text = ""

    if not text.strip():
        try:
            from PyPDF2 import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join([page.extract_text() or "" for page in reader.pages])
        except Exception as exc:
            return [], f"Não foi possível ler o PDF: {exc}"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    date_only = re.compile(r'^\d{2}/\d{2}/\d{4}$')
    time_desc_val = re.compile(r'^(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<desc>.+?)\s+(?P<value>-?\s*R?\$?\s*[\d\.\s]*[,\.\,]\d{2})', re.IGNORECASE)
    rows = []
    pending_date = None

    for line in lines:
        if date_only.match(line):
            pending_date = line
            continue

        m = time_desc_val.search(line)
        if m and pending_date:
            desc = (m.group('desc') or '').strip()
            value_raw = m.group('value') or ''
            desc = desc.replace('- -', '').strip()
            value_clean = value_raw
            for rep in ['R$', 'r$', ' ']:
                value_clean = value_clean.replace(rep, '')
            value_clean = value_clean.replace('.', '')
            value_clean = value_clean.replace(',', '.')
            negative = '-' in value_raw or (value_clean.startswith('(') and value_clean.endswith(')'))
            value_clean = value_clean.strip('()').replace('-', '')
            rows.append({
                'description': desc or f"Movimentação {pending_date}",
                'date': pending_date,
                'value': f"-{value_clean}" if negative else value_clean,
                'type': '',
                'category': '',
            })
            pending_date = None
            continue

        # fallback para linha com data e valor juntos
        full = re.search(r'(?P<date>\d{2}/\d{2}/\d{4}).+?(?P<value>-?\s*R?\$?\s*[\d\.\s]*[,\.\,]\d{2})', line)
        if full:
            date_raw = full.group('date')
            value_raw = full.group('value')
            desc_part = line.replace(date_raw, '').replace(value_raw, '').strip()
            value_clean = value_raw
            for rep in ['R$', 'r$', ' ']:
                value_clean = value_clean.replace(rep, '')
            value_clean = value_clean.replace('.', '')
            value_clean = value_clean.replace(',', '.')
            negative = '-' in value_raw or (value_clean.startswith('(') and value_clean.endswith(')'))
            value_clean = value_clean.strip('()').replace('-', '')
            rows.append({
                'description': desc_part or f"Movimentação {date_raw}",
                'date': date_raw,
                'value': f"-{value_clean}" if negative else value_clean,
                'type': '',
                'category': '',
            })
            pending_date = None

    if not rows:
        return [], "Nenhuma linha reconhecida no PDF (pode ser PDF de imagem). Converta para CSV/Excel para garantir."
    return rows, None


def _build_preview(rows, categories):
    preview = []
    ai_used = False
    for row in rows:
        desc = (row.get('description') or '').strip()
        raw_value = str(row.get('value') or '').strip()
        # extrai a última ocorrência numérica (suporta R$, separador , ou .)
        num_candidates = re.findall(r'[-+]?\d[\d\.\s]*[,\.\,]\d{1,2}', raw_value)
        if num_candidates:
            raw_value = num_candidates[-1]
        raw_value = raw_value.replace('R$', '').replace('$', '')
        raw_value = raw_value.replace(' ', '')
        if raw_value.count(',') == 1 and raw_value.count('.') > 1:
            raw_value = raw_value.replace('.', '')
        raw_value = raw_value.replace(',', '.')
        raw_date = (row.get('date') or '').strip()
        raw_type = (row.get('type') or '').lower()
        cat_hint = row.get('category') or ''
        if not desc or not raw_value:
            continue
        try:
            value_dec = Decimal(raw_value)
        except Exception:
            continue
        # tipo: entrada se valor >=0 ou se texto indicar
        tx_type = 'income'
        if raw_type in ('expense', 'saida', 'saída', 'despesa'):
            tx_type = 'expense'
        elif raw_type in ('income', 'entrada', 'receita'):
            tx_type = 'income'
        elif value_dec < 0:
            tx_type = 'expense'
            value_dec = abs(value_dec)

        # data
        date_obj = None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y'):
            try:
                date_obj = datetime.datetime.strptime(raw_date, fmt).date()
                break
            except Exception:
                continue
        if date_obj is None:
            date_obj = timezone.now().date()

        category = None
        source = ''
        reason = ''
        if cat_hint:
            for cat in categories:
                if cat.name.strip().lower() == cat_hint.strip().lower():
                    category = cat
                    source = 'csv'
                    reason = 'Categoria informada no arquivo'
                    break

        if not category:
            category, source, reason = _suggest_category_for_desc(desc, cat_hint, categories, user=request.user, workspace=workspace)
        if source == 'ai':
            ai_used = True
        preview.append({
            'description': desc,
            'date': date_obj.strftime('%Y-%m-%d'),
            'value': f"{value_dec:.2f}",
            'type': tx_type,
            'category': category.id if category else '',
            'category_name': category.name if category else 'Sem categoria',
            'category_source': source,
            'category_reason': reason,
        })
    return preview, ai_used


def _decode_upload(file_bytes: bytes) -> str:
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode('utf-8', errors='ignore')


def _ensure_categories_for_rows(rows, categories, workspace):
    if not rows:
        return categories
    existing = {c.name.strip().lower(): c for c in categories}
    created = False
    for row in rows:
        name = (row.get('category') or '').strip()
        if not name:
            continue
        key = name.lower()
        if key in existing:
            continue
        cat, _ = Category.objects.get_or_create(name=name, workspace=workspace)
        existing[key] = cat
        created = True
    if created:
        categories = list(Category.objects.filter(workspace=workspace) if workspace else Category.objects.all())
    return categories


def _match_category(name_hint: str, description: str, categories):
    hint = (name_hint or '').strip().lower()
    desc = (description or '').lower()
    if not categories:
        return None
    # match por nome exato
    for cat in categories:
        if hint and cat.name.lower() == hint:
            return cat
    # match por substring da descrição
    for cat in categories:
        if cat.name.lower() in desc:
            return cat
    # fallback para primeira
    return categories[0]


def _suggest_category_for_desc(desc: str, cat_hint: str, categories, user=None, workspace=None):
    """
    Sugere categoria com heuristica + IA (se OPENAI_API_KEY estiver configurada).
    Retorna (categoria, fonte, motivo) onde fonte e 'ai' ou 'heuristic'.
    """
    if not categories:
        return None, 'heuristic', 'Sem categorias dispon\u00edveis'
    # heuristica por hint
    hint = (cat_hint or '').strip().lower()
    if hint:
        for cat in categories:
            cname = cat.name.lower()
            if hint == cname or hint in cname:
                return cat, 'heuristic', 'Correspond\u00eancia pelo nome informado'

    # heuristica por substring no texto
    desc_lower = (desc or '').lower()
    for cat in categories:
        if cat.name.lower() in desc_lower:
            return cat, 'heuristic', 'Nome da categoria presente na descri\u00e7\u00e3o'

    # heuristica por intersecao de tokens
    tokens = set(re.findall(r'\w+', desc_lower))
    best = None
    best_score = 0
    for cat in categories:
        c_tokens = set(re.findall(r'\w+', cat.name.lower()))
        score = len(tokens & c_tokens)
        if score > best_score:
            best_score = score
            best = cat
    if best:
        return best, 'heuristic', 'Maior interse\u00e7\u00e3o de palavras com a categoria'

    # IA opcional
    if os.getenv("OPENAI_API_KEY"):
        if user:
            quota = get_ai_quota(user)
            if not quota.get('allowed', True):
                return categories[0], 'heuristic', 'Limite de IA atingido para o plano'
        names = [cat.name for cat in categories]
        sys_prompt = (
            "Voc\u00ea \u00e9 um classificador de categorias. Escolha uma das categorias existentes para a descri\u00e7\u00e3o fornecida. "
            "Responda no formato 'CATEGORIA|motivo breve'. Use apenas uma das categorias listadas. "
            "Se n\u00e3o souber, responda 'Sem categoria|motivo'."
        )
        user_prompt = f"Categorias: {', '.join(names)}. Descri\u00e7\u00e3o: {desc}"
        ai_choice, usage, model_name = llm_complete(sys_prompt, user_prompt)
        if ai_choice:
            _record_ai_usage(user, workspace, 'import', usage or {}, model_name)
            parts = ai_choice.split('|', 1)
            choice_raw = parts[0].strip().lower()
            reason = parts[1].strip() if len(parts) > 1 else 'Sugest\u00e3o via IA'
            for cat in categories:
                if cat.name.lower() == choice_raw:
                    return cat, 'ai', reason
                if choice_raw in cat.name.lower():
                    return cat, 'ai', reason

    return categories[0], 'heuristic', 'Categoria padr\u00e3o'



@login_required
def transaction_import(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover finan\u00e7as deste workspace.')
        return _finance_access_denied_redirect(request)

    categories = list(Category.objects.filter(workspace=workspace) if workspace else Category.objects.all())
    if not categories:
        default_cat, _ = Category.objects.get_or_create(name='Sem categoria', workspace=workspace)
        categories = [default_cat]
    upload_form = StatementUploadForm()
    preview_rows = []
    parse_error = None
    ai_used = False

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'confirm':
            desc_list = request.POST.getlist('description')
            date_list = request.POST.getlist('date')
            value_list = request.POST.getlist('value')
            type_list = request.POST.getlist('type')
            category_list = request.POST.getlist('category')
            selected_list = request.POST.getlist('selected')
            skip_list = set(request.POST.getlist('skip'))

            to_create = []
            default_responsible = workspace.owner if workspace else None
            for idx, desc in enumerate(desc_list):
                if str(idx) in skip_list:
                    continue
                desc = (desc or '').strip()
                if not desc:
                    continue
                try:
                    date_obj = datetime.date.fromisoformat(date_list[idx])
                except Exception:
                    continue
                try:
                    value_dec = Decimal(str(value_list[idx]).replace(',', '.'))
                except Exception:
                    continue
                tx_type = 'income' if type_list[idx] == 'income' else 'expense'
                try:
                    cat_id = int(category_list[idx])
                    category = next((c for c in categories if c.id == cat_id), None)
                except Exception:
                    category = categories[0] if categories else None
                selected = str(idx) in selected_list
                to_create.append(Transaction(
                    description=desc,
                    date=date_obj,
                    value=value_dec,
                    type=tx_type,
                    category=category,
                    workspace=workspace,
                    responsible=default_responsible,
                    selected=selected,
                ))
            if to_create:
                Transaction.objects.bulk_create(to_create, batch_size=500)
                _invalidate_workspace_caches(workspace)
                if workspace:
                    notify_balance_threshold(workspace.owner, workspace)
                    check_category_budgets(workspace)
                    check_balance_goals(workspace)
                messages.success(request, f'{len(to_create)} transações importadas com sucesso.')
            else:
                messages.warning(request, 'Nenhuma transação válida para importar.')
            return redirect('tracker:transactions_list')

        upload_form = StatementUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            up_file = upload_form.cleaned_data['file']
            name = (up_file.name or '').lower()
            rows = []
            if name.endswith('.pdf'):
                file_bytes = up_file.read()
                rows, parse_error = _parse_pdf_statement(file_bytes)
            else:
                try:
                    rows = _parse_statement_rows_file(up_file.file)
                except Exception:
                    file_bytes = up_file.read()
                    raw = _decode_upload(file_bytes)
                    rows = _parse_statement_rows(raw, categories)
            categories = _ensure_categories_for_rows(rows, categories, workspace)
            preview_rows, ai_used = _build_preview(rows, categories)
            if not preview_rows:
                messages.warning(request, 'Nenhuma linha válida encontrada no arquivo.')

    return render(request, 'tracker/transactions_import.html', {
        'upload_form': upload_form,
        'preview_rows': preview_rows,
        'categories': categories,
        'parse_error': parse_error,
        'mode': 'csv',
        'ai_used': ai_used,
    })


# -------- Tasks --------

@login_required
def tasks_list(request):
    workspace = getattr(request, "workspace", None)
    tasks_qs = _apply_workspace_filter(
        Task.objects.select_related('responsible_user', 'task_category').prefetch_related('steps').all(),
        workspace,
        request.user,
    )
    status = request.GET.get('status', '')
    search = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    start_raw = request.GET.get('start', '')
    end_raw = request.GET.get('end', '')
    start = _parse_date_input(start_raw)
    end = _parse_date_input(end_raw)

    task_category_qs = TaskCategory.objects.all()
    if workspace:
        task_category_qs = task_category_qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        task_category_qs = task_category_qs.none()
    task_category_names = list(task_category_qs.values_list('name', flat=True))
    custom_category_names = list(
        tasks_qs.exclude(category='').values_list('category', flat=True).distinct()
    )
    task_categories = []
    for name in task_category_names + custom_category_names:
        if name and name not in task_categories:
            task_categories.append(name)

    if status in ('ongoing', 'done'):
        tasks_qs = tasks_qs.filter(status=status)
    if search:
        tasks_qs = tasks_qs.filter(title__icontains=search)
    if category:
        tasks_qs = tasks_qs.filter(category__iexact=category)
    if start:
        tasks_qs = tasks_qs.filter(due_date__gte=start)
    if end:
        tasks_qs = tasks_qs.filter(due_date__lte=end)

    tasks = tasks_qs.order_by('due_date', '-created_at')

    if request.GET.get('export') == 'csv':
        return _export_tasks_csv(tasks)

    paginator = Paginator(tasks, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    for task in page_obj:
        completed_late = False
        if task.status == 'done' and task.due_date:
            completed_at = task.completed_at or task.updated_at
            completed_date = timezone.localdate(completed_at) if completed_at else None
            if completed_date and completed_date > task.due_date:
                completed_late = True
        task.completed_late = completed_late
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    today = timezone.now().date()
    soon_threshold = today + datetime.timedelta(days=3)

    def fmt(val):
        try:
            return datetime.date.fromisoformat(val).strftime('%d/%m/%Y')
        except Exception:
            return val or ''

    status_label = {'ongoing': 'Em andamento', 'done': 'Finalizada'}.get(status, '')
    filter_chips = _build_filter_chips(
        request,
        [
            ('q', 'Busca', search),
            ('status', 'Status', status_label),
            ('category', 'Categoria', category),
            ('start', 'De', fmt(start_raw)),
            ('end', 'Até', fmt(end_raw)),
        ],
    )

    context = {
        'tasks_page': page_obj,
        'tasks_total': paginator.count,
        'open_count': tasks_qs.filter(status='ongoing').count(),
        'done_count': tasks_qs.filter(status='done').count(),
        'today': today,
        'soon_threshold': soon_threshold,
        'bulk_form': TaskBulkUpdateForm(workspace=workspace),
        'task_categories': task_categories,
        'query_string': query_string,
        'filters': {
            'status': status,
            'q': search,
            'category': category,
            'start': start.isoformat() if start else '',
            'end': end.isoformat() if end else '',
        },
        'filters_display': {
            'start': fmt(start_raw),
            'end': fmt(end_raw),
        },
        'filter_chips': filter_chips,
    }
    return render(request, 'tracker/tasks_list.html', context)


@login_required
def tasks_bulk_update(request):
    if request.method != 'POST':
        return redirect('tracker:tasks_list')
    workspace = getattr(request, "workspace", None)
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para editar tarefas neste workspace.')
            return redirect('tracker:tasks_list')
    select_all = request.POST.get('select_all') == '1'
    ids_raw = (request.POST.get('ids') or '').strip()
    ids = [int(val) for val in ids_raw.split(',') if val.isdigit()]
    if not select_all and not ids:
        messages.warning(request, 'Selecione ao menos uma tarefa.')
        return redirect('tracker:tasks_list')
    form = TaskBulkUpdateForm(request.POST, workspace=workspace)
    if not form.is_valid():
        messages.error(request, 'Não foi possível aplicar as alterações.')
        return redirect('tracker:tasks_list')
    if select_all:
        qs = _tasks_queryset(request, workspace)
        search = (request.POST.get('q') or '').strip()
        status = request.POST.get('status') or ''
        category = (request.POST.get('category') or '').strip()
        start = request.POST.get('start') or ''
        end = request.POST.get('end') or ''
        start_date = _parse_date_input(start)
        end_date = _parse_date_input(end)
        if search:
            qs = qs.filter(title__icontains=search)
        if status in ('ongoing', 'done'):
            qs = qs.filter(status=status)
        if category:
            qs = qs.filter(category__iexact=category)
        if start_date:
            qs = qs.filter(due_date__gte=start_date)
        if end_date:
            qs = qs.filter(due_date__lte=end_date)
    else:
        qs = Task.objects.filter(id__in=ids)
        if workspace:
            qs = qs.filter(workspace=workspace)
        elif not request.user.is_superuser:
            qs = qs.none()
    updates = {}
    responsible_user = form.cleaned_data.get('responsible_user')
    task_category = form.cleaned_data.get('task_category')
    category = (form.cleaned_data.get('category') or '').strip()
    due_date = form.cleaned_data.get('due_date')
    status = form.cleaned_data.get('status')
    icon = form.cleaned_data.get('icon')
    selected_action = form.cleaned_data.get('selected_action')
    if request.POST.get('bulk_delete') == '1':
        deleted_count = qs.count()
        snapshot_limit = 200
        payload = []
        if deleted_count:
            for item in list(qs.values(
                'title',
                'due_date',
                'status',
                'progress',
                'selected',
                'responsible_user_id',
                'responsible',
                'responsible_email',
                'workspace_id',
                'task_category_id',
                'category',
                'icon',
            )[:snapshot_limit]):
                payload.append({
                    'title': item.get('title', ''),
                    'due_date': item.get('due_date').isoformat() if item.get('due_date') else '',
                    'status': item.get('status', 'ongoing'),
                    'progress': str(item.get('progress')) if item.get('progress') is not None else '0',
                    'selected': bool(item.get('selected')),
                    'responsible_user_id': item.get('responsible_user_id'),
                    'responsible': item.get('responsible', ''),
                    'responsible_email': item.get('responsible_email', ''),
                    'workspace_id': item.get('workspace_id'),
                    'task_category_id': item.get('task_category_id'),
                    'category': item.get('category', ''),
                    'icon': item.get('icon') or '',
                })
        if payload:
            request.session['undo_task_bulk'] = payload
            request.session['undo_task_bulk_expires'] = (timezone.now() + datetime.timedelta(minutes=10)).isoformat()
        qs.delete()
        _invalidate_workspace_caches(workspace)
        messages.success(request, f'{deleted_count} tarefas exclu\u00eddas. Voc\u00ea pode desfazer a a\u00e7\u00e3o.')
        return redirect('tracker:tasks_list')
    if request.POST.get('bulk_mark') == '1':
        qs.update(selected=True)
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Tarefas destacadas.')
        return redirect('tracker:tasks_list')
    if request.POST.get('bulk_convert') == '1':
        now = timezone.now()
        qs.update(
            status=Case(
                When(status='ongoing', then=Value('done')),
                When(status='done', then=Value('ongoing')),
                default=Value('ongoing'),
                output_field=CharField(),
            ),
            completed_at=Case(
                When(status='ongoing', then=Value(now)),
                When(status='done', then=Value(None)),
                default=Value(None),
                output_field=DateTimeField(),
            ),
        )
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Tarefas convertidas.')
        return redirect('tracker:tasks_list')
    if responsible_user:
        updates['responsible_user'] = responsible_user
    if category:
        updates['category'] = category
        updates['task_category'] = None
    elif task_category:
        updates['task_category'] = task_category
        updates['category'] = task_category.name
    if icon:
        updates['icon'] = icon
    if due_date:
        updates['due_date'] = due_date
    if status:
        updates['status'] = status
        updates['completed_at'] = timezone.now() if status == 'done' else None
    if not updates:
        messages.warning(request, 'Nenhuma altera\u00e7\u00e3o escolhida.')
        return redirect('tracker:tasks_list')
    qs.update(**updates)
    _invalidate_workspace_caches(workspace)
    messages.success(request, 'Tarefas atualizadas.')
    return redirect('tracker:tasks_list')


@login_required
def task_create(request):
    workspace = getattr(request, "workspace", None)
    # Membros sem permissao de edicao nao podem criar tarefas
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não tem permissão para criar tarefas neste workspace.')
            return redirect('tracker:tasks_list')
    initial = {'due_date': timezone.now().date()}
    if workspace and workspace.owner_id:
        initial['responsible_user'] = workspace.owner_id
    form = TaskForm(request.POST or None, initial=initial, workspace=workspace)
    step_form = TaskStepForm()
    task_category_count = form.fields['task_category'].queryset.count() if 'task_category' in form.fields else 0
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if workspace:
            obj.workspace = workspace
        obj.save()
        step_titles = request.POST.getlist('step_title')
        step_responsibles = request.POST.getlist('step_responsible')
        step_emails = request.POST.getlist('step_responsible_email')
        step_responsible_custom = request.POST.getlist('step_responsible_custom')
        step_email_custom = request.POST.getlist('step_responsible_email_custom')
        order = 1
        for idx, title in enumerate(step_titles):
            step_title = (title or '').strip()
            if not step_title:
                continue
            responsible_value = step_responsibles[idx].strip() if idx < len(step_responsibles) and step_responsibles[idx] else ''
            if responsible_value == '__custom__':
                responsible_value = step_responsible_custom[idx].strip() if idx < len(step_responsible_custom) and step_responsible_custom[idx] else ''
            email_value = step_emails[idx].strip() if idx < len(step_emails) and step_emails[idx] else ''
            if email_value == '__custom__':
                email_value = step_email_custom[idx].strip() if idx < len(step_email_custom) and step_email_custom[idx] else ''
            step = TaskStep(
                task=obj,
                title=step_title,
                order=order,
                responsible=responsible_value,
                responsible_email=email_value,
            )
            step.save()
            order += 1
        if order > 1:
            _update_task_progress(obj)
        messages.success(request, 'Tarefa criada com sucesso.')
        if request.POST.get('save_new'):
            return redirect('tracker:task_create')
        return redirect('tracker:tasks_list')
    workspace_members = _get_workspace_members(workspace)
    member_names = [member['name'] for member in workspace_members]
    member_emails = [member['email'] for member in workspace_members if member.get('email')]
    context = {
        'form': form,
        'step_form': step_form,
        'is_edit': False,
        'steps': [],
        'task_category_count': task_category_count,
        'workspace_members': workspace_members,
        'workspace_member_names': member_names,
        'workspace_member_emails': member_emails,
    }
    return render(request, 'tracker/task_form.html', context)


@login_required
def task_update(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.prefetch_related('steps')
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    form = TaskForm(request.POST or None, instance=task, workspace=workspace)
    steps = task.steps.all()
    step_form = TaskStepForm()
    task_category_count = form.fields['task_category'].queryset.count() if 'task_category' in form.fields else 0
    prev_status = task.status
    if request.method == 'POST' and form.is_valid():
        if workspace and not request.user.is_superuser:
            membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
            if membership and not membership.can_edit_tasks:
                messages.error(request, 'Você não tem permissão para editar tarefas neste workspace.')
                return redirect('tracker:tasks_list')
        task = form.save(commit=False)
        if prev_status != 'done' and task.status == 'done':
            task.completed_at = timezone.now()
        elif prev_status == 'done' and task.status != 'done':
            task.completed_at = None
        task.save()
        _invalidate_workspace_caches(workspace)
        if prev_status != 'done' and task.status == 'done':
            notify_task_completed(task, actor=request.user)
        messages.success(request, 'Tarefa atualizada.')
        return redirect('tracker:tasks_list')
    workspace_members = _get_workspace_members(workspace)
    member_names = [member['name'] for member in workspace_members]
    member_emails = [member['email'] for member in workspace_members if member.get('email')]
    context = {
        'form': form,
        'is_edit': True,
        'object': task,
        'is_detail': request.GET.get('detail') == '1',
        'steps': steps,
        'step_form': step_form,
        'task_category_count': task_category_count,
        'workspace_members': workspace_members,
        'workspace_member_names': member_names,
        'workspace_member_emails': member_emails,
    }
    return render(request, 'tracker/task_form.html', context)


@login_required
def task_step_detail(request, pk, step_id):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.prefetch_related('steps')
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    step = get_object_or_404(task.steps, pk=step_id)
    return render(request, 'tracker/task_step_detail.html', {'task': task, 'step': step})


@login_required
def task_step_add(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não pode editar etapas neste workspace.')
            return redirect('tracker:task_update', pk=pk)
    post_data = request.POST.copy()
    if post_data.get('responsible') == '__custom__':
        post_data['responsible'] = (post_data.get('responsible_custom') or '').strip()
    if post_data.get('responsible_email') == '__custom__':
        post_data['responsible_email'] = (post_data.get('responsible_email_custom') or '').strip()
    form = TaskStepForm(post_data or None)
    if request.method == 'POST' and form.is_valid():
        step = form.save(commit=False)
        step.task = task
        current_max = task.steps.aggregate(m=Max('order'))['m'] or 0
        step.order = current_max + 1
        step.save()
        _update_task_progress(task)
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Etapa adicionada.')
    return redirect('tracker:task_update', pk=pk)


@login_required
def task_step_update(request, pk, step_id):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    step = get_object_or_404(task.steps, pk=step_id)
    prev_step_status = step.status
    was_done = task.status == 'done'
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Voc\u00ea n\u00e3o pode editar etapas neste workspace.')
            return redirect('tracker:task_update', pk=pk)
    if request.method == 'POST':
        post_data = request.POST.copy()
        if post_data.get('responsible') == '__custom__':
            post_data['responsible'] = (post_data.get('responsible_custom') or '').strip()
        if post_data.get('responsible_email') == '__custom__':
            post_data['responsible_email'] = (post_data.get('responsible_email_custom') or '').strip()
        form = TaskStepForm(post_data, instance=step)
        if form.is_valid():
            step = form.save()
            _update_task_progress(task)
            _invalidate_workspace_caches(workspace)
            if prev_step_status != 'done' and step.status == 'done':
                notify_step_completed(task, step, actor=request.user)
            if not was_done and task.status == 'done':
                notify_task_completed(task, actor=request.user)
            messages.success(request, 'Etapa atualizada.')
        else:
            messages.error(request, 'N\u00e3o foi poss\u00edvel atualizar a etapa.')
    return redirect('tracker:task_update', pk=pk)


@login_required
def task_step_toggle(request, pk, step_id):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    step = get_object_or_404(TaskStep, pk=step_id, task=task)
    was_done = task.status == 'done'
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não pode editar etapas neste workspace.')
            return redirect('tracker:task_update', pk=pk)
    if request.method != 'POST':
        messages.error(request, 'Requisi\u00e7\u00e3o inv\u00e1lida.')
        return redirect('tracker:task_update', pk=pk)

    if step.status == 'done':
        step.status = 'ongoing'
    elif step.status == 'cancelled':
        step.status = 'ongoing'
    else:
        step.status = 'done'
    step.save()
    _invalidate_workspace_caches(workspace)
    if step.status == 'done':
        notify_step_completed(task, step, actor=request.user)
    _update_task_progress(task)
    if not was_done and task.status == 'done':
        notify_task_completed(task, actor=request.user)
    return redirect('tracker:task_update', pk=pk)


@login_required
def task_step_delete(request, pk, step_id):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    step = get_object_or_404(TaskStep, pk=step_id, task=task)
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não pode editar etapas neste workspace.')
            return redirect('tracker:task_update', pk=pk)
    step.delete()
    _update_task_progress(task)
    _invalidate_workspace_caches(workspace)
    return redirect('tracker:task_update', pk=pk)


def _update_task_progress(task: Task):
    prev_status = task.status
    steps = task.steps.all()
    if steps.exists():
        total = steps.count()
        done = steps.filter(status='done').count()
        progress = (done / total) * 100
        task.progress = progress
        task.status = 'done' if done == total else 'ongoing'
    else:
        task.progress = 0
        task.status = 'done' if task.status == 'done' else 'ongoing'

    update_fields = ['progress', 'status', 'updated_at']
    if task.status == 'done' and prev_status != 'done':
        task.completed_at = timezone.now()
        update_fields.append('completed_at')
    elif task.status != 'done' and prev_status == 'done':
        task.completed_at = None
        update_fields.append('completed_at')

    task.save(update_fields=update_fields)


@login_required

@login_required
def task_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.prefetch_related('steps')
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        steps_payload = []
        for step in task.steps.all():
            steps_payload.append({
                'title': step.title,
                'order': step.order,
                'responsible': step.responsible,
                'responsible_email': step.responsible_email,
                'status': step.status,
                'completed_at': step.completed_at.isoformat() if step.completed_at else '',
            })
        request.session['undo_task'] = {
            'title': task.title,
            'category': task.category,
            'task_category_id': task.task_category_id,
            'due_date': task.due_date.isoformat(),
            'status': task.status,
            'selected': task.selected,
            'responsible_user_id': task.responsible_user_id,
            'responsible': task.responsible,
            'responsible_email': task.responsible_email,
            'progress': str(task.progress),
            'completed_at': task.completed_at.isoformat() if task.completed_at else '',
            'workspace_id': task.workspace_id,
            'steps': steps_payload,
        }
        request.session['undo_task_expires'] = (timezone.now() + datetime.timedelta(minutes=10)).isoformat()
        task.delete()
        _invalidate_workspace_caches(workspace)
        messages.success(request, 'Tarefa removida. Você pode desfazer a ação.')
    return redirect('tracker:tasks_list')


@login_required
def task_undo_delete(request):
    if request.method != 'POST':
        return redirect('tracker:tasks_list')
    payload = request.session.get('undo_task')
    expires = request.session.get('undo_task_expires')
    if not payload:
        messages.error(request, 'Nada para desfazer.')
        return redirect('tracker:tasks_list')
    if expires:
        try:
            expires_at = datetime.datetime.fromisoformat(expires)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
            if timezone.now() > expires_at:
                request.session.pop('undo_task', None)
                request.session.pop('undo_task_expires', None)
                messages.error(request, 'O tempo para desfazer expirou.')
                return redirect('tracker:tasks_list')
        except Exception:
            pass
    due_date_val = payload.get('due_date')
    completed_at_val = payload.get('completed_at') or None
    if due_date_val:
        try:
            due_date_val = datetime.date.fromisoformat(due_date_val)
        except Exception:
            pass
    if completed_at_val:
        try:
            completed_at_val = datetime.datetime.fromisoformat(completed_at_val)
            if timezone.is_naive(completed_at_val):
                completed_at_val = timezone.make_aware(completed_at_val)
        except Exception:
            completed_at_val = None

    task = Task(
        title=payload.get('title', ''),
        category=payload.get('category', ''),
        task_category_id=payload.get('task_category_id'),
        due_date=due_date_val,
        status=payload.get('status', 'ongoing'),
        selected=bool(payload.get('selected')),
        responsible_user_id=payload.get('responsible_user_id'),
        responsible=payload.get('responsible', ''),
        responsible_email=payload.get('responsible_email', ''),
        progress=payload.get('progress') or 0,
        completed_at=completed_at_val,
        workspace_id=payload.get('workspace_id'),
    )
    task.save()
    steps_payload = payload.get('steps') or []
    for step in steps_payload:
        step_completed = step.get('completed_at') or None
        if step_completed:
            try:
                step_completed = datetime.datetime.fromisoformat(step_completed)
                if timezone.is_naive(step_completed):
                    step_completed = timezone.make_aware(step_completed)
            except Exception:
                step_completed = None
        TaskStep.objects.create(
            task=task,
            title=step.get('title', ''),
            order=step.get('order') or 0,
            responsible=step.get('responsible', ''),
            responsible_email=step.get('responsible_email', ''),
            status=step.get('status', 'ongoing'),
            completed_at=step_completed,
        )
    _update_task_progress(task)
    _invalidate_workspace_caches(task.workspace)
    request.session.pop('undo_task', None)
    request.session.pop('undo_task_expires', None)
    messages.success(request, 'Tarefa restaurada.')
    return redirect('tracker:tasks_list')


@login_required
def task_undo_clear(request):
    if request.method == 'POST':
        request.session.pop('undo_task', None)
        request.session.pop('undo_task_expires', None)
    return JsonResponse({'ok': True})


@login_required
def task_bulk_undo_delete(request):
    if request.method != 'POST':
        return redirect('tracker:tasks_list')
    payload = request.session.get('undo_task_bulk')
    expires = request.session.get('undo_task_bulk_expires')
    if not payload:
        messages.error(request, 'Nada para desfazer.')
        return redirect('tracker:tasks_list')
    if expires:
        try:
            expires_at = datetime.datetime.fromisoformat(expires)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
            if timezone.now() > expires_at:
                request.session.pop('undo_task_bulk', None)
                request.session.pop('undo_task_bulk_expires', None)
                messages.error(request, 'O tempo para desfazer expirou.')
                return redirect('tracker:tasks_list')
        except Exception:
            pass
    restored = 0
    for item in payload:
        due_val = item.get('due_date')
        if due_val:
            try:
                due_val = datetime.date.fromisoformat(due_val)
            except Exception:
                due_val = None
        try:
            progress_val = Decimal(str(item.get('progress', '0')))
        except Exception:
            progress_val = Decimal('0')
        task_category_id = item.get('task_category_id')
        task_category = TaskCategory.objects.filter(id=task_category_id).first() if task_category_id else None
        Task.objects.create(
            title=item.get('title', ''),
            due_date=due_val or timezone.localdate(),
            status=item.get('status', 'ongoing'),
            progress=progress_val,
            selected=bool(item.get('selected')),
            responsible_user_id=item.get('responsible_user_id'),
            responsible=item.get('responsible', ''),
            responsible_email=item.get('responsible_email', ''),
            workspace_id=item.get('workspace_id'),
            task_category=task_category,
            category=item.get('category', ''),
            icon=item.get('icon') or '',
        )
        restored += 1
    request.session.pop('undo_task_bulk', None)
    request.session.pop('undo_task_bulk_expires', None)
    messages.success(request, f'{restored} tarefas restauradas.')
    return redirect('tracker:tasks_list')


@login_required
def task_bulk_undo_clear(request):
    if request.method == 'POST':
        request.session.pop('undo_task_bulk', None)
        request.session.pop('undo_task_bulk_expires', None)
    return JsonResponse({'ok': True})


def task_toggle_status(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.prefetch_related('steps')
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)

    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não pode editar tarefas neste workspace.')
            return redirect('tracker:tasks_list')

    if request.method == 'POST':
        was_done = task.status == 'done'
        mark_done = task.status != 'done'
        if mark_done:
            if task.steps.exists():
                task.steps.update(status='done', done=True)
                _update_task_progress(task)
            else:
                task.status = 'done'
                task.progress = 100
                task.completed_at = timezone.now()
                task.save(update_fields=['status', 'progress', 'completed_at', 'updated_at'])
            if not was_done:
                notify_task_completed(task, actor=request.user)
        else:
            if task.steps.exists():
                task.steps.update(status='ongoing', done=False)
                _update_task_progress(task)
            else:
                task.status = 'ongoing'
                task.progress = 0
                task.completed_at = None
                task.save(update_fields=['status', 'progress', 'completed_at', 'updated_at'])
        _invalidate_workspace_caches(workspace)
        if mark_done:
            messages.success(request, 'Tarefa concluída.')
        else:
            messages.success(request, 'Tarefa reaberta.')
    return redirect('tracker:tasks_list')


@login_required
def task_toggle_selected(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)

    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não pode editar tarefas neste workspace.')
            return redirect('tracker:tasks_list')

    if request.method == 'POST':
        task.selected = not task.selected
        task.save(update_fields=['selected', 'updated_at'])
        _invalidate_workspace_caches(workspace)
        if task.selected:
            messages.success(request, 'Tarefa destacada.')
        else:
            messages.success(request, 'Tarefa removida dos destaques.')
    return redirect('tracker:tasks_list')


# -------- Categories --------

@login_required
def categories_list(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para ver categorias.')
        return redirect('tracker:dashboard')
    qs = Category.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    categories = qs.annotate(total_transactions=Count('transactions'))
    task_qs = TaskCategory.objects.all()
    if workspace:
        task_qs = task_qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        task_qs = task_qs.none()
    task_categories = task_qs.annotate(total_tasks=Count('tasks'))
    scope = (request.POST.get('category_scope') or 'finance').strip()
    form = CategoryForm(request.POST or None)
    task_form = TaskCategoryForm(request.POST or None, prefix='task')
    if request.method == 'POST':
        if scope == 'task':
            if task_form.is_valid():
                obj = task_form.save(commit=False)
                if workspace:
                    obj.workspace = workspace
                obj.save()
                messages.success(request, 'Categoria de tarefa criada com sucesso.')
                return redirect('tracker:categories_list')
        else:
            if form.is_valid():
                obj = form.save(commit=False)
                if workspace:
                    obj.workspace = workspace
                obj.save()
                messages.success(request, 'Categoria criada com sucesso.')
                return redirect('tracker:categories_list')
    color_palette = [
        '#0f172a', '#1e3a8a', '#2563eb', '#0ea5e9', '#14b8a6', '#10b981',
        '#22c55e', '#84cc16', '#eab308', '#f59e0b', '#f97316', '#ef4444',
        '#dc2626', '#be123c', '#db2777', '#c026d3', '#9333ea', '#6366f1',
        '#64748b', '#475569', '#334155', '#1f2937', '#111827', '#7c3aed',
        '#8b5cf6', '#a855f7', '#f472b6', '#fb7185', '#f43f5e', '#06b6d4',
    ]
    return render(
        request,
        'tracker/categories_list.html',
        {
            'categories': categories,
            'task_categories': task_categories,
            'form': form,
            'task_form': task_form,
            'color_palette': color_palette,
        },
    )


@login_required
def category_edit(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para ver categorias.')
        return redirect('tracker:dashboard')
    qs = Category.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    category = get_object_or_404(qs, pk=pk)
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Categoria atualizada.')
        return redirect('tracker:categories_list')
    color_palette = [
        '#0f172a', '#1e3a8a', '#2563eb', '#0ea5e9', '#14b8a6', '#10b981',
        '#22c55e', '#84cc16', '#eab308', '#f59e0b', '#f97316', '#ef4444',
        '#dc2626', '#be123c', '#db2777', '#c026d3', '#9333ea', '#6366f1',
        '#64748b', '#475569', '#334155', '#1f2937', '#111827', '#7c3aed',
        '#8b5cf6', '#a855f7', '#f472b6', '#fb7185', '#f43f5e', '#06b6d4',
    ]
    return render(
        request,
        'tracker/category_form.html',
        {'form': form, 'category': category, 'color_palette': color_palette},
    )


@login_required
def category_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para ver categorias.')
        return redirect('tracker:dashboard')
    qs = Category.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    category = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        try:
            category.delete()
            messages.success(request, 'Categoria removida.')
        except ProtectedError:
            messages.error(request, 'N\u00e3o \u00e9 poss\u00edvel remover: h\u00e1 transa\u00e7\u00f5es vinculadas.')
    return redirect('tracker:categories_list')


@login_required
def task_category_edit(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para ver categorias.')
        return redirect('tracker:dashboard')
    qs = TaskCategory.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    category = get_object_or_404(qs, pk=pk)
    form = TaskCategoryForm(request.POST or None, instance=category)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Categoria de tarefa atualizada.')
        return redirect('tracker:categories_list')
    color_palette = [
        '#0f172a', '#1e3a8a', '#2563eb', '#0ea5e9', '#14b8a6', '#10b981',
        '#22c55e', '#84cc16', '#eab308', '#f59e0b', '#f97316', '#ef4444',
        '#dc2626', '#be123c', '#db2777', '#c026d3', '#9333ea', '#6366f1',
        '#64748b', '#475569', '#334155', '#1f2937', '#111827', '#7c3aed',
        '#8b5cf6', '#a855f7', '#f472b6', '#fb7185', '#f43f5e', '#06b6d4',
    ]
    return render(
        request,
        'tracker/category_form.html',
        {
            'form': form,
            'category': category,
            'color_palette': color_palette,
            'category_label': 'Tarefas',
            'category_title': 'Editar categoria de tarefa',
        },
    )


@login_required
def task_category_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para ver categorias.')
        return redirect('tracker:dashboard')
    qs = TaskCategory.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    category = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        category.delete()
        messages.success(request, 'Categoria de tarefa removida.')
    return redirect('tracker:categories_list')


# -------- Chart API --------

@login_required
@cache_control(private=True, max_age=30)
def chart_data(request):
    today = timezone.now().date()
    start_param = request.GET.get('start')
    end_param = request.GET.get('end')
    period = request.GET.get('period') or ''
    type_filter = request.GET.get('type') or ''
    selected_only = request.GET.get('selected_only') == '1'
    category_id = request.GET.get('category_id') or ''
    responsible_id = request.GET.get('responsible') or ''
    workspace = getattr(request, "workspace", None)
    active_category_color = ""
    chart_ttl = int(getattr(settings, "CHART_CACHE_TTL", 300))

    def parse_date(val, fallback):
        try:
            return datetime.date.fromisoformat(val) if val else fallback
        except ValueError:
            return fallback

    def shift_months(date_obj: datetime.date, months: int) -> datetime.date:
        year = date_obj.year + ((date_obj.month - 1 + months) // 12)
        month = (date_obj.month - 1 + months) % 12 + 1
        days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        day = min(date_obj.day, days_in_month)
        return datetime.date(year, month, day)

    reference_date = today

    def range_from_period(p: str):
        if p == 'week':
            return reference_date - datetime.timedelta(days=7), reference_date
        if p == 'month':
            return shift_months(reference_date, -1), reference_date
        if p == 'quarter':
            return shift_months(reference_date, -3), reference_date
        if p == 'semester':
            return shift_months(reference_date, -6), reference_date
        if p == 'year':
            return shift_months(reference_date, -12), reference_date
        if p == 'all':
            scoped = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user)
            first_tx = scoped.order_by('date').first()
            last_tx = scoped.order_by('-date').first()
            if first_tx and last_tx:
                return first_tx.date, last_tx.date
        return reference_date.replace(day=1), reference_date

    start_date = parse_date(start_param, None)
    end_date = parse_date(end_param, None)
    if not start_date or not end_date:
        start_date, end_date = range_from_period(period)
    if not start_date or not end_date:
        start_date, end_date = range_from_period('month')

    cache_key = None
    if start_date and end_date:
        cache_key = (
            f"chart:{request.user.id}:{workspace.id if workspace else 'global'}:"
            f"{start_date.isoformat()}:{end_date.isoformat()}:{period}:"
            f"{type_filter}:{selected_only}:{category_id}:{responsible_id}"
        )
        cached_payload = cache.get(cache_key)
        if cached_payload:
            return JsonResponse(cached_payload)

    can_finance = _user_can_view_finance(request, workspace)

    qs_all = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user) if can_finance else Transaction.objects.none()
    if responsible_id.isdigit():
        qs_all = qs_all.filter(responsible_id=int(responsible_id))
    if category_id.isdigit():
        category_obj = Category.objects.filter(id=int(category_id)).first()
        if category_obj and category_obj.color:
            active_category_color = category_obj.color
    qs = qs_all.filter(date__gte=start_date, date__lte=end_date)
    if type_filter in ('income', 'expense'):
        qs = qs.filter(type=type_filter)
    if selected_only:
        qs = qs.filter(selected=True)
    if category_id.isdigit():
        qs = qs.filter(category_id=int(category_id))

    income_total = qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense_total = qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0

    category_net = qs.values('category_id', 'category__name').annotate(
        total=Sum(
            Case(
                When(type='income', then=F('value')),
                When(type='expense', then=F('value') * -1),
                default=0,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
    ).order_by('-total')

    labels = [item['category__name'] for item in category_net]
    values = [float(item['total'] or 0) for item in category_net]
    category_ids = [item['category_id'] for item in category_net]
    fallback_colors = ['#10b981','#3b82f6','#ef4444','#f59e0b','#8b5cf6','#14b8a6','#ec4899','#6366f1','#0ea5e9','#22c55e']
    color_map = {}
    if qs.exists():
        cat_qs = Category.objects.filter(id__in=qs.values_list('category_id', flat=True).distinct())
        for cat in cat_qs:
            if cat.color:
                color_map[cat.id] = cat.color
    colors = []
    for idx, cat_id in enumerate(category_ids):
        colors.append(color_map.get(cat_id, fallback_colors[idx % len(fallback_colors)]))
    if category_id.isdigit() and not active_category_color and colors:
        active_category_color = colors[0]

    def build_pie(qs_subset):
        data = (
            qs_subset.values('category_id', 'category__name')
            .annotate(total=Sum('value'))
            .order_by('-total')
        )
        pie_labels = [item['category__name'] for item in data]
        pie_values = [float(item['total'] or 0) for item in data]
        pie_ids = [item['category_id'] for item in data]
        pie_colors = []
        for idx, cat_id in enumerate(pie_ids):
            pie_colors.append(color_map.get(cat_id, fallback_colors[idx % len(fallback_colors)]))
        return pie_labels, pie_values, pie_colors, pie_ids

    income_category_labels, income_category_values, income_category_colors, income_category_ids = build_pie(qs.filter(type='income'))
    expense_category_labels, expense_category_values, expense_category_colors, expense_category_ids = build_pie(qs.filter(type='expense'))

    monthly = (
        qs.annotate(month=F('date__month'), year=F('date__year'))
        .values('year', 'month')
        .annotate(
            net=Sum(
                Case(
                    When(type='income', then=F('value')),
                    When(type='expense', then=F('value') * -1),
                    default=0,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )
        .order_by('year', 'month')
    )
    monthly_labels = [
        datetime.date(item['year'], item['month'], 1).strftime('%m/%Y')
        for item in monthly
    ]
    monthly_values = [float(item['net'] or 0) for item in monthly]

    top_income = (
        qs.filter(type='income')
        .values('category__name')
        .annotate(total=Sum('value'))
        .order_by('-total')[:5]
    )
    top_expense = (
        qs.filter(type='expense')
        .values('category__name')
        .annotate(total=Sum('value'))
        .order_by('-total')[:5]
    )

    daily = (
        qs.values('date')
        .annotate(
            net=Sum(
                Case(
                    When(type='income', then=F('value')),
                    When(type='expense', then=F('value') * -1),
                    default=0,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )
        .order_by('date')
    )
    daily_map = {item['date']: float(item['net'] or 0) for item in daily}
    daily_labels = [item['date'].strftime('%d/%m') for item in daily]
    daily_values = [float(item['net'] or 0) for item in daily]

    running_labels = []
    running_values = []
    running_total = 0
    cursor = start_date
    while cursor <= end_date:
        running_total += daily_map.get(cursor, 0.0)
        running_labels.append(cursor.strftime('%d/%m'))
        running_values.append(running_total)
        cursor += datetime.timedelta(days=1)

    payload = {
        'labels': labels,
        'values': values,
        'colors': colors,
        'category_ids': category_ids,
        'active_category_color': active_category_color,
        'income_total': float(income_total),
        'expense_total': float(expense_total),
        'balance_total': float(income_total - expense_total),
        'start': start_date.isoformat(),
        'end': end_date.isoformat(),
        'monthly_labels': monthly_labels,
        'monthly_values': monthly_values,
        'top_income': [{'label': item['category__name'], 'total': float(item['total'] or 0)} for item in top_income],
        'top_expense': [{'label': item['category__name'], 'total': float(item['total'] or 0)} for item in top_expense],
        'daily_labels': daily_labels,
        'daily_values': daily_values,
        'running_labels': running_labels,
        'running_values': running_values,
        'income_category_labels': income_category_labels,
        'income_category_values': income_category_values,
        'income_category_colors': income_category_colors,
        'income_category_ids': income_category_ids,
        'expense_category_labels': expense_category_labels,
        'expense_category_values': expense_category_values,
        'expense_category_colors': expense_category_colors,
        'expense_category_ids': expense_category_ids,
    }
    if cache_key:
        cache.set(cache_key, payload, chart_ttl)
    return JsonResponse(payload)


# -------- CSV exports --------

def _export_transactions_csv(queryset):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="transacoes.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['descricao', 'categoria', 'responsavel', 'data', 'tipo', 'valor'])
    for tx in queryset:
        responsible_name = ''
        if tx.responsible:
            responsible_name = tx.responsible.get_full_name() or tx.responsible.username
        writer.writerow([
            tx.description,
            tx.category.name,
            responsible_name,
            tx.date.isoformat(),
            tx.get_type_display(),
            f'{tx.value:.2f}',
        ])
    return response


def _export_tasks_csv(queryset):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="tarefas.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['titulo', 'categoria', 'responsavel', 'prazo', 'status'])
    for task in queryset:
        responsible_name = ''
        if task.responsible_user:
            responsible_name = task.responsible_user.get_full_name() or task.responsible_user.username
        elif task.responsible:
            responsible_name = task.responsible
        task_category_label = task.task_category.name if task.task_category else task.category
        writer.writerow([
            task.title,
            task_category_label,
            responsible_name,
            task.due_date.isoformat(),
            task.get_status_display(),
        ])
    return response


# -------- Auth & workspaces --------


def login_view(request):
    if request.user.is_authenticated:
        return redirect('tracker:dashboard')
    form = LoginForm(request, data=request.POST or None)
    if request.method == 'POST':
        if _is_login_blocked(request):
            messages.error(request, 'Muitas tentativas. Aguarde alguns minutos e tente novamente.')
            record_metric('login_blocked', metadata={'ip': request.META.get('REMOTE_ADDR')})
            return render(request, 'tracker/auth_login.html', {'form': form})
        if form.is_valid():
            user = form.get_user()
            profile = getattr(user, 'profile', None)
            if not profile:
                profile, _ = UserProfile.objects.get_or_create(user=user)
            if getattr(settings, 'EMAIL_CONFIRMATION_REQUIRED', False) and not user.is_superuser:
                if not (profile and profile.email_verified):
                    messages.error(request, 'Confirme seu e-mail antes de entrar.')
                    _register_login_failure(request)
                    record_metric('login_failed', user=user, metadata={'reason': 'email_not_verified'})
                    return render(request, 'tracker/auth_login.html', {'form': form})
            login(request, user)
            _clear_login_failures(request)
            record_metric('login_success', user=user, metadata={'ip': request.META.get('REMOTE_ADDR')})
            if not user.is_superuser and not profile.is_guest and not profile.payment_confirmed:
                if profile and profile.trial_active():
                    today = timezone.localdate()
                    remaining = (profile.trial_expires_at.date() - today).days if profile.trial_expires_at else 0
                    remaining = max(remaining, 0)
                    messages.info(request, f'Per\u00edodo de teste ativo. Restam {remaining} dia(s).')
                else:
                    messages.info(request, 'Finalize a assinatura para liberar o acesso ao sistema.')
                    return redirect('payments:subscription_start')
            if profile and profile.subscription_expires:
                today = timezone.localdate()
                grace_days = int(getattr(settings, 'SUBSCRIPTION_GRACE_DAYS', 7))
                grace_until = profile.subscription_expires + datetime.timedelta(days=grace_days)
                if today > profile.subscription_expires and today <= grace_until:
                    remaining = (grace_until - today).days
                    messages.warning(request, f'Assinatura expirada. Voc\u00ea tem {remaining} dia(s) de car\u00eancia.')
            messages.success(request, 'Bem-vindo(a) de volta!')
            return redirect(request.GET.get('next') or 'tracker:workspace_select')
        _register_login_failure(request)
        record_metric('login_failed', metadata={'ip': request.META.get('REMOTE_ADDR')})
    return render(request, 'tracker/auth_login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, 'Sessão encerrada.')
    return redirect('tracker:login')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('tracker:dashboard')
    form = SignupForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        trial_days = int(getattr(settings, 'TRIAL_DAYS', 7))
        invite_code = (form.cleaned_data.get('invite_code') or '').strip()
        plan_choice = form.cleaned_data.get('plan_choice') or 'monthly:essential'
        billing_cycle, plan_tier = plan_choice.split(':', 1)
        master_guest_limit = form.cleaned_data.get('master_guest_limit')
        invite = None
        if invite_code:
            invite = SubscriptionInvite.objects.filter(code__iexact=invite_code).first()
            if not invite or not invite.can_use():
                form.add_error('invite_code', 'Convite inv\u00e1lido ou expirado.')
                return render(request, 'tracker/auth_register.html', {'form': form})
        with db_transaction.atomic():
            user = form.save()
            ws_name = form.cleaned_data.get('workspace_name') or f"Workspace de {user.username}"
            slug = _ensure_unique_slug(ws_name)
            requires_manual = getattr(settings, 'REQUIRE_MANUAL_APPROVAL', True)
            trial_active = bool(trial_days and not invite)
            ws_active = bool(invite) or trial_active
            ws = Workspace.objects.create(name=ws_name, slug=slug, owner=user, is_active=ws_active)
            WorkspaceMembership.objects.create(workspace=ws, user=user, role='owner')
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.plan = plan_tier
            profile.billing_cycle = billing_cycle
            if plan_tier == 'master' and master_guest_limit:
                profile.master_guest_limit = master_guest_limit
            profile.save(update_fields=['plan', 'billing_cycle', 'master_guest_limit'])
            if invite:
                today = timezone.localdate()
                if invite.plan_cycle == 'annual':
                    expires = today + datetime.timedelta(days=365)
                else:
                    expires = today + datetime.timedelta(days=30)
                profile.billing_cycle = invite.plan_cycle
                profile.plan = invite.plan_tier
                if invite.plan_tier == 'master' and master_guest_limit:
                    profile.master_guest_limit = master_guest_limit
                profile.payment_confirmed = True
                profile.payment_confirmed_at = timezone.now()
                profile.subscription_expires = expires
                profile.is_approved = True
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
                SubscriptionInviteUse.objects.create(invite=invite, user=user)
                SubscriptionInvite.objects.filter(pk=invite.id).update(used_count=F('used_count') + 1)
                record_metric('payment_confirmed', user=user, workspace=ws, metadata={'source': 'invite'})
            if trial_active:
                now = timezone.now()
                profile.trial_started_at = now
                profile.trial_expires_at = now + datetime.timedelta(days=trial_days)
                profile.is_approved = True
                profile.approved_at = timezone.now()
                profile.approved_by = None
                profile.save(update_fields=['trial_started_at', 'trial_expires_at', 'is_approved', 'approved_at', 'approved_by'])
            elif not invite and not requires_manual:
                profile.is_approved = True
                profile.approved_at = timezone.now()
                profile.approved_by = None
                profile.save(update_fields=['is_approved', 'approved_at', 'approved_by'])

            if getattr(settings, 'EMAIL_CONFIRMATION_REQUIRED', False):
                profile.email_verified = False
                profile.email_verified_at = None
                profile.save(update_fields=['email_verified', 'email_verified_at'])
                _send_email_verification(request, user)
            else:
                profile.email_verified = True
                profile.email_verified_at = timezone.now()
                profile.save(update_fields=['email_verified', 'email_verified_at'])
            record_metric('signup', user=user, workspace=ws, metadata={'invite': bool(invite)})
        if invite:
            messages.success(request, 'Conta criada com convite. Voc\u00ea j\u00e1 pode acessar o sistema.')
        else:
            if trial_active:
                login(request, user)
                messages.success(request, f'Conta criada. Voc\u00ea tem {trial_days} dias de teste.')
                return redirect('tracker:workspace_select')
            if not requires_manual:
                login(request, user)
                messages.success(request, 'Conta criada. Conclua a assinatura para ativar todos os recursos.')
                cycle = getattr(profile, 'billing_cycle', 'monthly')
                plan = getattr(profile, 'plan', 'essential')
                return redirect(f"{reverse('payments:subscription_start')}?plan={cycle}:{plan}")
            notify_new_account(user)
            messages.success(request, 'Cadastro enviado. Aguarde aprova\u00e7\u00e3o do administrador.')
        return redirect('tracker:login')
    return render(request, 'tracker/auth_register.html', {'form': form})


def register_guest_view(request):
    if request.user.is_authenticated:
        return redirect('tracker:dashboard')
    form = GuestSignupForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        with db_transaction.atomic():
            user = form.save()
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.is_guest = True
            profile.is_approved = True
            profile.approved_at = timezone.now()
            profile.approved_by = None
            profile.email_verified = True
            profile.email_verified_at = timezone.now()
            profile.save(update_fields=['is_guest', 'is_approved', 'approved_at', 'approved_by', 'email_verified', 'email_verified_at'])
        record_metric('signup', user=user, metadata={'guest': True})
        login(request, user)
        messages.success(request, 'Conta de convidado criada. Pe\u00e7a acesso a um workspace.')
        return redirect('tracker:workspace_select')
    return render(request, 'tracker/auth_register_guest.html', {'form': form})


def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    success = False
    if user and default_token_generator.check_token(user, token):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.email_verified = True
        profile.email_verified_at = timezone.now()
        profile.save(update_fields=['email_verified', 'email_verified_at'])
        success = True

    return render(request, 'tracker/email_verified.html', {'success': success})


@login_required
def workspace_overview(request):
    profile = getattr(request.user, "profile", None)
    is_guest = bool(profile and profile.is_guest)
    current_workspace = getattr(request, "workspace", None)
    memberships = WorkspaceMembership.objects.select_related('workspace', 'workspace__owner').filter(
        user=request.user,
        workspace__is_active=True,
    ).order_by('workspace__name')
    owned_workspaces = Workspace.objects.filter(owner=request.user, is_active=True).order_by('name')
    pending_invites = WorkspaceInvite.objects.select_related('workspace', 'invited_by').filter(
        invited_user=request.user,
        status='pending',
    )
    all_workspaces = Workspace.objects.filter(is_active=True).order_by('name') if request.user.is_superuser else None

    return render(
        request,
        'tracker/workspace_overview.html',
        {
            'current_workspace': current_workspace,
            'memberships': memberships,
            'owned_workspaces': owned_workspaces,
            'pending_invites': pending_invites,
            'all_workspaces': all_workspaces,
            'is_guest': is_guest,
            'can_create': not is_guest,
        },
    )


@login_required
def workspace_select(request):
    profile = getattr(request.user, "profile", None)
    is_guest = bool(profile and profile.is_guest)
    memberships = WorkspaceMembership.objects.select_related('workspace').filter(user=request.user, workspace__is_active=True)
    pending_invites = WorkspaceInvite.objects.select_related('workspace', 'invited_by').filter(invited_user=request.user, status='pending')
    create_form = WorkspaceForm(prefix='create')
    slug_form = WorkspaceSlugForm(prefix='slug')
    accepted_workspace = None
    accepted_slug = request.session.pop('invite_flash_workspace', None)
    if accepted_slug:
        if request.user.is_superuser:
            accepted_workspace = Workspace.objects.filter(slug=accepted_slug, is_active=True).first()
        else:
            membership = memberships.filter(workspace__slug=accepted_slug).first()
            if membership:
                accepted_workspace = membership.workspace

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            if is_guest:
                messages.error(request, 'Contas de convidado n\u00e3o podem criar workspaces.')
                return redirect('tracker:workspace_select')
            create_form = WorkspaceForm(request.POST, prefix='create')
            if create_form.is_valid():
                workspace = create_form.save(commit=False)
                workspace.owner = request.user
                workspace.slug = create_form.cleaned_data['slug']
                workspace.save()
                WorkspaceMembership.objects.create(workspace=workspace, user=request.user, role='owner')
                request.session['workspace_slug'] = workspace.slug
                request.session['workspace_global'] = False
                messages.success(request, 'Workspace criado e selecionado.')
                return redirect('tracker:dashboard')
        elif action == 'switch':
            slug_form = WorkspaceSlugForm(request.POST, prefix='slug')
            if slug_form.is_valid():
                slug = slug_form.cleaned_data['workspace_slug']
                if request.user.is_superuser:
                    workspace = Workspace.objects.filter(slug=slug, is_active=True).first()
                else:
                    workspace = Workspace.objects.filter(
                        slug=slug,
                        memberships__user=request.user,
                        is_active=True,
                    ).first()
                if workspace:
                    request.session['workspace_slug'] = workspace.slug
                    request.session['workspace_global'] = False
                    messages.success(request, f'Workspace {workspace.name} selecionado.')
                    return redirect(request.GET.get('next') or 'tracker:dashboard')
                messages.error(request, 'Workspace n\u00e3o encontrado ou sem permiss\u00e3o.')

    all_workspaces = Workspace.objects.filter(is_active=True).order_by('name') if request.user.is_superuser else None
    return render(
        request,
        'tracker/workspace_select.html',
        {
            'memberships': memberships,
            'pending_invites': pending_invites,
            'create_form': create_form,
            'slug_form': slug_form,
            'all_workspaces': all_workspaces,
            'is_guest': is_guest,
            'can_create': not is_guest,
            'accepted_workspace': accepted_workspace,
        },
    )


@login_required
def workspace_switch(request, slug):
    if slug == 'global':
        if request.user.is_superuser:
            request.session.pop('workspace_slug', None)
            request.session['workspace_global'] = True
            messages.success(request, 'Vis\u00e3o global ativada.')
        else:
            messages.error(request, 'Apenas superusu\u00e1rios podem usar a vis\u00e3o global.')
        return redirect(request.GET.get('next') or 'tracker:dashboard')

    if request.user.is_superuser:
        workspace = Workspace.objects.filter(slug=slug, is_active=True).first()
    else:
        workspace = Workspace.objects.filter(slug=slug, memberships__user=request.user, is_active=True).first()
    if not workspace:
        messages.error(request, 'Workspace n\u00e3o encontrado ou sem permiss\u00e3o.')
        return redirect('tracker:workspace_select')
    request.session['workspace_slug'] = workspace.slug
    request.session['workspace_global'] = False
    messages.success(request, f'Usando workspace {workspace.name}.')
    return redirect(request.GET.get('next') or 'tracker:dashboard')

@login_required
def workspace_members(request, slug=None):
    target_ws = None
    if slug:
        target_ws = Workspace.objects.filter(slug=slug).first()
    else:
        target_ws = getattr(request, "workspace", None)

    if not target_ws:
        messages.error(request, 'Selecione um workspace antes de gerenciar membros.')
        return redirect('tracker:workspace_select')

    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(workspace=target_ws, user=request.user, role='owner').exists()
    is_member = request.user.is_superuser or WorkspaceMembership.objects.filter(workspace=target_ws, user=request.user).exists()
    if not is_member:
        messages.error(request, 'Voc\u00ea n\u00e3o faz parte deste workspace.')
        return redirect('tracker:workspace_select')

    memberships = WorkspaceMembership.objects.select_related('user', 'user__profile').filter(workspace=target_ws)
    search = (request.GET.get('q') or '').strip()
    role_filter = (request.GET.get('role') or '').strip()
    if search:
        memberships = memberships.filter(
            Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(user__email__icontains=search)
        )
    if role_filter:
        memberships = memberships.filter(role=role_filter)
    can_manage = is_owner
    invite_form = WorkspaceMemberInviteForm(request.POST or None) if can_manage else None
    invite_username_form = InviteByUsernameForm(request.POST or None, prefix='byuser') if can_manage else None
    pending_requests = WorkspaceAccessRequest.objects.filter(workspace=target_ws, status='pending') if can_manage else []
    pending_invites = WorkspaceInvite.objects.filter(workspace=target_ws, status='pending').select_related('invited_user') if can_manage else []
    owner_profile = getattr(request.user, "profile", None)
    trial_blocked = bool(
        owner_profile
        and owner_profile.trial_active()
        and not owner_profile.payment_confirmed
        and not request.user.is_superuser
    )

    if request.method == 'POST':
        if not can_manage:
            messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para alterar membros.')
            return redirect('tracker:workspace_members', slug=target_ws.slug)
        if trial_blocked:
            messages.error(request, 'Per\u00edodo de teste n\u00e3o permite convites. Conclua a assinatura para liberar.')
            return redirect('tracker:workspace_members', slug=target_ws.slug)
        action = request.POST.get('action')
        if action == 'invite_email' and invite_form.is_valid():
            email = invite_form.cleaned_data['email']
            name = invite_form.cleaned_data.get('name') or ''
            role = invite_form.cleaned_data['role']

            user = User.objects.filter(email=email).first()
            if not user:
                messages.error(request, 'Usuário não encontrado. Peça para ele criar uma conta.')
                return redirect('tracker:workspace_members', slug=target_ws.slug)
            if WorkspaceMembership.objects.filter(workspace=target_ws, user=user).exists():
                messages.info(request, 'Usuário já faz parte do workspace.')
                return redirect('tracker:workspace_members', slug=target_ws.slug)

            existing_invite = WorkspaceInvite.objects.filter(
                workspace=target_ws,
                invited_user=user,
                status='pending',
            ).first()
            if not existing_invite and not request.user.is_superuser and not _is_paid_user(user):
                unpaid_total = _workspace_unpaid_count(target_ws) + _workspace_unpaid_invite_count(target_ws)
                limit = _workspace_guest_limit(target_ws)
                if unpaid_total >= limit:
                    messages.error(request, f'Limite de {limit} convidados sem assinatura atingido.')
                    return redirect('tracker:workspace_members', slug=target_ws.slug)


            invite, created = WorkspaceInvite.objects.get_or_create(
                workspace=target_ws,
                invited_user=user,
                status='pending',
                defaults={'invited_by': request.user, 'role': role, 'can_edit_tasks': True},
            )
            if not created:
                invite.role = role
                invite.invited_by = request.user
                invite.can_edit_tasks = True
                invite.save(update_fields=['role', 'invited_by', 'can_edit_tasks'])
                messages.info(request, 'Convite atualizado e reenviado.')
            else:
                messages.success(request, 'Convite enviado.')
            record_metric('invite_sent', user=request.user, workspace=target_ws, metadata={'target': user.username})
            return redirect('tracker:workspace_members', slug=target_ws.slug)
        if action == 'invite_username' and invite_username_form.is_valid():
            username = invite_username_form.cleaned_data['username']
            role = invite_username_form.cleaned_data['role']
            user = User.objects.filter(username=username).first()
            if not user:
                messages.error(request, 'Usuário não encontrado.')
                return redirect('tracker:workspace_members', slug=target_ws.slug)
            if WorkspaceMembership.objects.filter(workspace=target_ws, user=user).exists():
                messages.info(request, 'Usuário já faz parte do workspace.')
                return redirect('tracker:workspace_members', slug=target_ws.slug)

            existing_invite = WorkspaceInvite.objects.filter(
                workspace=target_ws,
                invited_user=user,
                status='pending',
            ).first()
            if not existing_invite and not request.user.is_superuser and not _is_paid_user(user):
                unpaid_total = _workspace_unpaid_count(target_ws) + _workspace_unpaid_invite_count(target_ws)
                limit = _workspace_guest_limit(target_ws)
                if unpaid_total >= limit:
                    messages.error(request, f'Limite de {limit} convidados sem assinatura atingido.')
                    return redirect('tracker:workspace_members', slug=target_ws.slug)


            invite, created = WorkspaceInvite.objects.get_or_create(
                workspace=target_ws,
                invited_user=user,
                status='pending',
                defaults={'invited_by': request.user, 'role': role, 'can_edit_tasks': True},
            )
            if not created:
                invite.role = role
                invite.invited_by = request.user
                invite.can_edit_tasks = True
                invite.save(update_fields=['role', 'invited_by', 'can_edit_tasks'])
                messages.info(request, 'Convite atualizado e reenviado.')
            else:
                messages.success(request, 'Convite enviado.')
            record_metric('invite_sent', user=request.user, workspace=target_ws, metadata={'target': user.username})
            return redirect('tracker:workspace_members', slug=target_ws.slug)

    return render(
        request,
        'tracker/workspace_members.html',
        {
            'workspace': target_ws,
            'memberships': memberships,
            'invite_form': invite_form,
            'invite_username_form': invite_username_form,
            'pending_requests': pending_requests,
            'pending_invites': pending_invites,
            'can_manage': can_manage,
            'trial_blocked': trial_blocked,
            'filters': {
                'q': search,
                'role': role_filter,
            },
            'filter_chips': _build_filter_chips(
                request,
                [
                    ('q', 'Busca', search),
                    ('role', 'Papel', role_filter),
                ],
            ),
        },
    )


@login_required
def workspace_invite(request, slug=None):
    target_ws = None
    if slug:
        target_ws = Workspace.objects.filter(slug=slug).first()
    else:
        target_ws = getattr(request, "workspace", None)

    if not target_ws:
        messages.error(request, 'Selecione um workspace antes de convidar membros.')
        return redirect('tracker:workspace_select')

    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(
        workspace=target_ws,
        user=request.user,
        role='owner'
    ).exists()
    if not is_owner:
        messages.error(request, 'Apenas owners podem convidar membros.')
        return redirect('tracker:dashboard')

    owner_profile = getattr(request.user, "profile", None)
    if owner_profile and owner_profile.trial_active() and not owner_profile.payment_confirmed and not request.user.is_superuser:
        messages.error(request, 'Per\u00edodo de teste n\u00e3o permite convites. Conclua a assinatura para liberar.')
        return redirect('tracker:workspace_members', slug=target_ws.slug)

    invite_form = InviteByUsernameForm(request.POST or None)
    pending_invites = WorkspaceInvite.objects.filter(
        workspace=target_ws,
        status='pending'
    ).select_related('invited_user', 'invited_by')

    if request.method == 'POST' and invite_form.is_valid():
        username = invite_form.cleaned_data['username']
        role = invite_form.cleaned_data['role']
        user = User.objects.filter(username=username).first()
        if not user:
            messages.error(request, 'Usu\u00e1rio n\u00e3o encontrado.')
            return redirect('tracker:workspace_invite', slug=target_ws.slug)
        if WorkspaceMembership.objects.filter(workspace=target_ws, user=user).exists():
            messages.info(request, 'Usu\u00e1rio j\u00e1 faz parte do workspace.')
            return redirect('tracker:workspace_invite', slug=target_ws.slug)

        existing_invite = WorkspaceInvite.objects.filter(
            workspace=target_ws,
            invited_user=user,
            status='pending',
        ).first()
        if not existing_invite and not request.user.is_superuser and not _is_paid_user(user):
            unpaid_total = _workspace_unpaid_count(target_ws) + _workspace_unpaid_invite_count(target_ws)
            limit = _workspace_guest_limit(target_ws)
            if unpaid_total >= limit:
                messages.error(request, f'Limite de {limit} convidados sem assinatura atingido.')
                return redirect('tracker:workspace_invite', slug=target_ws.slug)

        invite, created = WorkspaceInvite.objects.get_or_create(
            workspace=target_ws,
            invited_user=user,
            status='pending',
            defaults={'invited_by': request.user, 'role': role, 'can_edit_tasks': True},
        )
        if not created:
            invite.role = role
            invite.invited_by = request.user
            invite.can_edit_tasks = True
            invite.save(update_fields=['role', 'invited_by', 'can_edit_tasks'])
            messages.info(request, 'Convite atualizado e reenviado.')
        else:
            messages.success(request, 'Convite enviado.')
        record_metric('invite_sent', user=request.user, workspace=target_ws, metadata={'target': user.username})
        return redirect('tracker:workspace_invite', slug=target_ws.slug)

    return render(
        request,
        'tracker/workspace_invite.html',
        {
            'workspace': target_ws,
            'invite_form': invite_form,
            'pending_invites': pending_invites,
        },
    )


@login_required
def workspace_delete(request, slug):
    workspace = Workspace.objects.filter(slug=slug).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado.')
        return redirect('tracker:workspace_select')
    if not (request.user.is_superuser or workspace.owner_id == request.user.id):
        messages.error(request, 'Apenas o owner pode excluir este workspace.')
        return redirect('tracker:workspace_members', slug=workspace.slug)
    if request.method != 'POST':
        messages.error(request, 'Requisição inválida.')
        return redirect('tracker:workspace_members', slug=workspace.slug)
    workspace_name = workspace.name
    workspace.delete()
    request.session.pop('workspace_slug', None)
    if request.user.is_superuser:
        request.session['workspace_global'] = True
    messages.success(request, f'Workspace {workspace_name} excluído.')
    return redirect('tracker:workspace_select')


@login_required
def workspace_request_access(request):
    if request.user.is_superuser:
        messages.info(request, 'Superusuário já possui acesso global.')
        return redirect('tracker:dashboard')
    form = AccessRequestForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        slug = form.cleaned_data['slug']
        workspace = Workspace.objects.filter(slug=slug, is_active=True).first()
        if not workspace:
            messages.error(request, 'Workspace não encontrado.')
        else:
            if WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).exists():
                messages.info(request, 'Você já faz parte deste workspace.')
            else:
                req, created = WorkspaceAccessRequest.objects.get_or_create(workspace=workspace, user=request.user)
                if created or req.status == 'pending':
                    req.status = 'pending'
                    req.save(update_fields=['status', 'updated_at'])
                    messages.success(request, 'Pedido enviado ao owner para aprovação.')
                else:
                    messages.info(request, f'Seu pedido está em estado: {req.status}.')
        return redirect('tracker:dashboard')
    return render(request, 'tracker/workspace_request_access.html', {'form': form})


@login_required
def workspace_request_action(request, slug, req_id, decision):
    workspace = Workspace.objects.filter(slug=slug).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado.')
        return redirect('tracker:workspace_select')
    if request.method != 'POST':
        messages.error(request, 'Requisição inválida.')
        return redirect('tracker:workspace_members', slug=slug)
    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(workspace=workspace, user=request.user, role='owner').exists()
    if not is_owner:
        messages.error(request, 'Apenas owners podem aprovar pedidos.')
        return redirect('tracker:dashboard')
    req = get_object_or_404(WorkspaceAccessRequest, pk=req_id, workspace=workspace)
    if decision == 'approve':
        if not request.user.is_superuser and not _is_paid_user(req.user):
            unpaid_total = _workspace_unpaid_count(workspace) + _workspace_unpaid_invite_count(workspace)
            limit = _workspace_guest_limit(workspace)
            if unpaid_total >= limit:
                messages.error(request, f'Limite de {limit} convidados sem assinatura atingido.')
                return redirect('tracker:workspace_members', slug=slug)
        WorkspaceMembership.objects.get_or_create(workspace=workspace, user=req.user, defaults={'role': 'member'})
        req.status = 'approved'
        req.save(update_fields=['status', 'updated_at'])
        messages.success(request, 'Pedido aprovado.')
    elif decision == 'reject':
        req.status = 'rejected'
        req.save(update_fields=['status', 'updated_at'])
        messages.info(request, 'Pedido rejeitado.')
    return redirect('tracker:workspace_members', slug=slug)


@login_required
def workspace_invite_action(request, invite_id, decision):
    invite = get_object_or_404(WorkspaceInvite, pk=invite_id, invited_user=request.user)
    if request.method != 'POST':
        messages.error(request, 'Requisi\u00e7\u00e3o inv\u00e1lida.')
        return redirect('tracker:workspace_select')
    if invite.status != 'pending':
        messages.info(request, 'Este convite j\u00e1 foi respondido.')
        return redirect('tracker:workspace_select')

    if decision == 'accept':
        limit = _workspace_guest_limit(invite.workspace)
        if limit == 0:
            messages.info(
                request,
                'Convite aceito. Este workspace n\u00e3o aceita novos convidados sem assinatura no plano atual.',
            )
        WorkspaceMembership.objects.get_or_create(
            workspace=invite.workspace,
            user=request.user,
            defaults={'role': invite.role, 'can_edit_tasks': invite.can_edit_tasks},
        )
        invite.status = 'accepted'
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        record_metric('invite_accepted', user=request.user, workspace=invite.workspace)
        request.session['workspace_slug'] = invite.workspace.slug
        request.session['workspace_global'] = False
        request.session['invite_flash_workspace'] = invite.workspace.slug
        messages.success(request, 'Convite aceito. Workspace selecionado.')
    elif decision == 'decline':
        invite.status = 'declined'
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        messages.info(request, 'Convite recusado.')
    return redirect('tracker:workspace_select')


@login_required
def profile_edit(request):
    user = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)
    form = ProfileForm(request.POST or None, instance=user)
    avatar_form = ProfileAvatarForm(request.POST or None, request.FILES or None, instance=profile)
    pref_form = NotificationPreferencesForm(request.POST or None, instance=profile, user=request.user)
    wa_profile = WhatsAppProfile.objects.filter(user=user).first()
    wa_form = WhatsAppProfileForm(request.POST or None, instance=wa_profile, user=user)
    pending_limit = profile.master_guest_limit_pending or 0
    current_limit = profile.master_guest_limit or 6
    initial_limit = pending_limit if pending_limit > current_limit else current_limit
    master_guest_form = MasterGuestLimitRequestForm(
        request.POST if request.POST.get('form_type') == 'master_guest' else None,
        initial={'master_guest_limit': initial_limit},
    )
    quota = get_ai_quota(user)
    ai_since = timezone.now() - datetime.timedelta(days=int(quota.get("window_days") or 30))
    ai_totals = AiUsage.objects.filter(user=user, created_at__gte=ai_since).aggregate(
        tokens=Sum('total_tokens'),
        cost=Sum('cost_brl'),
    )
    ai_used = ai_totals.get('tokens') or 0
    ai_cost = ai_totals.get('cost') or 0
    ai_limit = quota.get("limit") or 0
    ai_remaining = quota.get("remaining")
    ai_next_reset = quota.get("next_reset")
    trial_expires_at = profile.trial_expires_at
    trial_remaining = None
    if trial_expires_at:
        trial_remaining = (trial_expires_at.date() - timezone.localdate()).days
        trial_remaining = max(trial_remaining, 0)
    billing_label = dict(UserProfile.BILLING_CHOICES).get(profile.billing_cycle, profile.billing_cycle) if profile else ''
    plan_label_display = profile.get_plan_display() if profile else 'B\u00e1sico'
    if request.method == 'POST' and request.POST.get('form_type') == 'master_guest':
        if not (profile and profile.plan == 'master' and not profile.is_guest):
            messages.error(request, 'Apenas o dono do plano Master pode ajustar convidados.')
            return redirect('tracker:profile_edit')
        if not master_guest_form.is_valid():
            messages.error(request, 'Informe um limite v\u00e1lido de convidados.')
            return redirect('tracker:profile_edit')
        new_limit = master_guest_form.cleaned_data.get('master_guest_limit') or 6
        current_limit = profile.master_guest_limit or 6
        pending_limit = profile.master_guest_limit_pending or 0
        effective_limit = max(current_limit, pending_limit)
        if new_limit < current_limit:
            messages.error(request, 'Para reduzir o limite, fale com o suporte.')
            return redirect('tracker:profile_edit')
        if new_limit == effective_limit:
            messages.info(request, 'O limite atual j\u00e1 est\u00e1 configurado.')
            return redirect('tracker:profile_edit')
        profile.master_guest_limit_pending = new_limit
        profile.save(update_fields=['master_guest_limit_pending'])
        sub = MpSubscription.objects.filter(user=request.user).first()
        pricing_state = get_pricing_state()
        plan_cycle = (sub.plan_cycle if sub else profile.billing_cycle) or 'monthly'
        new_amount = plan_price(pricing_state, 'master', plan_cycle, guest_limit=new_limit)
        if sub and sub.preapproval_id:
            try:
                data = update_preapproval(
                    sub.preapproval_id,
                    {
                        'auto_recurring': {
                            'transaction_amount': float(new_amount),
                            'frequency': 1,
                            'frequency_type': 'months',
                            'currency_id': getattr(settings, 'MP_CURRENCY', 'BRL'),
                        }
                    },
                )
                auto_recurring = (data or {}).get('auto_recurring') if isinstance(data, dict) else None
                if auto_recurring:
                    sub.auto_recurring = auto_recurring
                else:
                    sub.auto_recurring = {**(sub.auto_recurring or {}), 'transaction_amount': float(new_amount)}
                sub.save(update_fields=['auto_recurring', 'updated_at'])
                messages.success(request, 'Limite solicitado. O novo valor ser\u00e1 aplicado na pr\u00f3xima cobran\u00e7a.')
            except Exception as exc:
                messages.warning(request, f'Limite solicitado, mas n\u00e3o foi poss\u00edvel atualizar a cobran\u00e7a automaticamente: {exc}')
        else:
            messages.success(request, 'Limite solicitado. Refa\u00e7a a assinatura para refletir o novo valor.')
        return redirect('tracker:profile_edit')
    if request.method == 'POST' and form.is_valid() and avatar_form.is_valid() and pref_form.is_valid() and wa_form.is_valid():
        form.save()
        avatar_form.save()
        pref_form.save()
        if wa_form.cleaned_data.get("phone_number"):
            wa_instance = wa_form.save(commit=False)
            wa_instance.user = user
            wa_instance.save()
        messages.success(request, 'Perfil atualizado.')
        return redirect('tracker:profile_edit')
    agent_number = getattr(settings, "TWILIO_WHATSAPP_NUMBER", "").strip()
    return render(
        request,
        'tracker/profile_edit.html',
        {
            'form': form,
            'avatar_form': avatar_form,
            'pref_form': pref_form,
            'wa_form': wa_form,
            'show_admin_prefs': request.user.is_superuser,
            'profile': profile,
            'ai_usage': {
                'used': _format_int(ai_used),
                'limit': _format_int(ai_limit) if ai_limit else None,
                'remaining': _format_int(ai_remaining) if ai_remaining is not None else None,
                'cost': ai_cost,
                'window_days': quota.get("window_days") or 30,
                'next_reset': ai_next_reset,
                'unlimited': not ai_limit,
            },
            'trial': {
                'active': bool(profile.trial_active()),
                'expires_at': trial_expires_at,
                'remaining': trial_remaining,
            },
            'plan_info': {
                'plan_label': plan_label_display,
                'billing_cycle': billing_label or 'Mensal',
                'payment_confirmed': bool(profile.payment_confirmed),
                'subscription_expires': profile.subscription_expires,
                'trial_active': bool(profile.trial_active()),
            },
            'master_guest_form': master_guest_form,
            'whatsapp_agent_number': agent_number,
        },
    )


@login_required
def export_my_data(request):
    user = request.user
    owned_workspaces = Workspace.objects.filter(owner=user)
    member_workspace_ids = WorkspaceMembership.objects.filter(user=user).values_list('workspace_id', flat=True)
    workspace_ids = set(member_workspace_ids) | set(owned_workspaces.values_list('id', flat=True))

    task_qs = Task.objects.filter(workspace_id__in=workspace_ids).select_related('workspace')
    steps_qs = TaskStep.objects.filter(task__in=task_qs).select_related('task')

    transactions_qs = Transaction.objects.none()
    categories_qs = Category.objects.none()
    profile = getattr(user, 'profile', None)
    if user.is_superuser or (profile and not profile.is_guest):
        transactions_qs = Transaction.objects.filter(workspace__in=owned_workspaces).select_related('workspace', 'category')
        categories_qs = Category.objects.filter(workspace__in=owned_workspaces)

    payload = {
        'user': {
            'username': user.username,
            'name': user.get_full_name(),
            'email': user.email,
            'date_joined': user.date_joined.isoformat() if user.date_joined else None,
        },
        'workspaces_owned': [
            {'id': ws.id, 'name': ws.name, 'slug': ws.slug, 'created_at': ws.created_at.isoformat()}
            for ws in owned_workspaces
        ],
        'tasks': [
            {
                'id': t.id,
                'title': t.title,
                'due_date': t.due_date.isoformat() if t.due_date else None,
                'status': t.status,
                'progress': float(t.progress or 0),
                'workspace': t.workspace.slug if t.workspace else None,
                'responsible': (t.responsible_user.get_full_name() or t.responsible_user.username) if t.responsible_user else t.responsible,
                'responsible_id': t.responsible_user_id,
                'created_at': t.created_at.isoformat(),
            }
            for t in task_qs
        ],
        'task_steps': [
            {
                'id': s.id,
                'task_id': s.task_id,
                'title': s.title,
                'status': s.status,
                'order': s.order,
                'responsible': s.responsible,
                'responsible_email': s.responsible_email,
                'created_at': s.created_at.isoformat(),
            }
            for s in steps_qs
        ],
        'transactions': [
            {
                'id': tx.id,
                'description': tx.description,
                'date': tx.date.isoformat(),
                'value': float(tx.value),
                'type': tx.type,
                'category': tx.category.name if tx.category else None,
                'workspace': tx.workspace.slug if tx.workspace else None,
                'created_at': tx.created_at.isoformat(),
            }
            for tx in transactions_qs
        ],
        'categories': [
            {
                'id': cat.id,
                'name': cat.name,
                'color': cat.color,
                'workspace': cat.workspace.slug if cat.workspace else None,
            }
            for cat in categories_qs
        ],
    }
    record_metric('data_export', user=user, metadata={'scope': 'self'})
    response = HttpResponse(json.dumps(payload, ensure_ascii=False, indent=2), content_type='application/json; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="meus_dados.json"'
    return response


@login_required
def account_delete_request(request):
    if request.method != 'POST':
        return redirect('tracker:profile_edit')
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if profile.deletion_requested_at:
        messages.info(request, 'Sua solicita\u00e7\u00e3o j\u00e1 foi registrada.')
        return redirect('tracker:profile_edit')
    profile.deletion_requested_at = timezone.now()
    profile.save(update_fields=['deletion_requested_at'])
    notify_account_deletion_request(request.user)
    record_metric('deletion_request', user=request.user)
    messages.success(request, 'Solicita\u00e7\u00e3o enviada. O administrador entrar\u00e1 em contato.')
    return redirect('tracker:profile_edit')


@login_required
def account_delete_self(request):
    if request.method != 'POST':
        return redirect('tracker:profile_edit')
    if request.user.is_superuser:
        messages.error(request, 'Superuser n\u00e3o pode excluir a conta por aqui.')
        return redirect('tracker:profile_edit')
    user = request.user
    username = user.username
    try:
        logout(request)
        user.delete()
        messages.success(request, f'Conta {username} exclu\u00edda com sucesso.')
    except ProtectedError:
        messages.error(request, 'N\u00e3o foi poss\u00edvel excluir a conta por depend\u00eancias.')
        return redirect('tracker:profile_edit')
    return redirect('tracker:login')


@login_required
def contact_admin(request):
    initial = {}
    if request.GET.get('topic') in ('general', 'payment', 'support'):
        initial['topic'] = request.GET.get('topic')
    form = ContactAdminForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        notify_contact_message(
            request.user,
            form.cleaned_data['topic'],
            form.cleaned_data['subject'],
            form.cleaned_data['message'],
        )
        messages.success(request, 'Mensagem enviada ao administrador.')
        return redirect('tracker:contact_admin')
    return render(request, 'tracker/contact_admin.html', {'form': form})


@login_required
def notifications_center(request):
    user = request.user
    filter_mode = request.GET.get('filter', 'all')
    qs = Notification.objects.filter(user=user).select_related('workspace')
    if filter_mode == 'unread':
        qs = qs.filter(read_at__isnull=True)
    notifications = qs.order_by('-created_at')[:80]

    workspace = getattr(request, "workspace", None)
    can_manage_finance = _user_can_view_finance(request, workspace)
    budget_form = CategoryBudgetForm()
    goal_form = BalanceGoalForm()
    budgets = CategoryBudget.objects.none()
    goals = BalanceGoal.objects.none()
    if workspace and can_manage_finance:
        budget_form.fields['category'].queryset = Category.objects.filter(workspace=workspace)
        budgets = CategoryBudget.objects.filter(workspace=workspace).select_related('category').order_by('category__name')
        goals = BalanceGoal.objects.filter(workspace=workspace).order_by('period')

    admin_broadcast_form = AdminBroadcastForm() if user.is_superuser else None

    context = {
        'notifications': notifications,
        'budget_form': budget_form,
        'goal_form': goal_form,
        'budgets': budgets,
        'goals': goals,
        'current_workspace': workspace,
        'can_manage_finance': can_manage_finance,
        'vapid_public_key': getattr(settings, 'VAPID_PUBLIC_KEY', ''),
        'admin_broadcast_form': admin_broadcast_form,
    }
    return render(request, 'tracker/notifications_center.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def notifications_broadcast(request):
    if request.method != 'POST':
        return redirect('tracker:notifications')
    form = AdminBroadcastForm(request.POST)
    if form.is_valid():
        total = broadcast_message(
            title=form.cleaned_data['title'],
            body=form.cleaned_data['message'],
            send_email=form.cleaned_data.get('send_email', False),
            send_push=form.cleaned_data.get('send_push', False),
        )
        messages.success(request, f'Notificação enviada para {total} usuário(s).')
    else:
        messages.error(request, 'Não foi possível enviar a notificação. Verifique os campos.')
    return redirect('tracker:notifications')


@login_required
def notifications_mark_read(request, pk):
    if request.method == 'POST':
        Notification.objects.filter(pk=pk, user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect('tracker:notifications')


@login_required
def notifications_mark_all(request):
    if request.method == 'POST':
        Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect('tracker:notifications')


@login_required
def notifications_delete(request, pk):
    if request.method == 'POST':
        Notification.objects.filter(pk=pk, user=request.user).delete()
    return redirect('tracker:notifications')


@login_required
def push_subscribe(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'invalid_method'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid_payload'}, status=400)
    endpoint = payload.get('endpoint')
    keys = payload.get('keys', {}) or {}
    if not endpoint:
        return JsonResponse({'error': 'missing_endpoint'}, status=400)
    PushSubscription.objects.update_or_create(
        user=request.user,
        endpoint=endpoint,
        defaults={'p256dh': keys.get('p256dh', ''), 'auth': keys.get('auth', '')},
    )
    return JsonResponse({'ok': True})


@login_required
def push_unsubscribe(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'invalid_method'}, status=405)
    PushSubscription.objects.filter(user=request.user).delete()
    return JsonResponse({'ok': True})


@login_required
def category_budget_create(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode configurar or\u00e7amentos neste workspace.')
        return redirect('tracker:notifications')
    if request.method != 'POST':
        return redirect('tracker:notifications')
    form = CategoryBudgetForm(request.POST)
    if form.is_valid():
        CategoryBudget.objects.update_or_create(
            workspace=workspace,
            category=form.cleaned_data['category'],
            period=form.cleaned_data['period'],
            defaults={
                'limit_value': form.cleaned_data['limit_value'],
                'notify_owner': form.cleaned_data['notify_owner'],
            },
        )
        messages.success(request, 'Or\u00e7amento atualizado.')
    else:
        messages.error(request, 'N\u00e3o foi poss\u00edvel salvar o or\u00e7amento.')
    return redirect('tracker:notifications')


@login_required
def category_budget_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover or\u00e7amentos deste workspace.')
        return redirect('tracker:notifications')
    if request.method == 'POST':
        CategoryBudget.objects.filter(pk=pk, workspace=workspace).delete()
        messages.success(request, 'Or\u00e7amento removido.')
    return redirect('tracker:notifications')


@login_required
def balance_goal_create(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode configurar metas neste workspace.')
        return redirect('tracker:notifications')
    if request.method != 'POST':
        return redirect('tracker:notifications')
    form = BalanceGoalForm(request.POST)
    if form.is_valid():
        BalanceGoal.objects.update_or_create(
            workspace=workspace,
            period=form.cleaned_data['period'],
            direction=form.cleaned_data['direction'],
            defaults={
                'target_value': form.cleaned_data['target_value'],
                'notify_owner': form.cleaned_data['notify_owner'],
            },
        )
        messages.success(request, 'Meta atualizada.')
    else:
        messages.error(request, 'N\u00e3o foi poss\u00edvel salvar a meta.')
    return redirect('tracker:notifications')


@login_required
def balance_goal_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Voc\u00ea n\u00e3o pode remover metas deste workspace.')
        return redirect('tracker:notifications')
    if request.method == 'POST':
        BalanceGoal.objects.filter(pk=pk, workspace=workspace).delete()
        messages.success(request, 'Meta removida.')
    return redirect('tracker:notifications')


@login_required
def workspace_member_remove(request, slug, member_id):
    workspace = Workspace.objects.filter(slug=slug).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado.')
        return redirect('tracker:workspace_select')

    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(
        workspace=workspace,
        user=request.user,
        role='owner'
    ).exists()
    if not is_owner:
        messages.error(request, 'Apenas owners podem remover membros.')
        return redirect('tracker:workspace_members', slug=slug)

    membership = get_object_or_404(WorkspaceMembership, pk=member_id, workspace=workspace)
    if request.method == 'POST':
        owner_count = WorkspaceMembership.objects.filter(workspace=workspace, role='owner').count()
        if membership.role == 'owner' and owner_count <= 1:
            messages.error(request, 'Não é possível remover o último owner.')
            return redirect('tracker:workspace_members', slug=slug)
        membership.delete()
        messages.success(request, 'Membro removido.')
    else:
        messages.error(request, 'Requisição inválida.')
    return redirect('tracker:workspace_members', slug=slug)


@login_required
def workspace_member_update(request, slug, member_id):
    workspace = Workspace.objects.filter(slug=slug).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado.')
        return redirect('tracker:workspace_select')
    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(
        workspace=workspace,
        user=request.user,
        role='owner'
    ).exists()
    if not is_owner:
        messages.error(request, 'Apenas owners podem alterar permissões.')
        return redirect('tracker:workspace_members', slug=slug)
    membership = get_object_or_404(WorkspaceMembership, pk=member_id, workspace=workspace)
    if request.method == 'POST':
        role = request.POST.get('role') or membership.role
        can_edit_tasks = request.POST.get('can_edit_tasks') == 'on'
        membership.role = role
        membership.can_edit_tasks = can_edit_tasks
        membership.save(update_fields=['role', 'can_edit_tasks', 'updated_at'])
        messages.success(request, 'Permissões atualizadas.')
    return redirect('tracker:workspace_members', slug=slug)


@login_required
def workspace_leave(request, slug):
    workspace = Workspace.objects.filter(slug=slug).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado.')
        return redirect('tracker:workspace_select')
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
    if not membership:
        messages.error(request, 'Você não faz parte deste workspace.')
        return redirect('tracker:workspace_select')
    if request.user.is_superuser:
        messages.error(request, 'Superuser não pode sair de workspaces. Use o modo global.')
        return redirect('tracker:workspace_members', slug=slug)
    if workspace.owner_id == request.user.id or membership.role == 'owner':
        messages.error(request, 'Owner não pode sair do próprio workspace.')
        return redirect('tracker:workspace_members', slug=slug)
    if request.method != 'POST':
        messages.error(request, 'Ação inválida.')
        return redirect('tracker:workspace_members', slug=slug)
    membership.delete()
    if request.session.get('workspace_slug') == workspace.slug:
        request.session.pop('workspace_slug', None)
    messages.success(request, 'Você saiu do workspace.')
    return redirect('tracker:workspace_select')


# ---------- Subscription invites ----------


@login_required
@user_passes_test(lambda u: u.is_superuser)
def subscription_invite_list(request):
    form = SubscriptionInviteForm(request.POST or None)
    invites = SubscriptionInvite.objects.all().order_by('-created_at')
    if request.method == 'POST' and form.is_valid():
        invite = form.save(commit=False)
        invite.code = (invite.code or _generate_invite_code()).strip().upper()
        invite.created_by = request.user
        invite.save()
        messages.success(request, f'Convite {invite.code} criado.')
        return redirect('tracker:subscription_invite_list')
    return render(request, 'tracker/subscription_invites.html', {'form': form, 'invites': invites})


@login_required
@user_passes_test(lambda u: u.is_superuser)
def subscription_invite_action(request, pk, action):
    invite = get_object_or_404(SubscriptionInvite, pk=pk)
    if request.method != 'POST':
        return redirect('tracker:subscription_invite_list')
    if action == 'toggle':
        invite.is_active = not invite.is_active
        invite.save(update_fields=['is_active'])
        messages.success(request, 'Status do convite atualizado.')
    elif action == 'reset':
        invite.used_count = 0
        invite.save(update_fields=['used_count'])
        SubscriptionInviteUse.objects.filter(invite=invite).delete()
        messages.success(request, 'Uso do convite reiniciado.')
    elif action == 'delete':
        invite.delete()
        messages.success(request, 'Convite removido.')
    return redirect('tracker:subscription_invite_list')


# ---------- Admin / superuser dashboards ----------


@login_required
@user_passes_test(lambda u: u.is_superuser)
def superuser_overview(request):
    users_all = User.objects.all()
    selected_user_id = request.GET.get('user')
    selected_user = None
    if selected_user_id:
        try:
            selected_user = users_all.get(id=selected_user_id)
        except User.DoesNotExist:
            selected_user = None

    pricing_config = PricingConfig.get_solo()
    pricing_state = get_pricing_state()
    pricing_form = PricingConfigForm(request.POST or None, instance=pricing_config)
    if request.method == 'POST' and request.POST.get('pricing_form') == '1':
        if pricing_form.is_valid():
            pricing_form.save()
            messages.success(request, 'Pre\u00e7os atualizados com sucesso.')
            return redirect('tracker:superuser_overview')
        messages.error(request, 'Corrija os campos da promo\u00e7\u00e3o.')

    tx_qs = Transaction.objects.all()
    ws_qs = Workspace.objects.select_related('owner').all()
    task_qs = Task.objects.all()
    cat_qs = Category.objects.all()
    membership_qs = WorkspaceMembership.objects.all()

    if selected_user:
        ws_qs = ws_qs.filter(owner=selected_user)
        tx_qs = tx_qs.filter(workspace__owner=selected_user)
        task_qs = task_qs.filter(workspace__owner=selected_user)
        cat_qs = cat_qs.filter(workspace__owner=selected_user)
        membership_qs = membership_qs.filter(workspace__owner=selected_user)

    total_users = users_all.count()
    total_workspaces = ws_qs.count()
    active_workspaces = ws_qs.filter(is_active=True).count()
    total_transactions = tx_qs.count()
    total_tasks = task_qs.count()
    total_categories = cat_qs.count()
    total_memberships = membership_qs.count()
    pending_requests = WorkspaceAccessRequest.objects.filter(status='pending').count()
    last30 = timezone.now().date() - datetime.timedelta(days=30)
    tx_last30 = tx_qs.filter(date__gte=last30)
    tx_last30_net = (tx_last30.filter(type='income').aggregate(t=Sum('value'))['t'] or 0) - (tx_last30.filter(type='expense').aggregate(t=Sum('value'))['t'] or 0)
    avg_tx_per_user = total_transactions / total_users if total_users else 0
    tasks_open = task_qs.filter(status='ongoing').count()
    tasks_done = task_qs.filter(status='done').count()
    income_sub = (
        Transaction.objects.filter(workspace=OuterRef('pk'), type='income')
        .values('workspace')
        .annotate(total=Sum('value'))
        .values('total')
    )
    expense_sub = (
        Transaction.objects.filter(workspace=OuterRef('pk'), type='expense')
        .values('workspace')
        .annotate(total=Sum('value'))
        .values('total')
    )
    zero_value = Value(0, output_field=DecimalField(max_digits=12, decimal_places=2))
    recent_workspaces = (
        ws_qs.annotate(
            member_count=Count('memberships', distinct=True),
            tx_count=Count('transactions', distinct=True),
            task_count=Count('tasks', distinct=True),
            last_tx_date=Max('transactions__date'),
            last_task_update=Max('tasks__updated_at'),
            income_sum=Coalesce(Subquery(income_sub, output_field=DecimalField(max_digits=12, decimal_places=2)), zero_value),
            expense_sum=Coalesce(Subquery(expense_sub, output_field=DecimalField(max_digits=12, decimal_places=2)), zero_value),
        ).annotate(
            net_sum=ExpressionWrapper(
                F('income_sum') - F('expense_sum'),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        ).order_by('-created_at')[:8]
    )

    activity_since = timezone.now() - datetime.timedelta(days=30)
    engagement_rows = (
        ws_qs.annotate(
            active_tx_users=Count('transactions__responsible', filter=Q(transactions__created_at__gte=activity_since), distinct=True),
            active_task_users=Count('tasks__responsible_user', filter=Q(tasks__updated_at__gte=activity_since), distinct=True),
        ).annotate(
            active_total=F('active_tx_users') + F('active_task_users')
        ).order_by('-active_total', 'name')[:8]
    )

    ai_qs = AiUsage.objects.all()
    if selected_user:
        ai_qs = ai_qs.filter(user=selected_user)
    ai_requests = ai_qs.count()
    ai_totals = ai_qs.aggregate(tokens=Sum('total_tokens'), cost_brl=Sum('cost_brl'), cost_usd=Sum('cost_usd'))
    ai_tokens = ai_totals['tokens'] or 0
    ai_cost_brl = ai_totals['cost_brl'] or 0
    ai_cost_usd = ai_totals['cost_usd'] or 0
    ai_last30 = ai_qs.filter(created_at__date__gte=last30).aggregate(tokens=Sum('total_tokens'), cost_brl=Sum('cost_brl'))
    ai_last30_tokens = ai_last30['tokens'] or 0
    ai_last30_cost_brl = ai_last30['cost_brl'] or 0
    ai_chat_tokens = ai_qs.filter(feature='chat').aggregate(tokens=Sum('total_tokens'))['tokens'] or 0
    ai_import_tokens = ai_qs.filter(feature='import').aggregate(tokens=Sum('total_tokens'))['tokens'] or 0

    ai_daily = (
        ai_qs.filter(created_at__date__gte=last30)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(tokens=Sum('total_tokens'), cost=Sum('cost_brl'))
        .order_by('day')
    )
    ai_daily_labels = [item['day'].strftime('%d/%m') for item in ai_daily]
    ai_daily_tokens = [int(item['tokens'] or 0) for item in ai_daily]
    ai_daily_costs = [float(item['cost'] or 0) for item in ai_daily]

    ai_by_feature = ai_qs.values('feature').annotate(tokens=Sum('total_tokens')).order_by('feature')
    ai_feature_labels = ['Chat' if item['feature'] == 'chat' else 'Importa\u00e7\u00e3o' for item in ai_by_feature]
    ai_feature_tokens = [int(item['tokens'] or 0) for item in ai_by_feature]

    metric_qs = MetricEvent.objects.all()
    if selected_user:
        metric_qs = metric_qs.filter(user=selected_user)
    metric_since = timezone.now() - datetime.timedelta(days=30)
    metric_last30 = metric_qs.filter(created_at__gte=metric_since)
    metric_counts = {
        item['event_type']: item['total']
        for item in metric_last30.values('event_type').annotate(total=Count('id'))
    }
    login_success_30 = metric_counts.get('login_success', 0)
    login_failed_30 = metric_counts.get('login_failed', 0)
    login_blocked_30 = metric_counts.get('login_blocked', 0)
    signup_30 = metric_counts.get('signup', 0)
    invite_sent_30 = metric_counts.get('invite_sent', 0)
    invite_accepted_30 = metric_counts.get('invite_accepted', 0)
    payment_confirmed_30 = metric_counts.get('payment_confirmed', 0)
    subscription_renewed_30 = metric_counts.get('subscription_renewed', 0)
    data_export_30 = metric_counts.get('data_export', 0)

    now = timezone.now()
    today = timezone.localdate()
    profile_qs = UserProfile.objects.select_related('user')
    if selected_user:
        profile_qs = profile_qs.filter(user=selected_user)
    else:
        profile_qs = profile_qs.exclude(user__is_superuser=True)
    profile_qs = profile_qs.filter(is_guest=False)

    funnel_signups = profile_qs.count()
    funnel_trials = profile_qs.filter(trial_expires_at__gte=now, payment_confirmed=False).count()
    funnel_active = profile_qs.filter(payment_confirmed=True, subscription_expires__gte=today).count()

    subs_qs = MpSubscription.objects.all()
    if selected_user:
        subs_qs = subs_qs.filter(user=selected_user)
    active_subs = subs_qs.filter(status__in=['active', 'authorized'])
    cancelled_30 = subs_qs.filter(status='cancelled', updated_at__date__gte=last30).count()
    pending_subs = subs_qs.filter(status__in=['pending', 'rejected']).count()
    conversion_rate = (funnel_active / funnel_signups * 100) if funnel_signups else 0

    def _subscription_amount(sub):
        auto = sub.auto_recurring or {}
        amount = auto.get('transaction_amount')
        if amount:
            try:
                return Decimal(str(amount))
            except Exception:
                pass
        if sub.plan_cycle == 'annual':
            return Decimal(str(pricing_config.regular_annual_price))
        return Decimal(str(pricing_config.regular_monthly_price))

    mrr = Decimal('0')
    for sub in active_subs:
        amount = _subscription_amount(sub)
        mrr += amount
    arr = mrr * Decimal('12')

    revenue_30 = Decimal('0')
    recent_payments = metric_last30.filter(event_type='payment_confirmed')
    for event in recent_payments.select_related('user'):
        sub = MpSubscription.objects.filter(user=event.user).first()
        if sub:
            revenue_30 += _subscription_amount(sub)
    churn_rate = (cancelled_30 / funnel_active * 100) if funnel_active else 0

    revenue_rows = []
    revenue_totals = {
        'monthly_equiv': Decimal('0'),
        'annual_equiv': Decimal('0'),
    }
    paid_profiles = UserProfile.objects.filter(
        is_guest=False,
        is_approved=True,
        payment_confirmed=True,
    ).filter(
        Q(subscription_expires__isnull=True) | Q(subscription_expires__gte=today)
    )
    if selected_user:
        paid_profiles = paid_profiles.filter(user=selected_user)
    for plan_key in ('essential', 'pro', 'master'):
        monthly_count = paid_profiles.filter(plan=plan_key, billing_cycle='monthly').count()
        annual_count = paid_profiles.filter(plan=plan_key, billing_cycle='annual').count()
        monthly_price = Decimal(plan_price(pricing_state, plan_key, 'monthly'))
        annual_price = Decimal(plan_price(pricing_state, plan_key, 'annual'))
        monthly_total = monthly_price * Decimal(monthly_count)
        annual_monthly_equiv = annual_price * Decimal(annual_count)
        annual_total = annual_price * Decimal('12') * Decimal(annual_count)
        total_monthly_equiv = monthly_total + annual_monthly_equiv
        total_annual_equiv = (monthly_total * Decimal('12')) + annual_total
        revenue_rows.append({
            'plan': plan_label(plan_key),
            'monthly_count': monthly_count,
            'monthly_price': monthly_price,
            'monthly_total': monthly_total,
            'annual_count': annual_count,
            'annual_price': annual_price,
            'annual_total': annual_total,
            'total_monthly_equiv': total_monthly_equiv,
            'total_annual_equiv': total_annual_equiv,
        })
        revenue_totals['monthly_equiv'] += total_monthly_equiv
        revenue_totals['annual_equiv'] += total_annual_equiv

    dau = metric_qs.filter(event_type='login_success', created_at__date=today).values('user').distinct().count()
    wau = metric_qs.filter(event_type='login_success', created_at__gte=now - datetime.timedelta(days=7)).values('user').distinct().count()
    mau = metric_qs.filter(event_type='login_success', created_at__gte=now - datetime.timedelta(days=30)).values('user').distinct().count()

    webhook_errors_30 = MpWebhookEvent.objects.filter(status='error', created_at__date__gte=last30).count()

    ai_top_users = (
        ai_qs.values('user__username')
        .annotate(tokens=Sum('total_tokens'), cost=Sum('cost_brl'))
        .order_by('-tokens')[:5]
    )

    ai_limit_alerts = []
    ai_limits = getattr(settings, "AI_PLAN_TOKEN_LIMITS", {"essential": 20000, "pro": 80000, "master": 200000})
    profile_plans = {
        profile.user_id: profile.plan
        for profile in UserProfile.objects.filter(is_guest=False).only('user_id', 'plan')
    }
    usage_window = timezone.now() - datetime.timedelta(days=int(getattr(settings, "AI_USAGE_WINDOW_DAYS", 30)))
    usage_by_user = (
        AiUsage.objects.filter(created_at__gte=usage_window)
        .values('user_id', 'user__username')
        .annotate(tokens=Sum('total_tokens'))
    )
    for row in usage_by_user:
        limit = int(ai_limits.get(profile_plans.get(row['user_id'], 'essential'), ai_limits.get('essential', 0)) or 0)
        if limit <= 0:
            continue
        tokens = int(row['tokens'] or 0)
        if tokens >= int(limit * 0.8):
            ai_limit_alerts.append({
                'username': row['user__username'],
                'tokens': tokens,
                'limit': limit,
            })

    # Global finance stats
    income_total = tx_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense_total = tx_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    balance_total = income_total - expense_total

    per_workspace = (
        tx_qs.values('workspace__name')
        .annotate(
            income=Sum('value', filter=Q(type='income')),
            expense=Sum('value', filter=Q(type='expense')),
        )
        .order_by('workspace__name')
    )
    ws_labels = [w['workspace__name'] or 'Sem workspace' for w in per_workspace]
    ws_income = [float(w['income'] or 0) for w in per_workspace]
    ws_expense = [float(w['expense'] or 0) for w in per_workspace]

    monthly = (
        tx_qs.annotate(month=F('date__month'), year=F('date__year'))
        .values('year', 'month')
        .annotate(
            net=Sum(
                Case(
                    When(type='income', then=F('value')),
                    When(type='expense', then=F('value') * -1),
                    default=0,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )
        .order_by('year', 'month')
    )
    monthly_labels = [f"{m['month']:02d}/{m['year']}" for m in monthly]
    monthly_values = [float(m['net'] or 0) for m in monthly]

    top_users = []
    if not selected_user:
        top_users = (
            Transaction.objects.values('workspace__owner__username')
            .annotate(total=Sum('value', filter=Q(type='income')) - Sum('value', filter=Q(type='expense')))
            .order_by('-total')[:5]
        )

    recent_webhooks = MpWebhookEvent.objects.order_by('-created_at')[:10]
    recent_subscriptions = MpSubscription.objects.select_related('user')
    if selected_user:
        recent_subscriptions = recent_subscriptions.filter(user=selected_user)
    recent_subscriptions = recent_subscriptions.order_by('-updated_at')[:10]

    context = {
        'total_users': total_users,
        'total_workspaces': total_workspaces,
        'active_workspaces': active_workspaces,
        'total_transactions': total_transactions,
        'total_tasks': total_tasks,
        'total_categories': total_categories,
        'total_memberships': total_memberships,
        'pending_requests': pending_requests,
        'tx_last30_net': tx_last30_net,
        'avg_tx_per_user': avg_tx_per_user,
        'tasks_open': tasks_open,
        'tasks_done': tasks_done,
        'recent_workspaces': recent_workspaces,
        'engagement_rows': engagement_rows,
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': balance_total,
        'ws_labels': ws_labels,
        'ws_income': ws_income,
        'ws_expense': ws_expense,
        'monthly_labels': monthly_labels,
        'monthly_values': monthly_values,
        'top_users': top_users,
        'users_all': users_all,
        'selected_user': selected_user,
        'ai_requests': ai_requests,
        'ai_tokens': ai_tokens,
        'ai_cost_brl': ai_cost_brl,
        'ai_cost_usd': ai_cost_usd,
        'ai_last30_tokens': ai_last30_tokens,
        'ai_last30_cost_brl': ai_last30_cost_brl,
        'ai_chat_tokens': ai_chat_tokens,
        'ai_import_tokens': ai_import_tokens,
        'ai_daily_labels': ai_daily_labels,
        'ai_daily_tokens': ai_daily_tokens,
        'ai_daily_costs': ai_daily_costs,
        'ai_feature_labels': ai_feature_labels,
        'ai_feature_tokens': ai_feature_tokens,
        'login_success_30': login_success_30,
        'login_failed_30': login_failed_30,
        'login_blocked_30': login_blocked_30,
        'signup_30': signup_30,
        'invite_sent_30': invite_sent_30,
        'invite_accepted_30': invite_accepted_30,
        'payment_confirmed_30': payment_confirmed_30,
        'subscription_renewed_30': subscription_renewed_30,
        'data_export_30': data_export_30,
        'pricing_form': pricing_form,
        'pricing_config': pricing_config,
        'recent_webhooks': recent_webhooks,
        'recent_subscriptions': recent_subscriptions,
        'funnel_signups': funnel_signups,
        'funnel_trials': funnel_trials,
        'funnel_active': funnel_active,
        'funnel_cancelled_30': cancelled_30,
        'conversion_rate': conversion_rate,
        'mrr': mrr,
        'arr': arr,
        'revenue_30': revenue_30,
        'churn_rate': churn_rate,
        'revenue_rows': revenue_rows,
        'revenue_totals': revenue_totals,
        'dau': dau,
        'wau': wau,
        'mau': mau,
        'pending_subs': pending_subs,
        'webhook_errors_30': webhook_errors_30,
        'ai_top_users': ai_top_users,
        'ai_limit_alerts': ai_limit_alerts,
    }
    return render(request, 'tracker/superuser_overview.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_list(request):
    base_qs = User.objects.select_related('profile').all().annotate(
        workspace_count=Count('workspace_memberships'),
        last_login_ts=F('last_login'),
    ).order_by('-date_joined')
    today = timezone.localdate()
    now = timezone.now()
    pending_count = 0
    active_count = 0
    trial_count = 0
    expired_count = 0
    suspended_count = 0
    guest_count = 0
    paid_count = 0
    unpaid_count = 0
    for user in base_qs:
        profile = getattr(user, 'profile', None)
        user.profile_obj = profile
        user.subscription_expires = profile.subscription_expires if profile else None
        user.plan_label = profile.get_plan_display() if profile else 'B\u00e1sico'
        user.billing_cycle = profile.billing_cycle if profile else None
        user.payment_confirmed = profile.payment_confirmed if profile else False
        user.payment_confirmed_at = profile.payment_confirmed_at if profile else None
        if user.is_superuser:
            user.approval_status = 'superuser'
            user.plan_label = 'Superuser'
            continue
        if profile and profile.is_guest:
            user.approval_status = 'guest'
            guest_count += 1
            user.plan_label = 'Convidado'
            continue
        if not profile or not profile.is_approved:
            if profile and profile.approved_at:
                user.approval_status = 'suspended'
                suspended_count += 1
            else:
                user.approval_status = 'pending'
                pending_count += 1
            continue
        if profile.trial_active() and not profile.payment_confirmed:
            user.approval_status = 'trial'
            trial_count += 1
            continue
        if profile.subscription_expires and profile.subscription_expires < today:
            user.approval_status = 'expired'
            expired_count += 1
            continue
        user.approval_status = 'active'
        active_count += 1
        if profile and profile.payment_confirmed:
            paid_count += 1
        else:
            unpaid_count += 1

    users = base_qs
    search = (request.GET.get('q') or '').strip()
    status_filter = request.GET.get('status') or ''
    plan_filter = request.GET.get('plan') or ''
    billing_filter = request.GET.get('billing') or ''
    payment_filter = request.GET.get('payment') or ''
    if search:
        users = users.filter(
            Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )
    if plan_filter:
        users = users.filter(profile__plan=plan_filter)
    if billing_filter:
        users = users.filter(profile__billing_cycle=billing_filter)
    if payment_filter == 'paid':
        users = users.filter(profile__payment_confirmed=True)
    elif payment_filter == 'unpaid':
        users = users.filter(Q(profile__payment_confirmed=False) | Q(profile__isnull=True))
    if status_filter:
        if status_filter == 'superuser':
            users = users.filter(is_superuser=True)
        elif status_filter == 'guest':
            users = users.filter(profile__is_guest=True)
        elif status_filter == 'pending':
            users = users.filter(Q(profile__isnull=True) | Q(profile__is_approved=False, profile__approved_at__isnull=True))
        elif status_filter == 'suspended':
            users = users.filter(profile__is_approved=False, profile__approved_at__isnull=False)
        elif status_filter == 'trial':
            users = users.filter(profile__trial_expires_at__gte=now, profile__payment_confirmed=False)
        elif status_filter == 'expired':
            users = users.filter(profile__subscription_expires__lt=today, profile__is_approved=True)
        elif status_filter == 'active':
            users = users.filter(profile__is_approved=True).exclude(profile__subscription_expires__lt=today)

    status_labels = {
        'active': 'Ativo',
        'trial': 'Trial',
        'pending': 'Pendente',
        'expired': 'Expirado',
        'suspended': 'Suspenso',
        'guest': 'Convidado',
        'superuser': 'Superuser',
    }
    plan_labels = dict(UserProfile.PLAN_CHOICES)
    billing_labels = dict(UserProfile.BILLING_CHOICES)
    payment_labels = {'paid': 'Pago', 'unpaid': 'Pendente'}

    filter_chips = _build_filter_chips(
        request,
        [
            ('q', 'Busca', search),
            ('status', 'Status', status_labels.get(status_filter, '')),
            ('plan', 'Plano', plan_labels.get(plan_filter, '')),
            ('billing', 'Ciclo', billing_labels.get(billing_filter, '')),
            ('payment', 'Pagamento', payment_labels.get(payment_filter, '')),
        ],
    )

    users = list(users)
    for user in users:
        profile = getattr(user, 'profile', None)
        user.profile_obj = profile
        user.subscription_expires = profile.subscription_expires if profile else None
        user.plan_label = profile.get_plan_display() if profile else 'B\u00e1sico'
        user.billing_cycle = profile.billing_cycle if profile else None
        user.payment_confirmed = profile.payment_confirmed if profile else False
        user.payment_confirmed_at = profile.payment_confirmed_at if profile else None
        if user.is_superuser:
            user.approval_status = 'superuser'
            user.plan_label = 'Superuser'
            continue
        if profile and profile.is_guest:
            user.approval_status = 'guest'
            user.plan_label = 'Convidado'
            continue
        if not profile or not profile.is_approved:
            if profile and profile.approved_at:
                user.approval_status = 'suspended'
            else:
                user.approval_status = 'pending'
            continue
        if profile.trial_active() and not profile.payment_confirmed:
            user.approval_status = 'trial'
            continue
        if profile.subscription_expires and profile.subscription_expires < today:
            user.approval_status = 'expired'
            continue
        user.approval_status = 'active'

    context = {
        'users': users,
        'filters': {
            'q': search,
            'status': status_filter,
            'plan': plan_filter,
            'billing': billing_filter,
            'payment': payment_filter,
        },
        'plan_choices': UserProfile.PLAN_CHOICES,
        'billing_choices': UserProfile.BILLING_CHOICES,
        'pending_count': pending_count,
        'active_count': active_count,
        'trial_count': trial_count,
        'expired_count': expired_count,
        'suspended_count': suspended_count,
        'guest_count': guest_count,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'filter_chips': filter_chips,
    }
    return render(request, 'tracker/user_admin_list.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_detail(request, pk):
    user_obj = get_object_or_404(User.objects.select_related('profile'), pk=pk)
    profile = getattr(user_obj, 'profile', None)
    today = timezone.localdate()
    now = timezone.now()

    trial_active = bool(profile and profile.trial_active() and not profile.payment_confirmed)
    if user_obj.is_superuser:
        approval_status = 'superuser'
    elif profile and profile.is_guest:
        approval_status = 'guest'
    elif not profile or not profile.is_approved:
        if profile and profile.approved_at:
            approval_status = 'suspended'
        else:
            approval_status = 'pending'
    elif trial_active:
        approval_status = 'trial'
    elif profile.subscription_expires and profile.subscription_expires < today:
        approval_status = 'expired'
    else:
        approval_status = 'active'

    owned_ws_qs = Workspace.objects.filter(owner=user_obj)
    owned_workspace_count = owned_ws_qs.count()
    member_workspace_count = WorkspaceMembership.objects.filter(user=user_obj).exclude(workspace__owner=user_obj).count()

    tasks_qs = Task.objects.filter(
        Q(workspace__owner=user_obj) | Q(workspace__memberships__user=user_obj)
    ).distinct()
    tasks_total = tasks_qs.count()
    tasks_done = tasks_qs.filter(status='done').count()
    tasks_open = tasks_qs.filter(status='ongoing').count()
    steps_total = TaskStep.objects.filter(task__in=tasks_qs).count()

    transactions_qs = Transaction.objects.filter(workspace__owner=user_obj)
    tx_totals = transactions_qs.aggregate(
        total=Sum('value'),
        income=Sum(Case(When(type='income', then=F('value')), default=Value(0), output_field=DecimalField())),
        expense=Sum(Case(When(type='expense', then=F('value')), default=Value(0), output_field=DecimalField())),
    )
    tx_income = tx_totals.get('income') or Decimal('0')
    tx_expense = tx_totals.get('expense') or Decimal('0')
    tx_total = transactions_qs.count()
    tx_net = tx_income - tx_expense
    last_tx = transactions_qs.order_by('-date', '-created_at').first()

    categories_count = Category.objects.filter(workspace__owner=user_obj).count()

    ai_window_days = 30
    ai_since = now - datetime.timedelta(days=ai_window_days)
    ai_qs = AiUsage.objects.filter(user=user_obj, created_at__gte=ai_since)
    ai_totals = ai_qs.aggregate(
        tokens=Sum('total_tokens'),
        cost_brl=Sum('cost_brl'),
        cost_usd=Sum('cost_usd'),
    )
    ai_tokens = int(ai_totals.get('tokens') or 0)
    ai_cost_brl = ai_totals.get('cost_brl') or 0
    ai_cost_usd = ai_totals.get('cost_usd') or 0
    ai_requests = ai_qs.count()
    ai_avg_tokens = int(ai_tokens / ai_requests) if ai_requests else 0
    last_ai = ai_qs.order_by('-created_at').first()
    chat_messages = ChatMessage.objects.filter(user=user_obj).count()

    context = {
        'object': user_obj,
        'profile': profile,
        'approval_status': approval_status,
        'trial_active': trial_active,
        'owned_workspace_count': owned_workspace_count,
        'member_workspace_count': member_workspace_count,
        'categories_count': categories_count,
        'tasks_total': tasks_total,
        'tasks_done': tasks_done,
        'tasks_open': tasks_open,
        'steps_total': steps_total,
        'tx_total': tx_total,
        'tx_income': tx_income,
        'tx_expense': tx_expense,
        'tx_net': tx_net,
        'last_tx': last_tx,
        'ai_window_days': ai_window_days,
        'ai_tokens': ai_tokens,
        'ai_cost_brl': ai_cost_brl,
        'ai_cost_usd': ai_cost_usd,
        'ai_requests': ai_requests,
        'ai_avg_tokens': ai_avg_tokens,
        'last_ai': last_ai,
        'chat_messages': chat_messages,
    }
    return render(request, 'tracker/user_admin_detail.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_form(request, pk=None):
    if pk:
        user_obj = get_object_or_404(User, pk=pk)
    else:
        user_obj = None
    form = UserAdminForm(request.POST or None, instance=user_obj)
    profile_instance = None
    if user_obj:
        profile_instance, _ = UserProfile.objects.get_or_create(user=user_obj)
    profile_form = UserProfileAdminForm(request.POST or None, instance=profile_instance)
    ai_usage = None
    if user_obj:
        quota = get_ai_quota(user_obj)
        ai_since = timezone.now() - datetime.timedelta(days=int(quota.get("window_days") or 30))
        ai_totals = AiUsage.objects.filter(user=user_obj, created_at__gte=ai_since).aggregate(
            tokens=Sum('total_tokens'),
            cost=Sum('cost_brl'),
        )
        ai_used = ai_totals.get('tokens') or 0
        ai_cost = ai_totals.get('cost') or 0
        ai_limit = quota.get("limit") or 0
        ai_remaining = quota.get("remaining")
        ai_usage = {
            'used': _format_int(ai_used),
            'limit': _format_int(ai_limit) if ai_limit else None,
            'remaining': _format_int(ai_remaining) if ai_remaining is not None else None,
            'cost': ai_cost,
            'window_days': quota.get("window_days") or 30,
            'next_reset': quota.get("next_reset"),
            'unlimited': not ai_limit,
        }
    if request.method == 'POST' and form.is_valid() and profile_form.is_valid():
        saved_user = form.save()
        profile = profile_form.save(commit=False)
        profile.user = saved_user
        if profile.payment_confirmed and not profile.payment_confirmed_at:
            profile.payment_confirmed_at = timezone.now()
        if not profile.payment_confirmed:
            profile.payment_confirmed_at = None
        profile.save()
        messages.success(request, 'Usuário salvo.')
        return redirect('tracker:user_admin_list')
    return render(
        request,
        'tracker/user_admin_form.html',
        {'form': form, 'profile_form': profile_form, 'object': user_obj, 'ai_usage': ai_usage},
    )


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_status(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if request.method != 'POST':
        messages.error(request, 'Requisição inválida.')
        return redirect('tracker:user_admin_list')

    action = request.POST.get('action')
    profile, _ = UserProfile.objects.get_or_create(user=user_obj)
    today = timezone.localdate()
    if profile.is_guest:
        messages.error(request, 'Conta de convidado não requer aprovação.')
        return redirect('tracker:user_admin_list')

    if action == 'approve':
        profile.is_approved = True
        profile.approved_at = timezone.now()
        profile.approved_by = request.user
        if not profile.subscription_expires or profile.subscription_expires < today:
            profile.subscription_expires = today + datetime.timedelta(days=30)
        profile.save(update_fields=['is_approved', 'approved_at', 'approved_by', 'subscription_expires'])
        Workspace.objects.filter(owner=user_obj).update(is_active=True)
        record_metric('subscription_renewed', user=user_obj, metadata={'cycle': 'monthly', 'action': 'approve'})
        messages.success(request, 'Cadastro aprovado.')
    elif action == 'renew':
        base_date = profile.subscription_expires or today
        if base_date < today:
            base_date = today
        profile.subscription_expires = base_date + datetime.timedelta(days=30)
        profile.is_approved = True
        if not profile.approved_at:
            profile.approved_at = timezone.now()
        profile.save(update_fields=['subscription_expires', 'is_approved', 'approved_at'])
        Workspace.objects.filter(owner=user_obj).update(is_active=True)
        record_metric('subscription_renewed', user=user_obj, metadata={'cycle': 'monthly'})
        messages.success(request, 'Cadastro renovado por 30 dias.')
    elif action == 'renew_annual':
        base_date = profile.subscription_expires or today
        if base_date < today:
            base_date = today
        profile.subscription_expires = base_date + datetime.timedelta(days=365)
        profile.billing_cycle = 'annual'
        profile.is_approved = True
        if not profile.approved_at:
            profile.approved_at = timezone.now()
        profile.save(update_fields=['subscription_expires', 'billing_cycle', 'is_approved', 'approved_at'])
        Workspace.objects.filter(owner=user_obj).update(is_active=True)
        record_metric('subscription_renewed', user=user_obj, metadata={'cycle': 'annual'})
        messages.success(request, 'Cadastro renovado por 1 ano.')
    elif action == 'suspend':
        if request.user == user_obj:
            messages.error(request, 'Você não pode suspender a si mesmo.')
            return redirect('tracker:user_admin_list')
        profile.is_approved = False
        profile.save(update_fields=['is_approved'])
        Workspace.objects.filter(owner=user_obj).update(is_active=False)
        messages.success(request, 'Cadastro suspenso.')
    elif action == 'mark_paid':
        profile.payment_confirmed = True
        profile.payment_confirmed_at = timezone.now()
        profile.save(update_fields=['payment_confirmed', 'payment_confirmed_at'])
        record_metric('payment_confirmed', user=user_obj, metadata={'source': 'admin'})
        messages.success(request, 'Pagamento marcado como confirmado.')
    elif action == 'mark_unpaid':
        profile.payment_confirmed = False
        profile.payment_confirmed_at = None
        profile.save(update_fields=['payment_confirmed', 'payment_confirmed_at'])
        messages.success(request, 'Pagamento marcado como pendente.')
    else:
        messages.error(request, 'Ação inválida.')

    return redirect('tracker:user_admin_list')


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_delete(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        if request.user == user_obj:
            messages.error(request, 'Você não pode remover a si mesmo.')
            return redirect('tracker:user_admin_list')
        user_obj.delete()
        messages.success(request, 'Usuário removido.')
    else:
        messages.error(request, 'Requisição inválida.')
    return redirect('tracker:user_admin_list')






