from __future__ import annotations

import calendar
import datetime as dt
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Sum, Q
from django.utils import timezone

from assistant.limits import get_ai_quota
from assistant.models import AiUsage
from assistant.services import llm_complete, estimate_costs
from tracker.models import Category, Transaction, Task, TaskStep, WorkspaceMembership
from .models import CategoryPreference, ParsedTransaction, WhatsAppMessage, WhatsAppProfile


@dataclass
class ParsedResult:
    description: str
    amount: Decimal
    tx_type: str
    tx_date: dt.date
    category: Category | None
    category_label: str
    requires_confirmation: bool


@dataclass
class ParsedTaskResult:
    title: str
    due_date: dt.date
    category: str
    steps: list[str]


DEFAULT_CATEGORY_COLORS = {
    "Moradia": "#38bdf8",
    "Contas/Agua": "#0ea5e9",
    "Contas/Luz": "#f59e0b",
    "Contas/Internet": "#14b8a6",
    "Alimentacao": "#22c55e",
    "Alimentacao/Mercado": "#16a34a",
    "Alimentacao/Restaurante": "#65a30d",
    "Transporte": "#6366f1",
    "Saude": "#ef4444",
    "Lazer": "#a855f7",
    "Educacao": "#3b82f6",
    "Receitas": "#10b981",
    "Receitas/Salario": "#22c55e",
}

DEFAULT_CATEGORY_MAP = {
    "agua": "Contas/Agua",
    "conta de agua": "Contas/Agua",
    "luz": "Contas/Luz",
    "energia": "Contas/Luz",
    "internet": "Contas/Internet",
    "telefone": "Contas/Internet",
    "aluguel": "Moradia",
    "condominio": "Moradia",
    "mercado": "Alimentacao/Mercado",
    "supermercado": "Alimentacao/Mercado",
    "restaurante": "Alimentacao/Restaurante",
    "almoco": "Alimentacao/Restaurante",
    "jantar": "Alimentacao/Restaurante",
    "lanche": "Alimentacao/Restaurante",
    "uber": "Transporte",
    "taxi": "Transporte",
    "gasolina": "Transporte",
    "combustivel": "Transporte",
    "farmacia": "Saude",
    "medico": "Saude",
    "consulta": "Saude",
    "academia": "Lazer",
    "cinema": "Lazer",
    "netflix": "Lazer",
    "curso": "Educacao",
    "escola": "Educacao",
    "faculdade": "Educacao",
    "salario": "Receitas/Salario",
    "pix recebido": "Receitas",
    "deposito": "Receitas",
    "venda": "Receitas",
}

DEFAULT_CATEGORY_LIST = [
    "Moradia",
    "Contas/Agua",
    "Contas/Luz",
    "Contas/Internet",
    "Alimentacao",
    "Alimentacao/Mercado",
    "Alimentacao/Restaurante",
    "Transporte",
    "Saude",
    "Lazer",
    "Educacao",
    "Receitas",
    "Receitas/Salario",
    "Gastos diversos",
]

INCOME_KEYWORDS = (
    "recebi",
    "ganhei",
    "deposito",
    "pix recebido",
    "pagamento recebido",
    "salario",
    "venda",
    "reembolso",
    "entrada",
)
EXPENSE_KEYWORDS = (
    "gastei",
    "paguei",
    "pagar",
    "comprei",
    "compra",
    "debito",
    "pix enviado",
    "transferencia",
    "boleto",
    "saque",
    "saida",
    "gasto",
)

TASK_KEYWORDS = (
    "tarefa",
    "task",
    "lembrete",
    "lembrar",
)


def normalize_text(text: str) -> str:
    text = text or ""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text)


def _match_existing_category(workspace, name: str) -> Category | None:
    normalized = normalize_text(name)
    for category in Category.objects.filter(workspace=workspace):
        if normalize_text(category.name) == normalized:
            return category
    return None


def _clean_amount(raw: str) -> Decimal | None:
    if not raw:
        return None
    value = raw.strip().replace(" ", "")
    sign = 1
    if value.startswith("-"):
        sign = -1
        value = value[1:]
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return Decimal(value) * sign
    except InvalidOperation:
        return None


def extract_amount(text: str) -> Decimal | None:
    if not text:
        return None
    candidates = re.findall(r"(?:r\$|rs|reais)?\s*(-?\d+(?:[\.,]\d{3})*(?:[\.,]\d{2})?)", text, flags=re.IGNORECASE)
    if not candidates:
        return None
    amounts = [_clean_amount(item) for item in candidates]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        return None
    return amounts[-1]


def extract_date(text: str) -> dt.date:
    now = timezone.localdate()
    normalized = normalize_text(text)
    if "hoje" in normalized:
        return now
    if "ontem" in normalized:
        return now - dt.timedelta(days=1)
    if "amanha" in normalized:
        return now + dt.timedelta(days=1)

    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", normalized)
    if match:
        return dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", normalized)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"dia\s+(\d{1,2})", normalized)
    if match:
        day = int(match.group(1))
        month = now.month
        year = now.year
        last_day = calendar.monthrange(year, month)[1]
        day = min(day, last_day)
        return dt.date(year, month, day)
    return now


def infer_type(text: str) -> str:
    normalized = normalize_text(text)
    is_income = any(keyword in normalized for keyword in INCOME_KEYWORDS)
    is_expense = any(keyword in normalized for keyword in EXPENSE_KEYWORDS)
    if is_income and not is_expense:
        return "income"
    if is_expense and not is_income:
        return "expense"
    if "salario" in normalized or "receb" in normalized:
        return "income"
    return "expense"


def extract_description(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"(r\$|rs)\s*\d[\d\.,]+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d{2}/\d{2}/\d{4}", "", cleaned)
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", cleaned)
    cleaned = re.sub(r"\bhoje\b|\bontem\b|\bamanha\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned or text.strip()


def _extract_steps(text: str) -> tuple[str, list[str]]:
    if not text:
        return "", []
    pattern = re.compile(r"(?:etapa|passo)\s*\d*\s*[:\-]", re.IGNORECASE)
    parts = pattern.split(text)
    if len(parts) <= 1:
        return text, []
    steps = []
    for part in parts[1:]:
        step_title = part.strip(" \t\n\r.;,-")
        if step_title:
            steps.append(step_title)
    return parts[0], steps


def _extract_task_category(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"categoria\s+([^,;\n]+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip().title()


def _clean_task_title(text: str) -> str:
    cleaned = text or ""
    cleaned = re.sub(r"\b(tarefa|task|lembrete|lembrar)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(prioridade|prazo|data|ate)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"categoria\s+[^,;\n]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d{2}/\d{2}/\d{4}", "", cleaned)
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", cleaned)
    cleaned = re.sub(r"\bhoje\b|\bontem\b|\bamanha\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned


def is_task_intent(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if normalized.startswith("/tarefa") or normalized.startswith("/task"):
        return True
    return any(keyword in normalized for keyword in TASK_KEYWORDS)


def parse_task_text(text: str) -> ParsedTaskResult | None:
    if not is_task_intent(text):
        return None
    due_date = extract_date(text)
    preface, steps = _extract_steps(text)
    category = _extract_task_category(text)
    title = _clean_task_title(preface)
    if not title:
        title = "Tarefa via WhatsApp"
    return ParsedTaskResult(
        title=title,
        due_date=due_date,
        category=category,
        steps=steps,
    )


def ensure_category(workspace, name: str) -> Category:
    existing = _match_existing_category(workspace, name)
    if existing:
        return existing
    color = DEFAULT_CATEGORY_COLORS.get(name, "#94a3b8")
    obj, _ = Category.objects.get_or_create(workspace=workspace, name=name, defaults={"color": color})
    return obj


def ensure_default_categories(workspace) -> None:
    if not _create_missing_categories():
        return
    for name in DEFAULT_CATEGORY_LIST:
        ensure_category(workspace, name)


def suggest_category(user, workspace, text: str) -> tuple[Category | None, str]:
    normalized = normalize_text(text)
    prefs = CategoryPreference.objects.filter(user=user, workspace=workspace)
    for pref in prefs.order_by("-usage_count"):
        if normalize_text(pref.keyword) in normalized:
            return pref.category, pref.category.name

    for keyword, category_label in DEFAULT_CATEGORY_MAP.items():
        if keyword in normalized:
            if _create_missing_categories():
                category = ensure_category(workspace, category_label)
            else:
                category = _match_existing_category(workspace, category_label)
            return category, category_label

    for category in Category.objects.filter(workspace=workspace):
        if normalize_text(category.name) in normalized:
            return category, category.name
    return None, ""


def _create_missing_categories() -> bool:
    return getattr(settings, "WHATSAPP_CREATE_MISSING_CATEGORIES", True)


def _confirmation_threshold() -> Decimal:
    return Decimal(str(getattr(settings, "WHATSAPP_CONFIRMATION_THRESHOLD", 1000)))


def parse_text_to_result(user, workspace, text: str) -> ParsedResult | None:
    amount = extract_amount(text)
    if amount is None:
        return None
    tx_type = infer_type(text)
    tx_date = extract_date(text)
    description = extract_description(text)
    category, category_label = suggest_category(user, workspace, text)
    requires_confirmation = abs(amount) >= _confirmation_threshold()
    return ParsedResult(
        description=description,
        amount=abs(amount),
        tx_type=tx_type,
        tx_date=tx_date,
        category=category,
        category_label=category_label,
        requires_confirmation=requires_confirmation,
    )


def _record_ai_usage(user, workspace, usage, model_name: str | None) -> None:
    if not usage:
        return
    total = int(usage.get("total_tokens") or 0)
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if total <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return
    cost_usd, cost_brl = estimate_costs(usage)
    AiUsage.objects.create(
        user=user,
        workspace=workspace,
        feature="whatsapp",
        model_name=model_name or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cost_usd=cost_usd,
        cost_brl=cost_brl,
    )


def _extract_json_payload(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def _parse_llm_date(value) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except Exception:
            continue
    return None


def _llm_fallback_parse(user, workspace, text: str) -> tuple[ParsedResult | None, ParsedTaskResult | None, str | None]:
    quota = get_ai_quota(user)
    if not quota.get("allowed", True):
        return None, None, "Limite de IA atingido. Reescreva informando valor e data."
    categories = list(Category.objects.filter(workspace=workspace).values_list("name", flat=True))
    categories_label = ", ".join(categories[:30]) if categories else "nenhuma"
    system_prompt = (
        "Você extrai dados de uma mensagem do WhatsApp para o iTracker. "
        "Use SOMENTE categorias da lista fornecida; se nenhuma servir, deixe category vazio. "
        "Responda APENAS em JSON com as chaves: "
        "intent (transaction|task|unknown), "
        "description, amount, date (YYYY-MM-DD), type (income|expense), category, "
        "confidence (0-1), needs_confirmation (true/false), "
        "task_title, task_due_date (YYYY-MM-DD), task_steps (array), task_category."
    )
    user_prompt = (
        f"Categorias disponíveis: {categories_label}\n"
        f"Mensagem: {text}"
    )
    reply, usage, model_name = llm_complete(system_prompt, user_prompt)
    _record_ai_usage(user, workspace, usage, model_name)
    if not reply:
        return None, None, "Nao consegui interpretar. Envie o valor e a data."
    payload = _extract_json_payload(reply)
    if not payload:
        return None, None, "Nao consegui interpretar. Envie o valor e a data."

    intent = (payload.get("intent") or "").lower().strip()
    if intent == "task":
        title = (payload.get("task_title") or "").strip()
        if not title:
            title = "Tarefa via WhatsApp"
        due_date = _parse_llm_date(payload.get("task_due_date")) or extract_date(text)
        steps = payload.get("task_steps") or []
        if isinstance(steps, str):
            steps = [s.strip() for s in steps.split(";") if s.strip()]
        category = (payload.get("task_category") or "").strip().title()
        return None, ParsedTaskResult(title=title, due_date=due_date, category=category, steps=steps), None

    if intent in ("transaction", "unknown", ""):
        raw_amount = payload.get("amount")
        amount = None
        if isinstance(raw_amount, (int, float, Decimal)):
            amount = Decimal(str(raw_amount))
        else:
            amount = _clean_amount(str(raw_amount)) if raw_amount is not None else None
        if amount is None:
            return None, None, "Nao consegui identificar o valor. Envie novamente com o valor da transacao."
        tx_type = (payload.get("type") or "").strip().lower()
        if tx_type not in ("income", "expense"):
            tx_type = infer_type(text)
        tx_date = _parse_llm_date(payload.get("date")) or extract_date(text)
        description = (payload.get("description") or "").strip() or extract_description(text)
        category_name = (payload.get("category") or "").strip()
        category = None
        category_label = ""
        if category_name:
            category = _match_existing_category(workspace, category_name)
            category_label = category.name if category else category_name
        else:
            category, category_label = suggest_category(user, workspace, text)
        if not category:
            category, category_label = suggest_category(user, workspace, text)
        confidence = payload.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else 0.0
        except Exception:
            confidence = 0.0
        needs_confirmation = confidence < 0.6
        if not category:
            return None, None, "Nao consegui identificar a categoria. Use uma das seguintes: " + ", ".join(categories[:8])
        return (
            ParsedResult(
                description=description,
                amount=abs(amount),
                tx_type=tx_type,
                tx_date=tx_date,
                category=category,
                category_label=category_label,
                requires_confirmation=needs_confirmation,
            ),
            None,
            None,
        )
    return None, None, "Nao consegui interpretar. Envie o valor e a data."


def _format_currency(value: Decimal) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_date(value: dt.date) -> str:
    today = timezone.localdate()
    if value == today:
        return "hoje"
    if value == today - dt.timedelta(days=1):
        return "ontem"
    return value.strftime("%d/%m/%Y")


def _can_create_task(user, workspace) -> bool:
    if not workspace:
        return False
    if user.is_superuser or workspace.owner_id == user.id:
        return True
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
    return bool(membership and membership.can_edit_tasks)


def _create_task_from_parsed(user, workspace, parsed: ParsedTaskResult) -> Task:
    responsible_name = user.get_full_name() or user.username
    task = Task.objects.create(
        title=parsed.title,
        category=parsed.category or "",
        due_date=parsed.due_date,
        workspace=workspace,
        responsible_user=user,
        responsible=responsible_name,
        responsible_email=user.email or "",
        status="ongoing",
    )
    if parsed.steps:
        for idx, step_title in enumerate(parsed.steps, start=1):
            TaskStep.objects.create(
                task=task,
                title=step_title,
                order=idx,
                responsible=responsible_name,
                responsible_email=user.email or "",
                status="ongoing",
            )
    return task


def build_task_message(task: Task, steps_count: int) -> str:
    due_label = _format_date(task.due_date)
    category = task.category or "Sem categoria"
    return (
        f"Tarefa criada: {task.title}. Prazo: {due_label}. "
        f"Categoria: {category}. Etapas: {steps_count}."
    )


def build_confirmation_message(parsed: ParsedTransaction) -> str:
    category_label = parsed.category.name if parsed.category else parsed.category_label or "Sem categoria"
    return (
        f"✅ Entendido! {('Receita' if parsed.type == 'income' else 'Despesa')} de {_format_currency(parsed.amount)} "
        f"em '{category_label}'. Data: {_format_date(parsed.date)}. Confirmar? (sim/nao)"
    )


def build_confirmed_message(workspace, amount: Decimal, tx_type: str) -> str:
    totals = Transaction.objects.filter(workspace=workspace).aggregate(
        income=Sum('value', filter=Q(type='income')),
        expense=Sum('value', filter=Q(type='expense')),
    )
    income = totals.get('income') or Decimal("0")
    expense = totals.get('expense') or Decimal("0")
    balance = income - expense
    return (
        f"📊 Lancamento registrado! "
        f"{('Entrada' if tx_type == 'income' else 'Saida')} de {_format_currency(amount)}. "
        f"Saldo atual: {_format_currency(balance)}"
    )


def _last_pending(user, workspace) -> ParsedTransaction | None:
    return (
        ParsedTransaction.objects.filter(user=user, workspace=workspace, status='pending')
        .order_by('-created_at')
        .first()
    )


def _create_transaction_from_parsed(parsed: ParsedTransaction) -> Transaction:
    category = parsed.category
    if not category:
        fallback = "Receitas" if parsed.type == "income" else "Gastos diversos"
        category = ensure_category(parsed.workspace, fallback)
    tx = Transaction.objects.create(
        description=parsed.description,
        date=parsed.date,
        category=category,
        value=parsed.amount,
        type=parsed.type,
        workspace=parsed.workspace,
        responsible=parsed.user,
    )
    parsed.status = 'confirmed'
    parsed.transaction = tx
    parsed.save(update_fields=['status', 'transaction'])
    return tx


def handle_confirmation(user, workspace, text: str) -> str | None:
    normalized = normalize_text(text)
    if normalized not in ("sim", "nao", "não"):
        return None
    parsed = _last_pending(user, workspace)
    if not parsed:
        return "Nao encontrei nenhum lancamento pendente para confirmar."
    if normalized in ("nao", "não"):
        parsed.status = 'rejected'
        parsed.save(update_fields=['status'])
        return "Ok! Lancamento descartado."
    tx = _create_transaction_from_parsed(parsed)
    return build_confirmed_message(workspace, tx.value, tx.type)


def handle_correction(user, workspace, text: str) -> str | None:
    normalized = normalize_text(text)
    if "corrige" not in normalized and "corrigir" not in normalized:
        return None
    match = re.search(r"categoria\s+(.+)$", normalized)
    if not match:
        return "Informe a categoria para corrigir. Ex.: corrige ultimo lancamento para categoria Alimentacao."
    category_name = match.group(1).strip().title()
    category = ensure_category(workspace, category_name)
    parsed = (
        ParsedTransaction.objects.filter(user=user, workspace=workspace, transaction__isnull=False)
        .order_by('-created_at')
        .first()
    )
    if not parsed or not parsed.transaction:
        return "Nao encontrei um lancamento recente para corrigir."
    parsed.transaction.category = category
    parsed.transaction.save(update_fields=['category'])
    parsed.status = 'corrected'
    parsed.category = category
    parsed.save(update_fields=['status', 'category'])
    keyword = normalize_text(parsed.description).split(" ")[0] if parsed.description else category_name.lower()
    pref, _ = CategoryPreference.objects.get_or_create(user=user, workspace=workspace, keyword=keyword, defaults={'category': category})
    if pref.category_id != category.id:
        pref.category = category
    pref.usage_count = pref.usage_count + 1
    pref.save(update_fields=['category', 'usage_count', 'updated_at'])
    return f"Categoria ajustada para {category.name}."


def handle_delete(user, workspace, text: str) -> str | None:
    normalized = normalize_text(text)
    if "remove" not in normalized and "excluir" not in normalized:
        return None
    amount = extract_amount(text)
    tx_date = extract_date(text)
    qs = Transaction.objects.filter(workspace=workspace, responsible=user)
    if amount is not None:
        qs = qs.filter(value=abs(amount))
    if tx_date:
        qs = qs.filter(date=tx_date)
    tx = qs.order_by('-created_at').first()
    if not tx:
        return "Nao encontrei nenhum lancamento com esses dados."
    tx.delete()
    return f"Lancamento removido: {_format_currency(abs(tx.value))} em {tx.date.strftime('%d/%m/%Y')}."


def _command_summary(workspace) -> str:
    today = timezone.localdate()
    start = today.replace(day=1)
    end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    totals = Transaction.objects.filter(workspace=workspace, date__range=(start, end)).aggregate(
        income=Sum('value', filter=Q(type='income')),
        expense=Sum('value', filter=Q(type='expense')),
    )
    income = totals.get('income') or Decimal("0")
    expense = totals.get('expense') or Decimal("0")
    balance = income - expense
    return (
        f"Resumo do mes: Entradas {_format_currency(income)} | Saidas {_format_currency(expense)} | "
        f"Saldo {_format_currency(balance)}."
    )


def _command_categories(workspace) -> str:
    categories = Category.objects.filter(workspace=workspace).order_by('name')[:20]
    names = ", ".join(cat.name for cat in categories) or "Nenhuma categoria cadastrada."
    return f"Categorias disponiveis: {names}."


def _parse_period(text: str) -> tuple[dt.date, dt.date] | None:
    normalized = normalize_text(text)
    match = re.findall(r"(\d{2})/(\d{2})/(\d{4})", normalized)
    if len(match) >= 2:
        start = dt.date(int(match[0][2]), int(match[0][1]), int(match[0][0]))
        end = dt.date(int(match[1][2]), int(match[1][1]), int(match[1][0]))
        return start, end
    match = re.findall(r"(\d{4})-(\d{2})-(\d{2})", normalized)
    if len(match) >= 2:
        start = dt.date(int(match[0][0]), int(match[0][1]), int(match[0][2]))
        end = dt.date(int(match[1][0]), int(match[1][1]), int(match[1][2]))
        return start, end
    return None


def _command_statement(workspace, text: str) -> str:
    period = _parse_period(text)
    if period:
        start, end = period
    else:
        today = timezone.localdate()
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    qs = Transaction.objects.filter(workspace=workspace, date__range=(start, end)).order_by('-date')[:8]
    if not qs:
        return "Nenhuma transacao no periodo."
    lines = [f"{tx.date.strftime('%d/%m')}: {_format_currency(tx.value)} - {tx.description}" for tx in qs]
    return "Extrato:\\n" + "\\n".join(lines)


def handle_command(workspace, text: str) -> str | None:
    normalized = normalize_text(text)
    if not normalized.startswith("/"):
        return None
    if normalized.startswith("/resumo"):
        return _command_summary(workspace)
    if normalized.startswith("/categorias"):
        return _command_categories(workspace)
    if normalized.startswith("/extrato"):
        return _command_statement(workspace, text)
    return "Comando nao reconhecido. Use /resumo, /categorias ou /extrato."


def process_incoming_text(profile: WhatsAppProfile, message: WhatsAppMessage, text: str) -> str:
    user = profile.user
    workspace = profile.workspace
    ensure_default_categories(workspace)

    command_response = handle_command(workspace, text)
    if command_response:
        return command_response

    confirmation_response = handle_confirmation(user, workspace, text)
    if confirmation_response:
        return confirmation_response

    correction_response = handle_correction(user, workspace, text)
    if correction_response:
        return correction_response

    delete_response = handle_delete(user, workspace, text)
    if delete_response:
        return delete_response

    task_result = parse_task_text(text)
    if task_result:
        if not _can_create_task(user, workspace):
            return "Voce nao tem permissao para criar tarefas neste workspace."
        task = _create_task_from_parsed(user, workspace, task_result)
        return build_task_message(task, len(task_result.steps))

    result = parse_text_to_result(user, workspace, text)
    if not result:
        llm_result, llm_task, llm_error = _llm_fallback_parse(user, workspace, text)
        if llm_task:
            if not _can_create_task(user, workspace):
                return "Voce nao tem permissao para criar tarefas neste workspace."
            task = _create_task_from_parsed(user, workspace, llm_task)
            return build_task_message(task, len(llm_task.steps))
        if llm_result:
            result = llm_result
        else:
            return llm_error or "Nao consegui identificar o valor. Envie novamente com o valor da transacao."

    with db_transaction.atomic():
        parsed = ParsedTransaction.objects.create(
            user=user,
            workspace=workspace,
            source_message=message,
            description=result.description,
            amount=result.amount,
            type=result.tx_type,
            date=result.tx_date,
            category=result.category,
            category_label=result.category_label,
            requires_confirmation=result.requires_confirmation,
            status='pending',
        )

    auto_confirm = getattr(settings, "WHATSAPP_AUTO_CONFIRM_BELOW_THRESHOLD", False)
    if auto_confirm and not result.requires_confirmation:
        tx = _create_transaction_from_parsed(parsed)
        return build_confirmed_message(workspace, tx.value, tx.type)

    return build_confirmation_message(parsed)
