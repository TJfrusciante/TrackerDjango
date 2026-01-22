import calendar
import datetime
import calendar
import logging
import os
import re
import unicodedata

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Case, When, DecimalField, F, Prefetch
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt

from tracker.models import Category, Transaction, Task, TaskStep, WorkspaceMembership
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


def _finance_queryset(request, workspace):
    qs = Transaction.objects.select_related('category')
    if workspace:
        if request.user.is_superuser or workspace.owner_id == request.user.id:
            return qs.filter(workspace=workspace)
        return qs.none()
    if request.user.is_superuser:
        return qs
    return qs.none()


def _tasks_queryset(request, workspace):
    qs = Task.objects.all()
    if workspace:
        return qs.filter(workspace=workspace)
    if request.user.is_superuser:
        return qs
    workspace_ids = WorkspaceMembership.objects.filter(user=request.user).values_list('workspace_id', flat=True)
    return qs.filter(workspace_id__in=workspace_ids)


def _parse_month_range(message: str):
    if not message:
        return None
    normalized = _normalize_text(message)
    today = timezone.localdate()

    month = None
    year = None

    if 'mes passado' in normalized or 'ultimo mes' in normalized:
        month = today.month - 1
        year = today.year
        if month <= 0:
            month = 12
            year -= 1
    if 'este mes' in normalized or 'mes atual' in normalized:
        month = today.month
        year = today.year

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
        return None

    if year is None:
        year = today.year
        if month > today.month:
            year -= 1

    start_date = datetime.date(year, month, 1)
    end_date = datetime.date(year, month, calendar.monthrange(year, month)[1])
    label = f'{month:02d}/{year}'
    return start_date, end_date, label


def _parse_year_range(message: str):
    if not message:
        return None
    normalized = _normalize_text(message)
    match = re.search(r'\b(20\d{2})\b', normalized)
    if not match:
        return None
    year = int(match.group(1))
    start_date = datetime.date(year, 1, 1)
    end_date = datetime.date(year, 12, 31)
    return start_date, end_date, str(year)


def _summarize_period(workspace, request, start_date, end_date, label: str, include_tasks: bool, include_finance: bool) -> str:
    lines = [f"Resumo de {label} ({start_date:%d/%m/%Y} a {end_date:%d/%m/%Y})"]

    if include_finance:
        finance_qs = _finance_queryset(request, workspace).filter(date__gte=start_date, date__lte=end_date)
        income = finance_qs.filter(type='income').aggregate(total=Sum('value'))['total'] or 0
        expense = finance_qs.filter(type='expense').aggregate(total=Sum('value'))['total'] or 0
        balance = income - expense
        tx_count = finance_qs.count()
        lines.append('')
        lines.append('Financeiro:')
        lines.append(f'- Transa\u00e7\u00f5es: {tx_count}')
        lines.append(f'- Entradas: R$ {income:.2f}')
        lines.append(f'- Sa\u00eddas: R$ {expense:.2f}')
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
                '- Top categorias (saldo l\u00edquido): '
                + ', '.join([f"{c['category__name']} ({c['total']:+.2f})" for c in top])
            )

    if include_tasks:
        tasks_qs = _tasks_queryset(request, workspace)
        tasks_due = tasks_qs.filter(due_date__gte=start_date, due_date__lte=end_date)
        tasks_done = tasks_qs.filter(status='done', completed_at__date__gte=start_date, completed_at__date__lte=end_date)
        tasks_created = tasks_qs.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
        lines.append('')
        lines.append('Tarefas:')
        lines.append(f'- Criadas no per\u00edodo: {tasks_created.count()}')
        lines.append(f'- Conclu\u00eddas no per\u00edodo: {tasks_done.count()}')
        lines.append(f'- Com prazo no per\u00edodo: {tasks_due.count()}')

    return "\n".join(lines)


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
            "Top categorias (saldo l\u00edquido): "
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
    return 'tarefa' in msg or 'tarefas' in msg


def _task_list_summary(workspace, request, status: str | None = None, limit_tasks: int = 6, limit_steps: int = 6) -> str:
    steps_prefetch = Prefetch('steps', queryset=TaskStep.objects.order_by('order', 'created_at'))
    tasks_qs = _tasks_queryset(request, workspace).order_by('-due_date', '-created_at').prefetch_related(steps_prefetch)
    status_label = None
    if status:
        tasks_qs = tasks_qs.filter(status=status)
        status_label = 'em andamento' if status == 'ongoing' else 'concluídas'
    total = tasks_qs.count()
    if total == 0:
        if status_label:
            return f"Nenhuma tarefa {status_label} no momento."
        return "Nenhuma tarefa encontrada no momento."

    header = f"Tarefas {status_label} ({total}):" if status_label else f"Tarefas ({total}):"
    lines = [header]
    for idx, task in enumerate(tasks_qs[:limit_tasks], start=1):
        due_label = task.due_date.strftime('%d/%m/%Y') if task.due_date else '-'
        lines.append(f"{idx}. {task.title} (prazo {due_label})")
        steps = list(task.steps.all())
        if steps:
            lines.append("   - Etapas:")
            for step in steps[:limit_steps]:
                lines.append(f"     - {step.title} ({step.get_status_display()})")
        else:
            lines.append("   - Etapas: nenhuma cadastrada")
    if total > limit_tasks:
        lines.append(f"Mostrando {limit_tasks} de {total} tarefas.")
    return "\n".join(lines)


def _help_response(message: str) -> str | None:
    normalized = _normalize_text(message or '')
    if not normalized:
        return None

    def has_any(keys):
        return any(k in normalized for k in keys)

    wants_howto = has_any(['como', 'ajuda', 'onde', 'posso', 'faco', 'fazer', 'adicionar', 'lancar', 'lan?ar'])

    if has_any(['transa', 'entrada', 'saida', 'despesa', 'receita']):
        return (
            "Para lan\u00e7ar transa\u00e7\u00f5es: v\u00e1 em Transa\u00e7\u00f5es > Nova. Informe descri\u00e7\u00e3o, data, "
            "valor, categoria e tipo (entrada/sa\u00edda) e salve. "
            "Voc\u00ea tamb\u00e9m pode usar o bot\u00e3o Falar para preencher por voz. "
            "Para importar em lote, use Transa\u00e7\u00f5es > Importar CSV/PDF."
        ) if wants_howto else None

    if has_any(['tarefa', 'etapa', 'prazo']):
        return (
            "Para criar tarefas: v\u00e1 em Tarefas > Nova, defina t\u00edtulo, prazo e categoria. "
            "Voc\u00ea pode adicionar etapas no mesmo formul\u00e1rio e depois editar/atualizar o status. "
            "O bot\u00e3o Falar ajuda a preencher a tarefa e as etapas."
        ) if wants_howto else None

    if has_any(['workspace', 'membro', 'convidar', 'convite']):
        return (
            "Para convidar pessoas: abra Workspaces > Convidar e informe o usu\u00e1rio. "
            "O convidado precisa aceitar o convite para entrar. "
            "Voc\u00ea pode ver membros em Workspaces > Membros."
        ) if wants_howto else None

    if has_any(['categoria', 'cor']):
        return (
            "As categorias ficam em Categorias. Voc\u00ea pode criar, editar e escolher cor. "
            "A cor aparece nos gr\u00e1ficos do dashboard."
        ) if wants_howto else None

    if has_any(['exportar', 'csv', 'importar', 'pdf']):
        return (
            "Exporta\u00e7\u00e3o: nas listas de Transa\u00e7\u00f5es ou Tarefas, use o bot\u00e3o Exportar CSV. "
            "Importa\u00e7\u00e3o: use Transa\u00e7\u00f5es > Importar CSV ou Importar PDF."
        ) if wants_howto else None

    if has_any(['notificacao', 'resumo', 'semanal', 'mensal', 'alerta']):
        return (
            "Notifica\u00e7\u00f5es e resumos ficam no Perfil. "
            "Marque as op\u00e7\u00f5es de alertas, tarefas e resumos semanais/mensais e salve."
        ) if wants_howto else None

    if has_any(['ia', 'agente', 'chat', 'modelo']):
        return (
            "O Agente de IA responde com base nos seus dados do workspace. "
            "Use o bot\u00e3o Falar no chat ou nos formul\u00e1rios para ditado por voz."
        ) if wants_howto else None

    if wants_howto:
        return "Voc\u00ea pode consultar a p\u00e1gina Ajuda no menu para um passo a passo completo."

    return None


def _task_detail_summary(workspace, request, limit_tasks: int = 5, limit_steps: int = 6) -> str:
    tasks_qs = _tasks_queryset(request, workspace)
    steps_prefetch = Prefetch('steps', queryset=TaskStep.objects.order_by('order', 'created_at'))
    done_tasks = tasks_qs.filter(status='done').order_by('-completed_at', '-updated_at').prefetch_related(steps_prefetch)
    total_done = done_tasks.count()
    if total_done == 0:
        return "Nenhuma tarefa conclu\u00edda no momento."

    lines = [f"Tarefas conclu\u00eddas ({total_done}):"]
    for idx, task in enumerate(done_tasks[:limit_tasks], start=1):
        completed_date = task.completed_at.date() if task.completed_at else None
        completed_label = completed_date.strftime('%d/%m/%Y') if completed_date else '-'
        due_label = task.due_date.strftime('%d/%m/%Y') if task.due_date else '-'
        status_label = 'no prazo'
        if completed_date and task.due_date and completed_date > task.due_date:
            status_label = 'com atraso'
        lines.append(f"{idx}. {task.title}")
        lines.append(f"   - Conclu\u00edda em: {completed_label}")
        lines.append(f"   - Prazo: {due_label}")
        lines.append(f"   - Status: {status_label}")
        steps = list(task.steps.all())
        if steps:
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
    msg_norm = _normalize_text(message or '')
    period_range = _parse_month_range(message)
    year_range = _parse_year_range(message) if not period_range else None

    wants_tasks = 'tarefa' in msg_norm or 'tarefas' in msg_norm
    wants_steps = 'etapa' in msg_norm or 'etapas' in msg_norm
    wants_ongoing = any(key in msg_norm for key in ['em andamento', 'andamento', 'pendente', 'pendentes', 'aberta', 'abertas'])
    wants_done = any(key in msg_norm for key in ['concluida', 'concluidas', 'finalizada', 'finalizadas'])

    if wants_tasks and (wants_ongoing or wants_steps):
        return _task_list_summary(workspace, request, status='ongoing'), None, None
    if wants_tasks and wants_done:
        return _task_list_summary(workspace, request, status='done'), None, None

    if any(key in msg_norm for key in ['maior gasto', 'maior despesa', 'maior saida']):
        finance_qs = _finance_queryset(request, workspace)
        if period_range:
            start_date, end_date, label = period_range
            finance_qs = finance_qs.filter(date__gte=start_date, date__lte=end_date)
        top = _top_category(finance_qs, 'expense')
        if not top:
            period_label = f" no per\u00edodo {label}" if period_range else ""
            return f"N\u00e3o encontrei despesas registradas{period_label}.", None, None
        name, total = top
        period_label = f" no per\u00edodo {label}" if period_range else ""
        return f"Seu maior gasto{period_label} foi em {name}: R$ {total:.2f}.", None, None

    if any(key in msg_norm for key in ['maior receita', 'maior entrada']):
        finance_qs = _finance_queryset(request, workspace)
        if period_range:
            start_date, end_date, label = period_range
            finance_qs = finance_qs.filter(date__gte=start_date, date__lte=end_date)
        top = _top_category(finance_qs, 'income')
        if not top:
            period_label = f" no per\u00edodo {label}" if period_range else ""
            return f"N\u00e3o encontrei receitas registradas{period_label}.", None, None
        name, total = top
        period_label = f" no per\u00edodo {label}" if period_range else ""
        return f"Sua maior receita{period_label} foi em {name}: R$ {total:.2f}.", None, None

    if period_range or year_range:
        if period_range:
            start_date, end_date, label = period_range
        else:
            start_date, end_date, label = year_range
        wants_tasks = 'tarefa' in msg_norm
        wants_finance = any(
            key in msg_norm for key in ['transa', 'financ', 'saldo', 'entrada', 'saida', 'despesa', 'receita']
        )
        include_tasks = wants_tasks or not wants_finance
        include_finance = wants_finance or not wants_tasks
        return _summarize_period(
            workspace, request, start_date, end_date, label, include_tasks, include_finance
        ), None, None

    help_text = _help_response(message)
    if help_text:
        return help_text, None, None

    if _wants_task_details(message):
        return _task_detail_summary(workspace, request), None, None

    summary = _summarize(workspace, request)
    today = datetime.date.today().strftime("%d/%m/%Y")
    ws_name = workspace.name if workspace else "global"
    context_line = f"workspace={ws_name}; usuario={'superuser' if request.user.is_superuser else request.user.username}"

    system_prompt = (
        "Voc\u00ea \u00e9 o assistente financeiro do iTracker. Responda em portugu\u00eas de forma curta. "
        "Use apenas os dados fornecidos no resumo. Se algo n\u00e3o estiver no resumo, diga que n\u00e3o sabe. "
        f"Contexto de workspace: {context_line}."
    )
    user_prompt = f"Data atual: {today}. Resumo dispon\u00edvel:\n{summary}\nPergunta: {message}"

    if not local_only:
        reply, usage, model_name = llm_complete(system_prompt, user_prompt)
        if reply:
            return reply, usage, model_name
    logger.info("LLM fallback usado (local_only=%s)", local_only)
    prefix = fallback_reason or "N\u00e3o consegui consultar o modelo agora. Aqui vai um resumo r\u00e1pido:"
    return f"{prefix}\n{summary}", None, None

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


@login_required
def chat(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest and not request.user.is_superuser:
        messages.error(request, "Conta de convidado n\u00e3o tem acesso ao agente de IA.")
        return redirect('tracker:tasks_list')
    quota = get_ai_quota(request.user)
    quota_label = _format_quota_label(quota)
    quota_blocked = not quota.get("allowed", True)
    if request.user.is_superuser and workspace is None:
        # se superuser estiver em modo global, nao mostrar historico para evitar volume; nao bloqueia
        pass
    history = _history_for_workspace(request.user, workspace, 30)

    if request.method == 'POST':
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
            try:
                reply, usage, model_name = _assistant_reply(
                    content,
                    workspace,
                    request,
                    local_only=local_only,
                    fallback_reason=fallback_reason,
                )
            except Exception:
                logger.exception("Erro ao gerar resposta do assistente")
                reply, usage, model_name = (
                    "Tive um problema ao gerar a resposta agora. Tente novamente em instantes.",
                    None,
                    None,
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
    quota = get_ai_quota(request.user)
    quota_label = _format_quota_label(quota)
    quota_blocked = not quota.get("allowed", True)
    history = _history_for_workspace(request.user, workspace, 20)

    if request.method == 'POST':
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
            try:
                reply, usage, model_name = _assistant_reply(
                    content,
                    workspace,
                    request,
                    local_only=local_only,
                    fallback_reason=fallback_reason,
                )
            except Exception:
                logger.exception("Erro ao gerar resposta do assistente (embed)")
                reply, usage, model_name = (
                    "Tive um problema ao gerar a resposta agora. Tente novamente em instantes.",
                    None,
                    None,
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






