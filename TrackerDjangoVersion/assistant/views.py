import calendar
import datetime
import hashlib
import logging
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.conf import settings
from django.db.models import Sum, Case, When, DecimalField, F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt

from tracker.models import Category, Transaction, Task, TaskStep, TaskCategory, WorkspaceMembership, Workspace
from tracker.metrics import record_metric
from .models import ChatMessage, AiUsage
from .limits import get_ai_quota
from .services import llm_complete, estimate_costs

logger = logging.getLogger(__name__)

MONTHS_PT = {
    'janeiro': 1,
    'fevereiro': 2,
    'marco': 3,
    'abril': 4,
    'maio': 5,
    'junho': 6,
    'julho': 7,
    'agosto': 8,
    'setembro': 9,
    'outubro': 10,
    'novembro': 11,
    'dezembro': 12,
}


def _normalize_text(value: str) -> str:
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', value.lower())
        if unicodedata.category(ch) != 'Mn'
    )


def _resolve_workspace_for_write(request, workspace):
    if workspace:
        return workspace
    if not request.user.is_superuser:
        return None
    slug = request.session.get('workspace_slug')
    if not slug:
        return None
    return Workspace.objects.filter(slug=slug, is_active=True).first()


def _assistant_can_create_transaction(request, workspace) -> bool:
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    if getattr(request, "subscription_grace", False):
        return False
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest:
        return True
    if workspace.owner_id == request.user.id:
        return True
    return getattr(request, "workspace_role", None) == 'owner'


def _assistant_can_create_task(request, workspace) -> bool:
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    if workspace.owner_id == request.user.id:
        return True
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
    if membership and not membership.can_edit_tasks:
        return False
    return bool(membership)


def _parse_date_from_text(message: str):
    msg_norm = _normalize_text(message or '')
    today = timezone.localdate()
    if 'amanha' in msg_norm:
        return today + datetime.timedelta(days=1)
    if 'ontem' in msg_norm:
        return today - datetime.timedelta(days=1)
    if 'hoje' in msg_norm:
        return today

    match = re.search(r'(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?!\d)', msg_norm)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))
        year = year + 2000 if year < 100 else year
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None

    match_iso = re.search(r'(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)', msg_norm)
    if match_iso:
        year = int(match_iso.group(1))
        month = int(match_iso.group(2))
        day = int(match_iso.group(3))
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_decimal_token(token: str):
    if not token:
        return None
    cleaned = str(token).lower().replace('r$', '').replace(' ', '')
    if not cleaned:
        return None
    if cleaned.count(',') == 1 and cleaned.count('.') >= 1 and cleaned.rfind(',') > cleaned.rfind('.'):
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif cleaned.count(',') == 1 and cleaned.count('.') == 0:
        cleaned = cleaned.replace(',', '.')
    else:
        cleaned = cleaned.replace(',', '')
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _extract_amount_from_text(message: str):
    if not message:
        return None
    matches = re.findall(
        r'(?<![\d/])(?:r\$\s*)?(-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})|-?\d+(?:[.,]\d{1,2})?)(?![\d/])',
        _normalize_text(message),
    )
    for raw in matches:
        value = _parse_decimal_token(raw)
        if value is not None and value != 0:
            return value
    return None


def _guess_transaction_type(*texts: str):
    merged = ' '.join(_normalize_text(t or '') for t in texts)
    if any(token in merged for token in ['saida', 'despesa', 'gasto', 'expense']):
        return 'expense'
    if any(token in merged for token in ['entrada', 'receita', 'ganho', 'income']):
        return 'income'
    return None


def _match_category_by_text(categories, hint: str):
    hint_norm = _normalize_text(hint or '').strip()
    if not hint_norm:
        return None
    for cat in categories:
        cat_norm = _normalize_text(cat.name)
        if cat_norm == hint_norm:
            return cat
    for cat in categories:
        cat_norm = _normalize_text(cat.name)
        if hint_norm in cat_norm or cat_norm in hint_norm:
            return cat
    return None


def _resolve_transaction_category(request, workspace, description: str, category_hint: str | None):
    qs = Category.objects.filter(workspace=workspace) if workspace else Category.objects.filter(workspace__isnull=True)
    categories = list(qs.order_by('name'))

    for probe in [category_hint, description]:
        matched = _match_category_by_text(categories, probe or '')
        if matched:
            return matched

    fallback = _match_category_by_text(categories, 'sem categoria') or _match_category_by_text(categories, 'outros')
    if fallback:
        return fallback
    return Category.objects.get_or_create(name='Sem categoria', workspace=workspace)[0]


def _resolve_task_category(workspace, hint: str | None):
    if not hint:
        return None
    categories = list(TaskCategory.objects.filter(workspace=workspace).order_by('name'))
    return _match_category_by_text(categories, hint or '')


def _extract_transaction_payload(message: str):
    original_message = (message or '').strip()
    body = re.sub(
        r'^\s*(insira|insere|adicione|adicionar|lance|lan[çc]ar|registre|registrar|crie|criar)\s+(uma\s+)?transa(?:ç|c)(?:ã|a)o[\s:,-]*',
        '',
        original_message,
        flags=re.IGNORECASE,
    ).strip()
    body_norm = _normalize_text(body)
    parts = [part.strip() for part in body.split(',') if part.strip()]
    parts_norm = [part.strip() for part in body_norm.split(',') if part.strip()]
    description = parts[0] if parts else body
    type_text = parts_norm[1] if len(parts_norm) > 1 else body_norm
    value_text = parts[2] if len(parts) > 2 else body
    date_text = parts[3] if len(parts) > 3 else body

    category_hint = None
    for piece in parts:
        match = re.search(r'categoria\s*(?:[:=]|\s)\s*([\w\s\-/]+)', piece)
        if match:
            category_hint = match.group(1).strip()
            break

    tx_type = _guess_transaction_type(type_text, body_norm)
    value = _parse_decimal_token(value_text) or _extract_amount_from_text(body)
    tx_date = _parse_date_from_text(date_text) or _parse_date_from_text(body) or timezone.localdate()

    return {
        'description': (description or '').strip()[:180],
        'type': tx_type,
        'value': abs(value) if value is not None else None,
        'date': tx_date,
        'category_hint': category_hint,
    }


def _extract_task_payload(message: str):
    original_message = (message or '').strip()
    body = re.sub(
        r'^\s*(insira|insere|adicione|adicionar|lance|lan[çc]ar|registre|registrar|crie|criar)\s+(uma\s+)?tarefa[\s:,-]*',
        '',
        original_message,
        flags=re.IGNORECASE,
    ).strip()
    parts = [part.strip() for part in body.split(',') if part.strip()]
    title = parts[0] if parts else body
    due_date = _parse_date_from_text(body) or timezone.localdate()
    msg_norm = _normalize_text(body)
    status = 'done' if any(token in msg_norm for token in ['concluida', 'finalizada', 'feita', 'resolvida']) else 'ongoing'

    category_hint = None
    for piece in parts:
        match = re.search(r'categoria\s*(?:[:=]|\s)\s*([\w\s\-/]+)', piece)
        if match:
            category_hint = match.group(1).strip()
            break

    cleaned_title = re.sub(r'\bdia\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b', '', title).strip(" .,-")
    return {
        'title': cleaned_title[:180],
        'due_date': due_date,
        'status': status,
        'category_hint': category_hint,
    }


def _create_transaction_from_message(message: str, workspace, request):
    if not workspace:
        return "Selecione um workspace antes de lançar transações pelo agente."
    if not _assistant_can_create_transaction(request, workspace):
        return "Você não tem permissão para lançar transações neste workspace."

    payload = _extract_transaction_payload(message)
    if not payload['description'] or not payload['type'] or payload['value'] is None:
        return (
            "Não consegui identificar todos os campos da transação. "
            "Use: Insira a transação descrição, entrada|saída, valor, dia DD/MM/AAAA."
        )

    category = _resolve_transaction_category(
        request=request,
        workspace=workspace,
        description=payload['description'],
        category_hint=payload.get('category_hint'),
    )
    responsible_user = workspace.owner if workspace and workspace.owner_id else request.user
    icon = 'fa-solid fa-hand-holding-dollar' if payload['type'] == 'income' else 'fa-solid fa-wallet'
    tx = Transaction.objects.create(
        description=payload['description'],
        type=payload['type'],
        value=payload['value'],
        date=payload['date'],
        category=category,
        workspace=workspace,
        responsible=responsible_user,
        icon=icon,
    )
    value_label = f"R$ {tx.value:.2f}"
    type_label = tx.get_type_display()
    return (
        f"Transação criada no workspace {workspace.name}: "
        f"{tx.description} ({type_label}) {value_label} em {tx.date:%d/%m/%Y}."
    )


def _create_task_from_message(message: str, workspace, request):
    if not workspace:
        return "Selecione um workspace antes de criar tarefas pelo agente."
    if not _assistant_can_create_task(request, workspace):
        return "Você não tem permissão para criar tarefas neste workspace."

    payload = _extract_task_payload(message)
    if not payload['title']:
        return "Não consegui identificar o título da tarefa. Exemplo: Crie tarefa pagar condomínio dia 10/02/2026."

    task_category = _resolve_task_category(workspace, payload.get('category_hint'))
    responsible_user = workspace.owner if workspace and workspace.owner_id else request.user
    icon = 'fa-solid fa-calendar-check' if payload['status'] == 'ongoing' else 'fa-solid fa-circle-check'
    task = Task.objects.create(
        title=payload['title'],
        due_date=payload['due_date'],
        status=payload['status'],
        workspace=workspace,
        responsible_user=responsible_user,
        icon=icon,
        task_category=task_category,
    )
    return (
        f"Tarefa criada no workspace {workspace.name}: "
        f"{task.title} (prazo {task.due_date:%d/%m/%Y}, status {task.get_status_display()})."
    )


def _assistant_try_create_entity(message: str, workspace, request):
    msg_norm = _normalize_text(message or '')
    tx_intent = re.search(r'^\s*(por favor\s+)?(insira|insere|adicione|adicionar|lance|lancar|registre|registrar|crie|criar)\b.*\btransacao\b', msg_norm)
    task_intent = re.search(r'^\s*(por favor\s+)?(insira|insere|adicione|adicionar|lance|lancar|registre|registrar|crie|criar)\b.*\btarefa\b', msg_norm)
    if not tx_intent and not task_intent:
        return None

    write_workspace = _resolve_workspace_for_write(request, workspace)
    if tx_intent:
        return _create_transaction_from_message(message, write_workspace, request)
    return _create_task_from_message(message, write_workspace, request)


def _parse_month_range(message: str):
    if not message:
        return None
    normalized = _normalize_text(message)
    today = timezone.localdate()

    def _year_from_text(text: str):
        match = re.search(r'\b(20\d{2})\b', text)
        if match:
            return int(match.group(1))
        if 'ano passado' in text or 'ano anterior' in text or 'last year' in text:
            return today.year - 1
        if 'este ano' in text or 'ano atual' in text:
            return today.year
        return None

    def _safe_year(year_val: int) -> int:
        return year_val + 2000 if year_val < 100 else year_val

    def _last_day(year_val: int, month_val: int) -> int:
        return calendar.monthrange(year_val, month_val)[1]

    year = _year_from_text(normalized)

    # Ranges explícitos (ex.: 01/2025 até 06/2025, 10/01/2025 a 20/01/2025)
    range_tokens = re.findall(r'\b(\d{1,2})[\/-](\d{1,2})[\/-](\d{2,4})\b', normalized)
    month_tokens = re.findall(r'\b(0?[1-9]|1[0-2])[\/-](\d{4})\b', normalized)
    if range_tokens and len(range_tokens) >= 2:
        d1, m1, y1 = range_tokens[0]
        d2, m2, y2 = range_tokens[1]
        start_date = datetime.date(_safe_year(int(y1)), int(m1), int(d1))
        end_date = datetime.date(_safe_year(int(y2)), int(m2), int(d2))
        label = f"{start_date:%d/%m/%Y} a {end_date:%d/%m/%Y}"
        return start_date, end_date, label
    if len(month_tokens) >= 2:
        m1, y1 = month_tokens[0]
        m2, y2 = month_tokens[1]
        y1 = _safe_year(int(y1))
        y2 = _safe_year(int(y2))
        m1 = int(m1)
        m2 = int(m2)
        start_date = datetime.date(y1, m1, 1)
        end_date = datetime.date(y2, m2, _last_day(y2, m2))
        label = f"{m1:02d}/{y1} a {m2:02d}/{y2}"
        return start_date, end_date, label

    # Semestres / metades do ano
    if any(key in normalized for key in ['primeira metade', '1 metade', 'primeiro semestre', '1 semestre', 'h1']):
        if year is None:
            year = today.year - 1 if 'ano passado' in normalized else today.year
        start_date = datetime.date(year, 1, 1)
        end_date = datetime.date(year, 6, _last_day(year, 6))
        label = f"1º semestre/{year}"
        return start_date, end_date, label
    if any(key in normalized for key in ['segunda metade', '2 metade', 'segundo semestre', '2 semestre', 'h2']):
        if year is None:
            year = today.year - 1 if 'ano passado' in normalized else today.year
        start_date = datetime.date(year, 7, 1)
        end_date = datetime.date(year, 12, _last_day(year, 12))
        label = f"2º semestre/{year}"
        return start_date, end_date, label

    month = None
    if 'mes passado' in normalized or 'ultimo mes' in normalized:
        month = today.month - 1
        year = year or today.year
        if month <= 0:
            month = 12
            year = (year or today.year) - 1
    if 'este mes' in normalized or 'mes atual' in normalized:
        month = today.month
        year = year or today.year

    if month is None:
        name_match = re.search(
            r'\b(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\b(?:\s*de)?\s*(\d{4})?',
            normalized,
        )
        if name_match:
            month = MONTHS_PT.get(name_match.group(1))
            if name_match.group(2):
                year = int(name_match.group(2))

    if month is None:
        num_match = re.search(r'\b(0?[1-9]|1[0-2])[\/-](\d{4})\b', normalized)
        if num_match:
            month = int(num_match.group(1))
            year = int(num_match.group(2))

    if month is None:
        inv_match = re.search(r'\b(\d{4})[\/-](0?[1-9]|1[0-2])\b', normalized)
        if inv_match:
            year = int(inv_match.group(1))
            month = int(inv_match.group(2))

    if month is None:
        if year is None:
            return None
        # Ano inteiro (ex.: "ano passado")
        start_date = datetime.date(year, 1, 1)
        end_date = datetime.date(year, 12, _last_day(year, 12))
        label = f"{year}"
        return start_date, end_date, label

    if year is None:
        year = today.year
        if month > today.month:
            year -= 1

    start_date = datetime.date(year, month, 1)
    end_date = datetime.date(year, month, _last_day(year, month))
    label = f'{month:02d}/{year}'
    return start_date, end_date, label


def _summarize_period(workspace, request, start_date, end_date, label: str, include_tasks: bool, include_finance: bool) -> str:
    ws_key = workspace.id if workspace else 'global'
    cache_key = f"ai:summary:{request.user.id}:{ws_key}:{start_date}:{end_date}:{int(include_tasks)}:{int(include_finance)}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    lines = [f'Resumo de {label} ({start_date:%d/%m/%Y} a {end_date:%d/%m/%Y})']

    if include_finance:
        finance_qs = _finance_queryset(request, workspace).filter(date__gte=start_date, date__lte=end_date)
        income = finance_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
        expense = finance_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
        balance = income - expense
        tx_count = finance_qs.count()
        lines.append('')
        lines.append('Financeiro:')
        lines.append(f'- Transações: {tx_count}')
        lines.append(f'- Entradas: R$ {income:.2f}')
        lines.append(f'- Saídas: R$ {expense:.2f}')
        lines.append(f'- Saldo: R$ {balance:.2f}')

        per_category = finance_qs.values('category__name').annotate(
            total=Sum(
                Case(
                    When(type='income', then=F('value')),
                    When(type='expense', then=F('value') * -1),
                    default=0,
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        ).order_by('-total')
        if per_category:
            top = per_category[:3]
            lines.append(
                '- Top categorias (saldo líquido): '
                + ', '.join([f"{c['category__name']} ({c['total']:+.2f})" for c in top])
            )

    if include_tasks:
        tasks_qs = _tasks_queryset(request, workspace)
        tasks_due = tasks_qs.filter(due_date__gte=start_date, due_date__lte=end_date)
        tasks_done = tasks_qs.filter(status='done', completed_at__date__gte=start_date, completed_at__date__lte=end_date)
        tasks_created = tasks_qs.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
        lines.append('')
        lines.append('Tarefas:')
        lines.append(f'- Criadas no período: {tasks_created.count()}')
        lines.append(f'- Com prazo no período: {tasks_due.count()}')
        lines.append(f'- Concluídas no período: {tasks_done.count()}')

    result = '\n'.join(lines)
    ttl = int(getattr(settings, "AI_SUMMARY_CACHE_TTL", 600))
    cache.set(cache_key, result, ttl)
    return result


def _finance_queryset(request, workspace):
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        return Transaction.objects.none()
    return qs


def _tasks_queryset(request, workspace):
    qs = Task.objects.all()
    if workspace:
        if request.user.is_superuser:
            return qs
        return qs.filter(workspace=workspace)
    if request.user.is_superuser:
        return qs
    return Task.objects.none()


def _summarize(workspace, request):
    finance_qs = _finance_queryset(request, workspace)
    income = finance_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
    expense = finance_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
    balance = income - expense

    per_category = finance_qs.values('category__name').annotate(
        total=Sum(
            Case(
                When(type='income', then=F('value')),
                When(type='expense', then=F('value') * -1),
                default=0,
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
    ).order_by('-total')

    tasks_qs = _tasks_queryset(request, workspace)
    open_tasks = tasks_qs.filter(status='ongoing').count()
    done_tasks = tasks_qs.filter(status='done').count()

    summary_lines = [
        f"Entradas: R$ {income:.2f}",
        f"Sa\u00eddas: R$ {expense:.2f}",
        f"Saldo: R$ {balance:.2f}",
        f"Tarefas em andamento: {open_tasks}",
        f"Tarefas conclu\u00eddas: {done_tasks}",
    ]
    categories_qs = Category.objects.none()
    if workspace:
        categories_qs = Category.objects.filter(workspace=workspace)
    elif request.user.is_superuser:
        categories_qs = Category.objects.all()
    category_names = list(categories_qs.values_list('name', flat=True).order_by('name'))
    if category_names:
        preview = ", ".join(category_names[:12])
        if len(category_names) > 12:
            preview = f"{preview}, ..."
        summary_lines.append(f"Categorias dispon\u00edveis: {preview}")
    if per_category:
        top = per_category[:3]
        summary_lines.append(
            "Top categorias (saldo líquido): "
            + ", ".join([f"{c['category__name']} ({c['total']:+.2f})" for c in top])
        )
    return "\n".join(summary_lines)

def _top_category(finance_qs, tx_type: str):
    top = (
        finance_qs.filter(type=tx_type)
        .values('category__name')
        .annotate(total=Sum('value'))
        .order_by('-total')
        .first()
    )
    if not top or not top.get('category__name'):
        return None
    return top['category__name'], top['total'] or 0


def _wants_task_details(message: str) -> bool:
    msg = (message or '').lower()
    if not msg:
        return False
    asks_capability = any(token in msg for token in ['consegue', 'consigo', 'pode', 'capaz', 'capacidade'])
    asks_howto = any(token in msg for token in ['como', 'ajuda', 'onde', 'faço', 'faco', 'fazer', 'inserir', 'adicionar', 'criar'])
    if ('tarefa' in msg or 'tarefas' in msg) and (asks_capability or asks_howto):
        return False

    if msg.strip() in {'tarefa', 'tarefas'}:
        return True

    detail_intents = [
        'minhas tarefas',
        'quais tarefas',
        'listar tarefas',
        'liste tarefas',
        'mostre tarefas',
        'tarefas em andamento',
        'tarefas conclu',
        'detalhe da tarefa',
    ]
    return any(intent in msg for intent in detail_intents)


def _help_response(message: str) -> str | None:
    msg = (message or '').lower()
    if not msg:
        return None

    def has_any(values: list[str]) -> bool:
        return any(v in msg for v in values)

    wants_howto = has_any([
        'como', 'ajuda', 'onde', 'posso', 'faço', 'fazer', 'adicionar', 'lançar', 'lancar',
        'consegue', 'consigo', 'pode', 'capaz', 'inserir', 'criar',
    ])
    if not wants_howto:
        return None

    if has_any(['consegue', 'consigo', 'pode', 'capaz']) and has_any(['tarefa', 'tarefas']):
        return (
            "Sim. Eu consigo criar tarefas no workspace atual quando você pedir em linguagem natural. "
            "Exemplo: \"Insira a tarefa pagar condomínio para 10/02/2026\". "
            "Se quiser, também adiciono categoria e etapas."
        )

    if has_any(['consegue', 'consigo', 'pode', 'capaz']) and has_any(['transa', 'entrada', 'saída', 'saida']):
        return (
            "Sim. Eu consigo lançar transações no workspace atual. "
            "Exemplo: \"Insira a transação condomínio, saída, 350, dia 10/02/2026\"."
        )

    if has_any(['transa', 'lançar', 'lancar', 'lanc', 'entrada', 'saída', 'saida', 'despesa', 'receita']):
        return (
            "Para lançar transações: vá em Transações > Nova. Informe descrição, data, valor, categoria e tipo "
            "(entrada/saída) e salve. Você também pode usar o botão Falar para preencher por voz. "
            "Para importar em lote, use Transações > Importar CSV."
        )

    if has_any(['tarefa', 'etapa', 'subtarefa', 'todo']):
        return (
            "Para criar tarefas: vá em Tarefas > Nova, defina título e prazo. "
            "Você pode adicionar etapas no mesmo formulário e depois editar/atualizar o status. "
            "O botão Falar ajuda a preencher a tarefa e as etapas."
        )

    if has_any(['workspace', 'membro', 'membros', 'convidar', 'convite', 'participante']):
        return (
            "Para convidar pessoas: abra o painel de Workspaces e clique em Convites. "
            "Informe o username e defina se pode editar tarefas. "
            "O convidado aceita na tela de Workspaces (Convites pendentes)."
        )

    if has_any(['categoria', 'categorias', 'cor']):
        return (
            "Para cadastrar categorias: acesse Categorias > Nova. "
            "A cor escolhida aparece nos gráficos do dashboard."
        )

    if has_any(['exportar', 'csv', 'pdf', 'importar']):
        return (
            "Exportação: nas listas de Transações ou Tarefas, use o botão Exportar CSV. "
            "Importação: use Transações > Importar CSV ou Importar PDF."
        )

    if has_any(['notifica', 'email', 'alerta', 'resumo']):
        return (
            "Notificações e resumos ficam no Perfil. "
            "Marque as opções de alertas, tarefas e resumos semanais/mensais e salve."
        )

    if has_any(['ia', 'assistente', 'agente']):
        return (
            "O agente de IA responde com base no workspace atual. "
            "Use perguntas como 'saldo', 'tarefas em andamento' ou 'top categorias'. "
            "Você pode pedir criação direta, por exemplo: "
            "'Insira a transação condomínio, saída, 350, dia 10/02/2026' "
            "ou 'Crie tarefa pagar condomínio dia 10/02/2026'. "
            "Você pode ativar o modo 'resumo local' se quiser evitar chamadas externas."
        )

    if has_any(['plano', 'assinatura', 'mensal', 'anual', 'pagamento']):
        return (
            "Para assinar, acesse seu Perfil e clique em 'Assinar com Mercado Pago'. "
            "Escolha mensal ou anual e conclua o pagamento para liberar o acesso completo."
        )

    if has_any(['voz', 'ditado', 'falar', 'microfone']):
        return (
            "Use o botão Falar nos formulários ou no chat do agente. "
            "Fale normalmente e finalize para preencher os campos automaticamente."
        )

    return "Você pode consultar a página Ajuda no menu para um passo a passo completo."


def _task_detail_summary(workspace, request, message: str, limit_tasks: int = 5, limit_steps: int = 6) -> str:
    tasks_qs = _tasks_queryset(request, workspace)
    msg_norm = _normalize_text(message or '')
    wants_done = any(key in msg_norm for key in ['conclu', 'finaliz', 'encerrad'])
    wants_open = any(key in msg_norm for key in ['penden', 'andamento', 'aberta', 'aberto', 'ongoing'])

    done_tasks = tasks_qs.filter(status='done').order_by('-completed_at', '-updated_at')
    open_tasks = tasks_qs.exclude(status='done').order_by('due_date', '-updated_at')
    total_done = done_tasks.count()
    total_open = open_tasks.count()

    if wants_done and not total_done:
        return "Nenhuma tarefa concluída no momento."
    if wants_open and not total_open:
        return "Nenhuma tarefa em andamento no momento."
    if not wants_done and not wants_open and total_done == 0 and total_open == 0:
        return "Nenhuma tarefa cadastrada no momento."

    lines = []
    if not wants_done or wants_open:
        lines.append(f"Tarefas em andamento ({total_open}):")
        for idx, task in enumerate(open_tasks[:limit_tasks], start=1):
            due_label = task.due_date.strftime('%d/%m/%Y') if task.due_date else '-'
            lines.append(f"{idx}. {task.title}")
            lines.append(f"   - Prazo: {due_label}")
            lines.append(f"   - Status: {task.get_status_display()}")
            steps = TaskStep.objects.filter(task=task).order_by('order', 'created_at')
            if steps.exists():
                lines.append("   - Etapas:")
                for step in steps[:limit_steps]:
                    responsible = step.responsible or '-'
                    email = step.responsible_email or '-'
                    order_label = step.order if step.order else '?'
                    lines.append(
                        f"     - Etapa {order_label}: {step.title} ({step.get_status_display()})"
                    )
                    lines.append(f"       Respons\u00e1vel: {responsible} | {email}")
            else:
                lines.append("   - Etapas: nenhuma cadastrada")
        if total_open > limit_tasks:
            lines.append(f"Mostrando {limit_tasks} de {total_open} tarefas em andamento.")

    if not wants_open or wants_done:
        if lines:
            lines.append("")
        lines.append(f"Tarefas concluídas ({total_done}):")
        for idx, task in enumerate(done_tasks[:limit_tasks], start=1):
            completed_date = task.completed_at.date() if task.completed_at else None
            completed_label = completed_date.strftime('%d/%m/%Y') if completed_date else '-'
            due_label = task.due_date.strftime('%d/%m/%Y') if task.due_date else '-'
            status_label = 'no prazo'
            if completed_date and task.due_date and completed_date > task.due_date:
                status_label = 'com atraso'
            lines.append(f"{idx}. {task.title}")
            lines.append(f"   - Concluída em: {completed_label}")
            lines.append(f"   - Prazo: {due_label}")
            lines.append(f"   - Status: {status_label}")
            steps = TaskStep.objects.filter(task=task).order_by('order', 'created_at')
            if steps.exists():
                lines.append("   - Etapas:")
                for step in steps[:limit_steps]:
                    responsible = step.responsible or '-'
                    email = step.responsible_email or '-'
                    order_label = step.order if step.order else '?'
                    lines.append(
                        f"     - Etapa {order_label}: {step.title} ({step.get_status_display()})"
                    )
                    lines.append(f"       Respons\u00e1vel: {responsible} | {email}")
            else:
                lines.append("   - Etapas: nenhuma cadastrada")
        if total_done > limit_tasks:
            lines.append(f"Mostrando {limit_tasks} de {total_done} tarefas conclu\u00eddas.")
    return "\n".join(lines)


def _assistant_reply(message, workspace, request, local_only: bool = False, fallback_reason: str | None = None):
    """
    Usa LLM se configurado; fallback para resumo baseado em regras.
    Retorna (reply, usage, model_name).
    """
    create_reply = _assistant_try_create_entity(message, workspace, request)
    if create_reply:
        return create_reply, None, None

    msg_norm = _normalize_text(message or '')
    cache_key = None
    if msg_norm:
        ws_key = str(workspace.id) if workspace else 'global'
        digest = hashlib.sha256(msg_norm.encode('utf-8')).hexdigest()
        cache_key = f"ai:reply:{request.user.id}:{ws_key}:{digest}"
        cached = cache.get(cache_key)
        if cached:
            return cached, None, None
    period_range = _parse_month_range(message)

    if any(key in msg_norm for key in ['maior gasto', 'maior despesa', 'maior saida']):
        finance_qs = _finance_queryset(request, workspace)
        if period_range:
            start_date, end_date, label = period_range
            finance_qs = finance_qs.filter(date__gte=start_date, date__lte=end_date)
        top = _top_category(finance_qs, 'expense')
        if not top:
            period_label = f" no período {label}" if period_range else ""
            reply = f"Não encontrei despesas registradas{period_label}."
            if cache_key:
                cache.set(cache_key, reply, 300)
            return reply, None, None
        name, total = top
        period_label = f" no período {label}" if period_range else ""
        reply = f"Seu maior gasto{period_label} foi em {name}: R$ {total:.2f}."
        if cache_key:
            cache.set(cache_key, reply, 300)
        return reply, None, None

    if any(key in msg_norm for key in ['maior receita', 'maior entrada']):
        finance_qs = _finance_queryset(request, workspace)
        if period_range:
            start_date, end_date, label = period_range
            finance_qs = finance_qs.filter(date__gte=start_date, date__lte=end_date)
        top = _top_category(finance_qs, 'income')
        if not top:
            period_label = f" no período {label}" if period_range else ""
            reply = f"Não encontrei receitas registradas{period_label}."
            if cache_key:
                cache.set(cache_key, reply, 300)
            return reply, None, None
        name, total = top
        period_label = f" no período {label}" if period_range else ""
        reply = f"Sua maior receita{period_label} foi em {name}: R$ {total:.2f}."
        if cache_key:
            cache.set(cache_key, reply, 300)
        return reply, None, None

    if period_range:
        start_date, end_date, label = period_range
        wants_tasks = 'tarefa' in msg_norm
        wants_finance = any(
            key in msg_norm for key in ['transa', 'financ', 'saldo', 'entrada', 'saida', 'despesa', 'receita']
        )
        include_tasks = wants_tasks or not wants_finance
        include_finance = wants_finance or not wants_tasks
        reply = _summarize_period(
            workspace, request, start_date, end_date, label, include_tasks, include_finance
        )
        if cache_key:
            cache.set(cache_key, reply, 300)
        return reply, None, None

    help_text = _help_response(message)
    if help_text:
        if cache_key:
            cache.set(cache_key, help_text, 300)
        return help_text, None, None

    if _wants_task_details(message):
        reply = _task_detail_summary(workspace, request, message)
        if cache_key:
            cache.set(cache_key, reply, 300)
        return reply, None, None

    summary = _summarize(workspace, request)
    summary_lines = summary.splitlines()
    if len(summary_lines) > 14:
        summary = "\n".join(summary_lines[:14] + ["..."])
    today = datetime.date.today().strftime("%d/%m/%Y")
    ws_name = workspace.name if workspace else "global"
    context_line = f"workspace={ws_name}; usuario={'superuser' if request.user.is_superuser else request.user.username}"

    system_prompt = (
        "Você é o assistente financeiro do iTracker. Responda em português de forma curta. "
        "Use apenas os dados fornecidos no resumo. Se algo não estiver no resumo, diga que não sabe. "
        f"Contexto de workspace: {context_line}."
    )
    user_prompt = f"Data atual: {today}. Resumo disponível:\n{summary}\nPergunta: {message}"
    if not local_only:
        reply, usage, model_name = llm_complete(system_prompt, user_prompt)
        if reply:
            if cache_key:
                cache.set(cache_key, reply, 300)
            return reply, usage, model_name
    logger.info("LLM fallback usado (local_only=%s)", local_only)
    prefix = fallback_reason or "Não consegui consultar o modelo agora. Aqui vai um resumo rápido:"
    reply = f"{prefix}\n{summary}"
    if cache_key:
        cache.set(cache_key, reply, 300)
    return reply, None, None

def _record_usage(user, workspace, feature, usage, model_name):
    if not usage:
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


def _format_quota_label(quota: dict | None) -> str | None:
    if not quota:
        return None
    limit = int(quota.get("limit") or 0)
    if limit <= 0 or quota.get("remaining") is None:
        return "Uso IA: ilimitado"
    used = int(quota.get("used") or 0)
    window_days = int(quota.get("window_days") or 30)
    used_label = f"{used:,}".replace(",", ".")
    limit_label = f"{limit:,}".replace(",", ".")
    label = f"Uso IA: {used_label} / {limit_label} tokens ({window_days} dias)"
    if not quota.get("allowed", True):
        next_reset = quota.get("next_reset")
        if next_reset:
            next_reset = timezone.localtime(next_reset)
            label = f"{label} \u2022 renova em {next_reset:%d/%m/%Y %H:%M}"
    return label

def _history_for_workspace(user, workspace, limit: int):
    qs = ChatMessage.objects.filter(user=user)
    if workspace:
        qs = qs.filter(workspace=workspace)
    else:
        qs = qs.filter(workspace__isnull=True)
    return qs.order_by('-created_at')[:limit][::-1]


def _rate_limit(request, key, limit=30, window=60):
    ident = request.user.id if request.user.is_authenticated else request.META.get("REMOTE_ADDR", "anon")
    cache_key = f"rl:{key}:{ident}"
    count = cache.get(cache_key, 0)
    if count >= limit:
        return True
    cache.set(cache_key, count + 1, window)
    return False


@login_required
def chat(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest and not request.user.is_superuser:
        messages.error(request, "Conta de convidado não tem acesso ao agente de IA.")
        return redirect('tracker:tasks_list')
    if getattr(request, "subscription_grace", False) and not request.user.is_superuser:
        messages.warning(request, "Assinatura em carência: IA bloqueada até a regularização.")
        return redirect('tracker:tasks_list')
    quota = get_ai_quota(request.user)
    quota_label = _format_quota_label(quota)
    quota_blocked = not quota.get("allowed", True)
    if request.user.is_superuser and workspace is None:
        # se superuser estiver em modo global, nao mostrar historico para evitar volume; nao bloqueia
        pass
    history = _history_for_workspace(request.user, workspace, 30)

    if request.method == 'POST':
        if _rate_limit(request, 'ai_chat', limit=30, window=60):
            messages.warning(request, "Muitas mensagens em pouco tempo. Aguarde alguns segundos e tente novamente.")
            return redirect('assistant:chat')
        content = (request.POST.get('message') or '').strip()
        local_only = request.POST.get('local_only') == 'on' or not os.getenv("OPENAI_API_KEY") or quota_blocked
        fallback_reason = None
        if quota_blocked:
            fallback_reason = (
                f"Limite de IA do seu plano ({quota.get('limit')} tokens/{quota.get('window_days')}d) atingido. "
                "Resumo local:"
            )
        if content:
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='user', content=content)
            reply, usage, model_name = _assistant_reply(
                content,
                workspace,
                request,
                local_only=local_only,
                fallback_reason=fallback_reason,
            )
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='assistant', content=reply)
            _record_usage(request.user, workspace, 'chat', usage, model_name)
            record_metric('ai_request', user=request.user, workspace=workspace, metadata={'local_only': local_only})
        history = _history_for_workspace(request.user, workspace, 30)

    return render(request, 'assistant/chat.html', {
        'history': history,
        'local_only_default': not os.getenv("OPENAI_API_KEY"),
        'ai_quota_label': quota_label,
        'ai_quota_blocked': quota_blocked,
        'current_workspace': workspace,
    })


@login_required
@xframe_options_exempt
def chat_embed(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest and not request.user.is_superuser:
        return render(request, 'assistant/embed.html', {'history': [], 'guest_blocked': True})
    if getattr(request, "subscription_grace", False) and not request.user.is_superuser:
        return render(request, 'assistant/embed.html', {'history': [], 'grace_blocked': True})
    quota = get_ai_quota(request.user)
    quota_label = _format_quota_label(quota)
    quota_blocked = not quota.get("allowed", True)
    history = _history_for_workspace(request.user, workspace, 20)

    if request.method == 'POST':
        if _rate_limit(request, 'ai_chat_embed', limit=30, window=60):
            return render(request, 'assistant/embed.html', {
                'history': history,
                'current_workspace': workspace,
                'ai_quota_label': quota_label,
                'ai_quota_blocked': quota_blocked,
                'rate_limited': True,
            })
        content = (request.POST.get('message') or '').strip()
        local_only = not os.getenv("OPENAI_API_KEY") or quota_blocked
        fallback_reason = None
        if quota_blocked:
            fallback_reason = (
                f"Limite de IA do seu plano ({quota.get('limit')} tokens/{quota.get('window_days')}d) atingido. "
                "Resumo local:"
            )
        if content:
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='user', content=content)
            reply, usage, model_name = _assistant_reply(
                content,
                workspace,
                request,
                local_only=local_only,
                fallback_reason=fallback_reason,
            )
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='assistant', content=reply)
            _record_usage(request.user, workspace, 'chat', usage, model_name)
            record_metric('ai_request', user=request.user, workspace=workspace, metadata={'local_only': local_only})
        history = _history_for_workspace(request.user, workspace, 20)

    return render(request, 'assistant/embed.html', {
        'history': history,
        'current_workspace': workspace,
        'ai_quota_label': quota_label,
        'ai_quota_blocked': quota_blocked,
    })

    
