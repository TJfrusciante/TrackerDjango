from __future__ import annotations

import calendar
import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Sum, Q
from django.utils import timezone

from tracker.models import Category, Transaction
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
    candidates = re.findall(r"(?:r\\$|rs|reais)?\\s*(-?\\d+(?:[\\.,]\\d{3})*(?:[\\.,]\\d{2})?)", text, flags=re.IGNORECASE)
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

    match = re.search(r"(\\d{2})/(\\d{2})/(\\d{4})", normalized)
    if match:
        return dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    match = re.search(r"(\\d{4})-(\\d{2})-(\\d{2})", normalized)
    if match:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"dia\\s+(\\d{1,2})", normalized)
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
    cleaned = re.sub(r"(r\\$|rs)\\s*\\d[\\d\\.,]+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\d{2}/\\d{2}/\\d{4}", "", cleaned)
    cleaned = re.sub(r"\\d{4}-\\d{2}-\\d{2}", "", cleaned)
    cleaned = re.sub(r"\\bhoje\\b|\\bontem\\b|\\bamanha\\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned or text.strip()


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


def _format_currency(value: Decimal) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _format_date(value: dt.date) -> str:
    today = timezone.localdate()
    if value == today:
        return "hoje"
    if value == today - dt.timedelta(days=1):
        return "ontem"
    return value.strftime("%d/%m/%Y")


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
    match = re.search(r"categoria\\s+(.+)$", normalized)
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
    match = re.findall(r"(\\d{2})/(\\d{2})/(\\d{4})", normalized)
    if len(match) >= 2:
        start = dt.date(int(match[0][2]), int(match[0][1]), int(match[0][0]))
        end = dt.date(int(match[1][2]), int(match[1][1]), int(match[1][0]))
        return start, end
    match = re.findall(r"(\\d{4})-(\\d{2})-(\\d{2})", normalized)
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

    result = parse_text_to_result(user, workspace, text)
    if not result:
        return "Nao consegui identificar o valor. Envie novamente com o valor da transacao."

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
