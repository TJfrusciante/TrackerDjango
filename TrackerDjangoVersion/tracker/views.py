import csv
import datetime
import io
import os
import re
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Case, Count, DecimalField, F, Q, Sum, When, Max
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import cache_control

from .forms import (
    CategoryForm,
    LoginForm,
    SignupForm,
    TaskForm,
    TaskStepForm,
    TransactionForm,
    WorkspaceForm,
    WorkspaceMemberInviteForm,
    WorkspaceSlugForm,
    UserAdminForm,
    ProfileForm,
    ProfileAvatarForm,
    InviteByUsernameForm,
    AccessRequestForm,
    StatementUploadForm,
)
from .models import Category, Task, TaskStep, Transaction, Workspace, WorkspaceMembership, WorkspaceAccessRequest, UserProfile
from assistant.services import llm_reply

User = get_user_model()


# -------- Helpers --------

def _user_can_view_finance(request, workspace) -> bool:
    """Only superuser or owners can ver/editar finanças."""
    if not workspace:
        return request.user.is_superuser
    if request.user.is_superuser:
        return True
    if workspace.owner_id == request.user.id:
        return True
    role = getattr(request, "workspace_role", None)
    return role == 'owner'


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


# -------- Landing --------

def home(request):
    if request.user.is_authenticated:
        return redirect('tracker:dashboard')

    pricing_plans = [
        {
            "name": "Mensal",
            "price": Decimal("39.90"),
            "subtitle": "Para começar agora, sem fidelidade.",
            "badge": "Flexível",
            "features": [
                "1 workspace com até 3 participantes",
                "Dashboards financeiros e tarefas",
                "Exportação CSV e filtros avançados",
                "Agente de IA com dados do workspace",
            ],
        },
        {
            "name": "Anual",
            "price": Decimal("29.90"),
            "subtitle": "Melhor custo benefício (cobrança mensal).",
            "badge": "Mais escolhido",
            "features": [
                "Tudo do Mensal",
                "Suporte prioritário",
                "Uso de IA ampliado",
                "Backups e histórico estendido",
            ],
        },
    ]

    feature_cards = [
        {"icon": "fa-solid fa-wallet", "title": "Finanças claras", "description": "Entradas, saídas, categorias coloridas e gráfico por período."},
        {"icon": "fa-solid fa-list-check", "title": "Tarefas em etapas", "description": "Subtarefas com responsáveis, progresso automático e alertas."},
        {"icon": "fa-solid fa-users", "title": "Multi-tenant", "description": "Workspaces, convites, permissões de edição e visão do owner."},
        {"icon": "fa-solid fa-robot", "title": "Agente de IA", "description": "Responde sobre seu workspace; superuser enxerga tudo."},
        {"icon": "fa-solid fa-cloud-arrow-down", "title": "Exportações", "description": "Listagens em CSV e APIs para gráficos/Chart.js."},
        {"icon": "fa-solid fa-mobile-screen", "title": "Responsivo", "description": "Bootstrap 5 e tema atual do Tracker para qualquer dispositivo."},
    ]

    steps = [
        {"title": "Crie sua conta", "text": "Cadastro rápido e escolha do primeiro workspace."},
        {"title": "Importe ou lance dados", "text": "Importação do SQLite antigo e lançamentos manuais."},
        {"title": "Convide sua equipe", "text": "Até 3 participantes grátis por workspace do owner."},
        {"title": "Use o agente", "text": "Pergunte sobre saldo, categorias e tarefas em segundos."},
    ]

    faq_items = [
        {"question": "Quem paga o plano?", "answer": "Apenas o dono do workspace. Participantes entram sem custo."},
        {"question": "Meu financeiro é privado?", "answer": "Sim. Cada workspace isola finanças; o owner controla permissões de tarefas."},
        {"question": "Precisa instalar algo?", "answer": "Não. É web, responsivo e pronto para uso."},
        {"question": "Posso exportar dados?", "answer": "Sim, CSV das listas e endpoints JSON para gráficos."},
    ]

    context = {
        "pricing_plans": pricing_plans,
        "feature_cards": feature_cards,
        "steps": steps,
        "faq_items": faq_items,
    }
    return render(request, 'landing.html', context)


def help_page(request):
    quick_links = [
        {"id": "transacoes", "label": "Transações", "icon": "fa-coins"},
        {"id": "tarefas", "label": "Tarefas", "icon": "fa-list-check"},
        {"id": "workspaces", "label": "Workspaces", "icon": "fa-users"},
        {"id": "ia", "label": "Agente de IA", "icon": "fa-robot"},
        {"id": "filtros", "label": "Filtros e exportação", "icon": "fa-filter"},
        {"id": "faq", "label": "FAQ", "icon": "fa-circle-question"},
    ]

    faq_items = [
        {"question": "Como pedir acesso a um workspace?", "answer": "Use o botão Pedir acesso no menu do usuário e informe o slug do workspace. O owner aprova na tela de membros."},
        {"question": "Quem pode ver finanças?", "answer": "Apenas o dono do workspace (owner) e o superuser. Membros comuns veem tarefas, mas não finanças."},
        {"question": "Como funciona o agente de IA?", "answer": "Ele responde com base nos dados do seu workspace. Se a chave da OpenAI estiver configurada, a resposta vem do modelo; senão, um resumo local é exibido."},
        {"question": "Posso exportar dados?", "answer": "Sim, há exportação CSV nas listas e endpoint JSON para gráficos."},
    ]

    return render(request, 'tracker/help.html', {"quick_links": quick_links, "faq_items": faq_items})


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

    period = request.GET.get('period', '') or 'month'
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

    context = {
        'income_total': income_total,
        'expense_total': expense_total,
        'balance_total': income_total - expense_total,
        'tasks_open': tasks_qs.filter(status='ongoing').count(),
        'tasks_done': tasks_done,
        'tasks_progress_pct': tasks_progress_pct,
        'latest_transactions': base_qs.order_by('-date')[:5],
        'latest_tasks': tasks_qs.order_by('-created_at')[:5],
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
        obj.save()
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

        category, source, reason = _suggest_category_for_desc(desc, cat_hint, categories)
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


def _suggest_category_for_desc(desc: str, cat_hint: str, categories):
    """
    Sugere categoria com heurística + IA (se OPENAI_API_KEY estiver configurada).
    Retorna (categoria, fonte, motivo) onde fonte é 'ai' ou 'heuristic'.
    """
    if not categories:
        return None, 'heuristic', 'Sem categorias disponíveis'
    # heurística por hint
    hint = (cat_hint or '').strip().lower()
    if hint:
        for cat in categories:
            cname = cat.name.lower()
            if hint == cname or hint in cname:
                return cat, 'heuristic', 'Correspondência pelo nome informado'

    # heurística por substring no texto
    desc_lower = (desc or '').lower()
    for cat in categories:
        if cat.name.lower() in desc_lower:
            return cat, 'heuristic', 'Nome da categoria presente na descrição'

    # heurística por interseção de tokens
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
        return best, 'heuristic', 'Maior interseção de palavras com a categoria'

    # IA opcional
    if os.getenv("OPENAI_API_KEY"):
        names = [cat.name for cat in categories]
        sys_prompt = (
            "Você é um classificador de categorias. Escolha uma das categorias existentes para a descrição fornecida. "
            "Responda no formato 'CATEGORIA|motivo breve'. Use apenas uma das categorias listadas. "
            "Se não souber, responda 'Sem categoria|motivo'."
        )
        user_prompt = f"Categorias: {', '.join(names)}. Descrição: {desc}"
        ai_choice = llm_reply(sys_prompt, user_prompt)
        if ai_choice:
            parts = ai_choice.split('|', 1)
            choice_raw = parts[0].strip().lower()
            reason = parts[1].strip() if len(parts) > 1 else 'Sugestão via IA'
            for cat in categories:
                if cat.name.lower() == choice_raw:
                    return cat, 'ai', reason
                if choice_raw in cat.name.lower():
                    return cat, 'ai', reason

    return categories[0], 'heuristic', 'Categoria padrão'


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
                raw = file_bytes.decode('utf-8-sig', errors='ignore')
                rows = _parse_statement_rows(raw, categories)
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
    # membros sem permissão de edição não podem criar tarefas
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
    if request.method == 'POST' and form.is_valid():
        if workspace and not request.user.is_superuser:
            membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
            if membership and not membership.can_edit_tasks:
                messages.error(request, 'Você não tem permissão para editar tarefas neste workspace.')
                return redirect('tracker:tasks_list')
        form.save()
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
def task_step_toggle(request, pk, step_id):
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
    step.done = not step.done
    step.save()
    _update_task_progress(task)
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
    steps = task.steps.all()
    if steps.exists():
        total = steps.count()
        done = steps.filter(done=True).count()
        progress = (done / total) * 100
        task.progress = progress
        task.status = 'done' if done == total else 'ongoing'
    else:
        task.progress = 0
        task.status = 'done' if task.status == 'done' else 'ongoing'
    task.save(update_fields=['progress', 'status', 'updated_at'])


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
        mark_done = task.status != 'done'
        if mark_done:
            if task.steps.exists():
                task.steps.update(done=True)
                _update_task_progress(task)
            else:
                task.status = 'done'
                task.progress = 100
                task.save(update_fields=['status', 'progress', 'updated_at'])
        else:
            if task.steps.exists():
                task.steps.update(done=False)
                _update_task_progress(task)
            else:
                task.status = 'ongoing'
                task.progress = 0
                task.save(update_fields=['status', 'progress', 'updated_at'])
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
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        try:
            category.delete()
            messages.success(request, 'Categoria removida.')
        except ProtectedError:
            messages.error(request, 'Não é possível remover: há transações vinculadas.')
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
    monthly_labels = [f"{item['month']:02d}/{item['year']}" for item in monthly]
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
    daily_labels = [item['date'].strftime('%d/%m') for item in daily]
    daily_values = [float(item['net'] or 0) for item in daily]

    running_labels = []
    running_values = []
    running_total = 0
    for item in daily:
        running_total += float(item['net'] or 0)
        running_labels.append(item['date'].strftime('%d/%m'))
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
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="transacoes.csv"'
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
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="tarefas.csv"'
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
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, 'Bem-vindo(a) de volta!')
            return redirect(request.GET.get('next') or 'tracker:workspace_select')
        else:
            messages.error(request, 'Usuário ou senha inválidos.')
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
        with db_transaction.atomic():
            user = form.save()
            ws_name = form.cleaned_data.get('workspace_name') or f"Workspace de {user.username}"
            slug = _ensure_unique_slug(ws_name)
            ws = Workspace.objects.create(name=ws_name, slug=slug, owner=user)
            WorkspaceMembership.objects.create(workspace=ws, user=user, role='owner')
        login(request, user)
        request.session['workspace_slug'] = ws.slug
        messages.success(request, 'Conta criada com sucesso.')
        return redirect('tracker:dashboard')
    return render(request, 'tracker/auth_register.html', {'form': form})


@login_required
def workspace_select(request):
    memberships = WorkspaceMembership.objects.select_related('workspace').filter(user=request.user, workspace__is_active=True)
    create_form = WorkspaceForm(prefix='create')
    slug_form = WorkspaceSlugForm(prefix='slug')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            create_form = WorkspaceForm(request.POST, prefix='create')
            if create_form.is_valid():
                workspace = create_form.save(commit=False)
                workspace.owner = request.user
                workspace.slug = create_form.cleaned_data['slug']
                workspace.save()
                WorkspaceMembership.objects.create(workspace=workspace, user=request.user, role='owner')
                request.session['workspace_slug'] = workspace.slug
                messages.success(request, 'Workspace criado e selecionado.')
                return redirect('tracker:dashboard')
        elif action == 'switch':
            slug_form = WorkspaceSlugForm(request.POST, prefix='slug')
            if slug_form.is_valid():
                slug = slug_form.cleaned_data['workspace_slug']
                if request.user.is_superuser:
                    workspace = Workspace.objects.filter(slug=slug, is_active=True).first()
                else:
                    workspace = Workspace.objects.filter(slug=slug, memberships__user=request.user, is_active=True).first()
                if workspace:
                    request.session['workspace_slug'] = workspace.slug
                    messages.success(request, f'Workspace {workspace.name} selecionado.')
                    return redirect(request.GET.get('next') or 'tracker:dashboard')
                messages.error(request, 'Workspace não encontrado ou sem permissão.')

    all_workspaces = Workspace.objects.filter(is_active=True).order_by('name') if request.user.is_superuser else None
    return render(
        request,
        'tracker/workspace_select.html',
        {
            'memberships': memberships,
            'create_form': create_form,
            'slug_form': slug_form,
            'all_workspaces': all_workspaces,
        },
    )


@login_required
def workspace_switch(request, slug):
    if slug == 'global':
        if request.user.is_superuser:
            request.session.pop('workspace_slug', None)
            messages.success(request, 'Visão global ativada.')
        else:
            messages.error(request, 'Apenas superusuários podem usar a visão global.')
        return redirect(request.GET.get('next') or 'tracker:dashboard')

    if request.user.is_superuser:
        workspace = Workspace.objects.filter(slug=slug, is_active=True).first()
    else:
        workspace = Workspace.objects.filter(slug=slug, memberships__user=request.user, is_active=True).first()
    if not workspace:
        messages.error(request, 'Workspace não encontrado ou sem permissão.')
        return redirect('tracker:workspace_select')
    request.session['workspace_slug'] = workspace.slug
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
    if not is_owner:
        messages.error(request, 'Apenas owners podem gerenciar membros.')
        return redirect('tracker:dashboard')

    memberships = WorkspaceMembership.objects.select_related('user').filter(workspace=target_ws)
    invite_form = WorkspaceMemberInviteForm(request.POST or None)
    invite_username_form = InviteByUsernameForm(request.POST or None, prefix='byuser')
    pending_requests = WorkspaceAccessRequest.objects.filter(workspace=target_ws, status='pending')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'invite_email' and invite_form.is_valid():
            email = invite_form.cleaned_data['email']
            name = invite_form.cleaned_data.get('name') or ''
            role = invite_form.cleaned_data['role']

            user = User.objects.filter(email=email).first()
            if not user:
                base_username = slugify(email.split('@')[0]) or 'usuario'
                username = base_username
                counter = 1
                while User.objects.filter(username=username).exists():
                    username = f"{base_username}{counter}"
                    counter += 1
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=User.objects.make_random_password(),
                    first_name=name,
                )
            membership, created = WorkspaceMembership.objects.get_or_create(workspace=target_ws, user=user, defaults={'role': role, 'can_edit_tasks': True})
            if not created:
                membership.role = role
                membership.save(update_fields=['role', 'updated_at'])
                messages.info(request, 'Membro atualizado.')
            else:
                messages.success(request, 'Membro adicionado.')
            return redirect('tracker:workspace_members', slug=target_ws.slug)
        if action == 'invite_username' and invite_username_form.is_valid():
            username = invite_username_form.cleaned_data['username']
            role = invite_username_form.cleaned_data['role']
            user = User.objects.filter(username=username).first()
            if not user:
                messages.error(request, 'Usuário não encontrado.')
                return redirect('tracker:workspace_members', slug=target_ws.slug)
            membership, created = WorkspaceMembership.objects.get_or_create(workspace=target_ws, user=user, defaults={'role': role, 'can_edit_tasks': True})
            if not created:
                membership.role = role
                membership.save(update_fields=['role', 'updated_at'])
                messages.info(request, 'Membro atualizado.')
            else:
                messages.success(request, 'Membro adicionado.')
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
        },
    )


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
    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(workspace=workspace, user=request.user, role='owner').exists()
    if not is_owner:
        messages.error(request, 'Apenas owners podem aprovar pedidos.')
        return redirect('tracker:dashboard')
    req = get_object_or_404(WorkspaceAccessRequest, pk=req_id, workspace=workspace)
    if decision == 'approve':
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
def profile_edit(request):
    user = request.user
    profile, _ = UserProfile.objects.get_or_create(user=user)
    form = ProfileForm(request.POST or None, instance=user)
    avatar_form = ProfileAvatarForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == 'POST' and form.is_valid() and avatar_form.is_valid():
        form.save()
        avatar_form.save()
        messages.success(request, 'Perfil atualizado.')
        return redirect('tracker:profile_edit')
    return render(request, 'tracker/profile_edit.html', {'form': form, 'avatar_form': avatar_form})


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
    recent_workspaces = ws_qs.order_by('-created_at')[:8]

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
    }
    return render(request, 'tracker/superuser_overview.html', context)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_list(request):
    users = User.objects.all().annotate(
        workspace_count=Count('workspace_memberships'),
        last_login_ts=F('last_login'),
    ).order_by('-date_joined')
    return render(request, 'tracker/user_admin_list.html', {'users': users})


@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_admin_form(request, pk=None):
    if pk:
        user_obj = get_object_or_404(User, pk=pk)
    else:
        user_obj = None
    form = UserAdminForm(request.POST or None, instance=user_obj)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Usuário salvo.')
        return redirect('tracker:user_admin_list')
    return render(request, 'tracker/user_admin_form.html', {'form': form, 'object': user_obj})


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
