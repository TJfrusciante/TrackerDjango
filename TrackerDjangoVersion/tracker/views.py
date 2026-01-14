import csv
import datetime
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
)
from .models import (
    Category,
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
)
from .metrics import record_metric
from .pricing import get_pricing_state
from assistant.services import llm_complete, estimate_costs
from assistant.models import AiUsage

User = get_user_model()


# -------- Helpers --------

def _user_can_view_finance(request, workspace) -> bool:
    """Only superuser or owners can ver/editar financas."""
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        return False
    if workspace.owner_id == request.user.id:
        return True
    role = getattr(request, "workspace_role", None)
    return role == 'owner'


def _is_paid_user(user) -> bool:
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    if not profile:
        return False
    if profile.is_guest:
        return False
    return bool(profile.payment_confirmed)


def _workspace_unpaid_count(workspace) -> int:
    return WorkspaceMembership.objects.filter(workspace=workspace).exclude(
        role='owner'
    ).exclude(
        user__is_superuser=True
    ).filter(
        Q(user__profile__isnull=True) | Q(user__profile__is_guest=True) | Q(user__profile__payment_confirmed=False)
    ).count()


def _workspace_unpaid_invite_count(workspace) -> int:
    return WorkspaceInvite.objects.filter(workspace=workspace, status='pending').exclude(
        invited_user__is_superuser=True
    ).filter(
        Q(invited_user__profile__isnull=True)
        | Q(invited_user__profile__is_guest=True)
        | Q(invited_user__profile__payment_confirmed=False)
    ).count()


def _apply_workspace_filter(queryset, workspace, user):
    if workspace:
        return queryset.filter(workspace=workspace)
    if not user.is_superuser:
        return queryset.none()
    return queryset


def _ensure_unique_slug(base: str) -> str:
    slug = slugify(base) or "workspace"
    base_slug = slug
    counter = 1
    while Workspace.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


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
    pricing_plans = [
        {
            "name": "Mensal",
            "price": pricing_state.monthly_price,
            "regular_price": pricing_state.regular_monthly_price,
            "subtitle": f"R$ {pricing_state.monthly_price:.2f}/m\u00eas. Cancele quando quiser.",
            "badge": "Promo\u00e7\u00e3o" if pricing_state.promo_active else "Flex\u00edvel",
            "features": [
                "1 workspace + at\u00e9 3 participantes",
                "Dashboards financeiros e tarefas em etapas",
                "Notifica\u00e7\u00f5es, or\u00e7amentos e metas",
                "Agente de IA com dados do workspace",
            ],
        },
        {
            "name": "Anual",
            "price": pricing_state.annual_price,
            "regular_price": pricing_state.regular_annual_price,
            "subtitle": f"R$ {pricing_state.annual_price:.2f}/m\u00eas (R$ {annual_total:.2f}/ano).",
            "badge": "Mais escolhido",
            "annual_total": annual_total,
            "features": [
                "Tudo do Mensal",
                "Resumos semanais e mensais",
                "Uso de IA ampliado",
                "Backups e hist\u00f3rico estendido",
            ],
        },
    ]

    feature_cards = [
        {
            "icon": "fa-solid fa-wallet",
            "title": "Financeiro inteligente",
            "description": "Entradas, sa\u00eddas, or\u00e7amentos por categoria e metas de saldo.",
        },
        {
            "icon": "fa-solid fa-list-check",
            "title": "Tarefas em etapas",
            "description": "Subtarefas com respons\u00e1veis, progresso autom\u00e1tico e prazos.",
        },
        {
            "icon": "fa-solid fa-bell",
            "title": "Notifica\u00e7\u00f5es",
            "description": "Centro in-app, e-mails, lembretes e resumos semanais/mensais.",
        },
        {
            "icon": "fa-solid fa-robot",
            "title": "Agente de IA",
            "description": "Responde sobre seu workspace; superuser enxerga tudo.",
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
        {"title": "Crie sua conta", "text": "Escolha plano mensal ou anual e defina o primeiro workspace."},
        {"title": "Importe dados", "text": "Importe CSV/PDF ou lance com o bot\u00e3o Falar em tempo real."},
        {"title": "Convide sua equipe", "text": "At\u00e9 3 convidados sem assinatura; assinantes n\u00e3o contam no limite."},
        {"title": "Acompanhe no painel", "text": "Troque workspaces, aprove convites e configure alertas."},
    ]

    faq_items = [
        {"question": "Quem paga o plano?", "answer": "Apenas o dono do workspace. H\u00e1 limite de 3 convidados sem assinatura."},
        {"question": "Posso escolher mensal ou anual?", "answer": "Sim. A escolha do ciclo \u00e9 feita no cadastro e pode ser revisada pelo admin."},
        {"question": "Meu financeiro \u00e9 privado?", "answer": "Sim. Cada workspace isola finan\u00e7as; o owner controla permiss\u00f5es de tarefas."},
        {"question": "Posso exportar dados?", "answer": "Sim, CSV das listas e endpoints JSON para gr\u00e1ficos."},
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
        {"id": "transacoes", "label": "Transa\u00e7\u00f5es", "icon": "fa-coins"},
        {"id": "tarefas", "label": "Tarefas", "icon": "fa-list-check"},
        {"id": "workspaces", "label": "Workspaces", "icon": "fa-users"},
        {"id": "notificacoes", "label": "Notifica\u00e7\u00f5es", "icon": "fa-bell"},
        {"id": "alertas", "label": "Or\u00e7amentos e metas", "icon": "fa-bullseye"},
        {"id": "voz", "label": "Falar por voz", "icon": "fa-microphone"},
        {"id": "ia", "label": "Agente de IA", "icon": "fa-robot"},
        {"id": "filtros", "label": "Filtros e exporta\u00e7\u00e3o", "icon": "fa-filter"},
        {"id": "faq", "label": "FAQ", "icon": "fa-circle-question"},
    ]

    faq_items = [
        {
            "question": "Como pedir acesso a um workspace?",
            "answer": "Use o bot\u00e3o Pedir acesso no menu do usu\u00e1rio e informe o slug do workspace. O owner aprova na tela de membros.",
        },
        {
            "question": "Quem pode ver finan\u00e7as?",
            "answer": "Apenas o dono do workspace (owner) e o superuser. Membros comuns veem tarefas, mas n\u00e3o finan\u00e7as.",
        },
        {
            "question": "Como funcionam alertas?",
            "answer": "O owner configura or\u00e7amentos e metas na tela de Notifica\u00e7\u00f5es. Alertas chegam por e-mail e in-app.",
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
            "answer": "Use o bot\u00e3o Ditado em tarefas ou transa\u00e7\u00f5es. O sistema tenta extrair descri\u00e7\u00e3o, data, valor e etapas.",
        },
        {
            "question": "Como funciona o agente de IA?",
            "answer": "Ele responde com base nos dados do seu workspace. Se a chave da OpenAI estiver configurada, a resposta vem do modelo; sen\u00e3o, um resumo local \u00e9 exibido.",
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
    ]

    return render(request, 'tracker/help.html', {"quick_links": quick_links, "faq_items": faq_items})


def terms_page(request):
    return render(request, 'tracker/terms.html')


def privacy_page(request):
    return render(request, 'tracker/privacy.html')


# -------- Dashboard --------

@login_required
def dashboard(request):
    today = timezone.now().date()
    workspace = getattr(request, "workspace", None)
    can_finance = _user_can_view_finance(request, workspace)
    last30_start = today - datetime.timedelta(days=30)

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
    start_param = request.GET.get('start')
    end_param = request.GET.get('end')

    try:
        start_date = datetime.date.fromisoformat(start_param) if start_param else None
    except ValueError:
        start_date = None
    try:
        end_date = datetime.date.fromisoformat(end_param) if end_param else None
    except ValueError:
        end_date = None

    if not start_date or not end_date:
        start_date, end_date = range_from_period(period)

    base_qs = _apply_workspace_filter(
        Transaction.objects.filter(date__gte=start_date, date__lte=end_date),
        workspace,
        request.user,
    ) if can_finance else Transaction.objects.none()
    period_has_data = base_qs.exists()

    income_total = base_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense_total = base_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    tx_count_period = base_qs.count()

    last30_qs = _apply_workspace_filter(
        Transaction.objects.filter(date__gte=last30_start, date__lte=today),
        workspace,
        request.user,
    ) if can_finance else Transaction.objects.none()
    last30_income = last30_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    last30_expense = last30_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    net_30 = last30_income - last30_expense
    avg_daily_expense = (last30_expense / 30) if last30_expense else 0

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

    top_income = (
        base_qs.filter(type='income')
        .values('category__name')
        .annotate(total=Sum('value'))
        .order_by('-total')[:5]
    )
    top_expense = (
        base_qs.filter(type='expense')
        .values('category__name')
        .annotate(total=Sum('value'))
        .order_by('-total')[:5]
    )
    quick_ranges = [
        ('week', 'Ultima semana'),
        ('month', 'Ultimo mes'),
        ('quarter', 'Ultimo trimestre'),
        ('semester', 'Ultimo semestre'),
        ('year', 'Ultimo ano'),
        ('all', 'Tudo'),
    ]

    tasks_qs = _apply_workspace_filter(Task.objects.all(), workspace, request.user)
    tasks_total = tasks_qs.count()
    tasks_done = tasks_qs.filter(status='done').count()
    tasks_progress_pct = round((tasks_done / tasks_total) * 100, 1) if tasks_total else 100
    categories_qs = _apply_workspace_filter(Category.objects.all(), workspace, request.user)
    category_count = categories_qs.count()
    latest_tasks = list(tasks_qs.order_by('-created_at')[:5])
    for task in latest_tasks:
        completed_late = False
        if task.status == 'done' and task.due_date:
            completed_at = task.completed_at or task.updated_at
            completed_date = timezone.localdate(completed_at) if completed_at else None
            if completed_date and completed_date > task.due_date:
                completed_late = True
        task.completed_late = completed_late

    context = {
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': income_total - expense_total,
        'tasks_open': tasks_qs.filter(status='ongoing').count(),
        'tasks_done': tasks_done,
        'tasks_progress_pct': tasks_progress_pct,
        'latest_transactions': base_qs.order_by('-date')[:5],
        'latest_tasks': latest_tasks,
        'chart_labels': chart_labels,
        'chart_values': chart_values,
        'start_date': start_date,
        'end_date': end_date,
        'period': period,
        'top_income': top_income,
        'top_expense': top_expense,
        'quick_ranges': quick_ranges,
        'period_has_data': period_has_data,
        'workspace_mode': workspace,
        'can_finance': can_finance,
        'tx_count_period': tx_count_period,
        'net_30': net_30,
        'avg_daily_expense': avg_daily_expense,
        'category_count': category_count,
    }
    return render(request, 'tracker/dashboard.html', context)


# -------- Transactions --------

@login_required
def transactions_list(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode ver finanças deste workspace.')
        return redirect('tracker:dashboard')

    transactions_qs = _apply_workspace_filter(Transaction.objects.select_related('category'), workspace, request.user)
    search = request.GET.get('q', '').strip()
    tx_type = request.GET.get('type', '')
    category_id = request.GET.get('category', '')
    start = request.GET.get('start', '')
    end = request.GET.get('end', '')

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

    context = {
        'transactions_page': page_obj,
        'transactions_total': paginator.count,
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': income_total - expense_total,
        'categories': Category.objects.filter(workspace=workspace) if workspace else Category.objects.all(),
        'query_string': query_string,
        'today': timezone.now().date(),
        'filters': {
            'q': search,
            'type': tx_type,
            'category': category_id,
            'start': start,
            'end': end,
        },
        'filters_display': {
            'start': fmt(start),
            'end': fmt(end),
        }
    }
    return render(request, 'tracker/transactions_list.html', context)


@login_required
def transaction_create(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode lançar finanças neste workspace.')
        return redirect('tracker:dashboard')
    form = TransactionForm(request.POST or None, initial={'date': timezone.now().date()})
    if workspace:
        form.fields['category'].queryset = Category.objects.filter(workspace=workspace)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if workspace:
            obj.workspace = workspace
        if obj.status == 'done':
            obj.completed_at = timezone.now()
        obj.save()
        if workspace:
            notify_balance_threshold(workspace.owner, workspace)
            check_category_budgets(workspace)
            check_balance_goals(workspace)
        messages.success(request, 'Transação criada com sucesso.')
        return redirect('tracker:transactions_list')
    return render(request, 'tracker/transaction_form.html', {'form': form, 'is_edit': False})


@login_required
def transaction_update(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode editar finanças neste workspace.')
        return redirect('tracker:dashboard')
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    transaction = get_object_or_404(qs, pk=pk)
    form = TransactionForm(request.POST or None, instance=transaction)
    if workspace:
        form.fields['category'].queryset = Category.objects.filter(workspace=workspace)
    is_detail = request.GET.get('detail') == '1'
    if request.method == 'POST' and form.is_valid():
        form.save()
        if workspace:
            check_category_budgets(workspace)
            check_balance_goals(workspace)
        messages.success(request, 'Transação atualizada.')
        return redirect('tracker:transactions_list')
    return render(request, 'tracker/transaction_form.html', {'form': form, 'is_edit': True, 'object': transaction, 'is_detail': is_detail})


@login_required
def transaction_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode remover finanças deste workspace.')
        return redirect('tracker:dashboard')
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    transaction = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        transaction.delete()
        messages.success(request, 'Transação removida.')
    return redirect('tracker:transactions_list')


@login_required
def transaction_toggle_selected(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode editar finanças deste workspace.')
        return redirect('tracker:dashboard')

    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    tx = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        tx.selected = not tx.selected
        tx.save(update_fields=['selected', 'updated_at'])
    return redirect('tracker:transactions_list')


@login_required
def transaction_toggle_type(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode editar finanças deste workspace.')
        return redirect('tracker:dashboard')

    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    tx = get_object_or_404(qs, pk=pk)
    if request.method == 'POST':
        tx.type = 'expense' if tx.type == 'income' else 'income'
        tx.save(update_fields=['type', 'updated_at'])
    return redirect('tracker:transactions_list')


def _parse_statement_rows(text: str, categories_qs):
    sample = text[:2048]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=',;')
        delimiter = sniffed.delimiter
    except Exception:
        delimiter = ','

    lines = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not lines:
        return []

    header = [c.strip().lower() for c in lines[0]]
    has_keywords = any(k in header for k in ('description', 'descrição', 'valor', 'value', 'data', 'date'))

    # Se tiver header, usa DictReader normalmente
    if has_keywords:
        key_map = {
            'descricao': 'description', 'descrição': 'description', 'description': 'description',
            'data': 'date', 'date': 'date',
            'valor': 'value', 'value': 'value',
            'tipo': 'type', 'type': 'type',
            'categoria': 'category', 'category': 'category',
        }
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        mapped_rows = []
        for row in reader:
            mapped = {}
            for key, val in row.items():
                if key is None:
                    continue
                norm = key.strip().lower()
                target = key_map.get(norm, norm)
                mapped[target] = val
            mapped_rows.append(mapped)
        return mapped_rows

    # Sem header: assume colunas [datahora, descrição, valor, tipo?, categoria?]
    data_rows = []
    for row in lines:
        if len(row) < 2:
            continue
        raw_date = (row[0] or '').strip()
        desc = (row[1] or '').strip()
        val = row[2] if len(row) > 2 else ''
        t_type = row[3] if len(row) > 3 else ''
        cat = row[4] if len(row) > 4 else ''
        # tenta separar data de hora
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
@login_required
def transaction_import(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não pode importar finanças neste workspace.')
        return redirect('tracker:dashboard')

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
                    selected=selected,
                ))
            if to_create:
                Transaction.objects.bulk_create(to_create, batch_size=500)
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
            file_bytes = up_file.read()
            rows = []
            if name.endswith('.pdf'):
                rows, parse_error = _parse_pdf_statement(file_bytes)
            else:
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
    tasks_qs = _apply_workspace_filter(Task.objects.prefetch_related('steps').all(), workspace, request.user)
    status = request.GET.get('status', '')
    search = request.GET.get('q', '').strip()
    start = request.GET.get('start', '')
    end = request.GET.get('end', '')

    if status in ('ongoing', 'done'):
        tasks_qs = tasks_qs.filter(status=status)
    if search:
        tasks_qs = tasks_qs.filter(title__icontains=search)
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

    context = {
        'tasks_page': page_obj,
        'tasks_total': paginator.count,
        'open_count': tasks_qs.filter(status='ongoing').count(),
        'done_count': tasks_qs.filter(status='done').count(),
        'today': today,
        'soon_threshold': soon_threshold,
        'query_string': query_string,
        'filters': {
            'status': status,
            'q': search,
            'start': start,
            'end': end,
        },
        'filters_display': {
            'start': fmt(start),
            'end': fmt(end),
        }
    }
    return render(request, 'tracker/tasks_list.html', context)


@login_required
def task_create(request):
    workspace = getattr(request, "workspace", None)
    # Membros sem permissao de edicao nao podem criar tarefas
    if workspace and not request.user.is_superuser:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
        if membership and not membership.can_edit_tasks:
            messages.error(request, 'Você não tem permissão para criar tarefas neste workspace.')
            return redirect('tracker:tasks_list')
    form = TaskForm(request.POST or None, initial={'due_date': timezone.now().date()})
    step_form = TaskStepForm()
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if workspace:
            obj.workspace = workspace
        obj.save()
        step_titles = request.POST.getlist('step_title')
        step_responsibles = request.POST.getlist('step_responsible')
        step_emails = request.POST.getlist('step_responsible_email')
        order = 1
        for idx, title in enumerate(step_titles):
            step_title = (title or '').strip()
            if not step_title:
                continue
            step = TaskStep(
                task=obj,
                title=step_title,
                order=order,
                responsible=(step_responsibles[idx].strip() if idx < len(step_responsibles) and step_responsibles[idx] else ''),
                responsible_email=(step_emails[idx].strip() if idx < len(step_emails) and step_emails[idx] else ''),
            )
            step.save()
            order += 1
        if order > 1:
            _update_task_progress(obj)
        messages.success(request, 'Tarefa criada com sucesso.')
        return redirect('tracker:tasks_list')
    return render(request, 'tracker/task_form.html', {'form': form, 'step_form': step_form, 'is_edit': False, 'steps': []})


@login_required
def task_update(request, pk):
    workspace = getattr(request, "workspace", None)
    qs = Task.objects.prefetch_related('steps')
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    task = get_object_or_404(qs, pk=pk)
    form = TaskForm(request.POST or None, instance=task)
    steps = task.steps.all()
    step_form = TaskStepForm()
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
        if prev_status != 'done' and task.status == 'done':
            notify_task_completed(task, actor=request.user)
        messages.success(request, 'Tarefa atualizada.')
        return redirect('tracker:tasks_list')
    return render(request, 'tracker/task_form.html', {'form': form, 'is_edit': True, 'object': task, 'is_detail': request.GET.get('detail') == '1', 'steps': steps, 'step_form': step_form})


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
    form = TaskStepForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        step = form.save(commit=False)
        step.task = task
        current_max = task.steps.aggregate(m=Max('order'))['m'] or 0
        step.order = current_max + 1
        step.save()
        _update_task_progress(task)
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
        form = TaskStepForm(request.POST, instance=step)
        if form.is_valid():
            step = form.save()
            _update_task_progress(task)
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
def task_delete(request, pk):
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
            messages.error(request, 'Você não pode excluir tarefas neste workspace.')
            return redirect('tracker:tasks_list')
    if request.method == 'POST':
        task.delete()
        messages.success(request, 'Tarefa removida.')
    return redirect('tracker:tasks_list')


@login_required
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
    return redirect('tracker:tasks_list')


# -------- Categories --------

@login_required
def categories_list(request):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não tem permissão para ver categorias.')
        return redirect('tracker:dashboard')
    qs = Category.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        qs = qs.none()
    categories = qs.annotate(total_transactions=Count('transactions'))
    form = CategoryForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if workspace:
            obj.workspace = workspace
        obj.save()
        messages.success(request, 'Categoria criada com sucesso.')
        return redirect('tracker:categories_list')
    return render(request, 'tracker/categories_list.html', {'categories': categories, 'form': form})


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
    return render(request, 'tracker/category_form.html', {'form': form, 'category': category})


@login_required
def category_delete(request, pk):
    workspace = getattr(request, "workspace", None)
    if not _user_can_view_finance(request, workspace):
        messages.error(request, 'Você não tem permissão para ver categorias.')
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
    workspace = getattr(request, "workspace", None)

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

    can_finance = _user_can_view_finance(request, workspace)

    qs_all = _apply_workspace_filter(Transaction.objects.all(), workspace, request.user) if can_finance else Transaction.objects.none()
    qs = qs_all.filter(date__gte=start_date, date__lte=end_date)
    if type_filter in ('income', 'expense'):
        qs = qs.filter(type=type_filter)
    if selected_only:
        qs = qs.filter(selected=True)

    income_total = qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense_total = qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0

    category_net = qs.values('category__name').annotate(
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
    fallback_colors = ['#10b981','#3b82f6','#ef4444','#f59e0b','#8b5cf6','#14b8a6','#ec4899','#6366f1','#0ea5e9','#22c55e']
    color_map = {}
    cat_qs = Category.objects.filter(name__in=labels)
    if workspace:
        cat_qs = cat_qs.filter(workspace=workspace)
    for cat in cat_qs:
        if cat.color:
            color_map[cat.name] = cat.color
    colors = []
    for idx, label in enumerate(labels):
        colors.append(color_map.get(label, fallback_colors[idx % len(fallback_colors)]))

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
        datetime.date(item['year'], item['month'], 1).strftime('%d/%m/%Y')
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
    daily_labels = [item['date'].strftime('%d/%m/%Y') for item in daily]
    daily_values = [float(item['net'] or 0) for item in daily]

    running_labels = []
    running_values = []
    running_total = 0
    for item in daily:
        running_total += float(item['net'] or 0)
        running_labels.append(item['date'].strftime('%d/%m/%Y'))
        running_values.append(running_total)

    return JsonResponse({
        'labels': labels,
        'values': values,
        'colors': colors,
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
    })


# -------- CSV exports --------

def _export_transactions_csv(queryset):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="transacoes.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['descricao', 'categoria', 'data', 'tipo', 'valor'])
    for tx in queryset:
        writer.writerow([
            tx.description,
            tx.category.name,
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
    writer.writerow(['titulo', 'prazo', 'status'])
    for task in queryset:
        writer.writerow([
            task.title,
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
        invite_code = (form.cleaned_data.get('invite_code') or '').strip()
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
            ws_active = bool(invite)
            ws = Workspace.objects.create(name=ws_name, slug=slug, owner=user, is_active=ws_active)
            WorkspaceMembership.objects.create(workspace=ws, user=user, role='owner')
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if invite:
                today = timezone.localdate()
                if invite.plan_cycle == 'annual':
                    expires = today + datetime.timedelta(days=365)
                else:
                    expires = today + datetime.timedelta(days=30)
                profile.billing_cycle = invite.plan_cycle
                profile.payment_confirmed = True
                profile.payment_confirmed_at = timezone.now()
                profile.subscription_expires = expires
                profile.is_approved = True
                profile.approved_at = timezone.now()
                profile.approved_by = None
                profile.save(update_fields=[
                    'billing_cycle',
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
            if not invite and not requires_manual:
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
            if not requires_manual:
                login(request, user)
                messages.success(request, 'Conta criada. Conclua a assinatura para ativar todos os recursos.')
                cycle = getattr(profile, 'billing_cycle', 'monthly')
                return redirect(f"{reverse('payments:subscription_start')}?plan={cycle}")
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

    memberships = WorkspaceMembership.objects.select_related('user').filter(workspace=target_ws)
    can_manage = is_owner
    invite_form = WorkspaceMemberInviteForm(request.POST or None) if can_manage else None
    invite_username_form = InviteByUsernameForm(request.POST or None, prefix='byuser') if can_manage else None
    pending_requests = WorkspaceAccessRequest.objects.filter(workspace=target_ws, status='pending') if can_manage else []
    pending_invites = WorkspaceInvite.objects.filter(workspace=target_ws, status='pending').select_related('invited_user') if can_manage else []

    if request.method == 'POST':
        if not can_manage:
            messages.error(request, 'Voc\u00ea n\u00e3o tem permiss\u00e3o para alterar membros.')
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
                if unpaid_total >= 3:
                    messages.error(request, 'Limite de 3 convidados sem assinatura atingido.')
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
                if unpaid_total >= 3:
                    messages.error(request, 'Limite de 3 convidados sem assinatura atingido.')
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
            if unpaid_total >= 3:
                messages.error(request, 'Limite de 3 convidados sem assinatura atingido.')
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
            if unpaid_total >= 3:
                messages.error(request, 'Limite de 3 convidados sem assinatura atingido.')
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
        if not request.user.is_superuser and not _is_paid_user(invite.invited_user):
            unpaid_invites = _workspace_unpaid_invite_count(invite.workspace)
            unpaid_total = _workspace_unpaid_count(invite.workspace) + max(unpaid_invites - 1, 0)
            if unpaid_total >= 3:
                messages.error(request, 'Limite de 3 convidados sem assinatura atingido.')
                return redirect('tracker:workspace_select')
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
    if request.method == 'POST' and form.is_valid() and avatar_form.is_valid() and pref_form.is_valid():
        form.save()
        avatar_form.save()
        pref_form.save()
        messages.success(request, 'Perfil atualizado.')
        return redirect('tracker:profile_edit')
    return render(
        request,
        'tracker/profile_edit.html',
        {
            'form': form,
            'avatar_form': avatar_form,
            'pref_form': pref_form,
            'show_admin_prefs': request.user.is_superuser,
            'profile': profile,
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

    context = {
        'notifications': notifications,
        'budget_form': budget_form,
        'goal_form': goal_form,
        'budgets': budgets,
        'goals': goals,
        'current_workspace': workspace,
        'can_manage_finance': can_manage_finance,
        'vapid_public_key': getattr(settings, 'VAPID_PUBLIC_KEY', ''),
    }
    return render(request, 'tracker/notifications_center.html', context)


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
    pricing_form = PricingConfigForm(request.POST or None, instance=pricing_config)
    if request.method == 'POST' and request.POST.get('pricing_form') == '1':
        if pricing_form.is_valid():
            pricing_form.save()
            messages.success(request, 'Pre\u00e7os atualizados com sucesso.')
            return redirect('tracker:superuser_overview')
        messages.error(request, 'Corrija os campos da promo\u00e7\u00e3o.')

    tx_qs = Transaction.objects.all()
    ws_qs = Workspace.objects.all()
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
    pending_count = 0
    active_count = 0
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
        elif status_filter == 'expired':
            users = users.filter(profile__subscription_expires__lt=today, profile__is_approved=True)
        elif status_filter == 'active':
            users = users.filter(profile__is_approved=True).exclude(profile__subscription_expires__lt=today)

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
        'expired_count': expired_count,
        'suspended_count': suspended_count,
        'guest_count': guest_count,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
    }
    return render(request, 'tracker/user_admin_list.html', context)


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
        {'form': form, 'profile_form': profile_form, 'object': user_obj},
    )


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_status(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if request.method != 'POST':
        messages.error(request, 'Requisi??o inv?lida.')
        return redirect('tracker:user_admin_list')

    action = request.POST.get('action')
    profile, _ = UserProfile.objects.get_or_create(user=user_obj)
    today = timezone.localdate()
    if profile.is_guest:
        messages.error(request, 'Conta de convidado n?o requer aprova??o.')
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
            messages.error(request, 'Voc? n?o pode suspender a si mesmo.')
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
        messages.error(request, 'A??o inv?lida.')

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
        messages.success(request, 'Cadastro renovado por 30 dias.')
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




