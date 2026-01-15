import datetime
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Case, When, DecimalField, F
from django.shortcuts import redirect, render
from django.views.decorators.clickjacking import xframe_options_exempt

from tracker.models import Category, Transaction, Task, TaskStep
from tracker.metrics import record_metric
from .models import ChatMessage, AiUsage
from .services import llm_complete, estimate_costs

logger = logging.getLogger(__name__)


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
            "Top categorias (net): "
            + ", ".join([f"{c['category__name']} ({c['total']:+.2f})" for c in top])
        )
    return "\n".join(summary_lines)


def _wants_task_details(message: str) -> bool:
    msg = (message or '').lower()
    if not msg:
        return False
    return 'tarefa' in msg or 'tarefas' in msg


def _task_detail_summary(workspace, request, limit_tasks: int = 5, limit_steps: int = 6) -> str:
    tasks_qs = _tasks_queryset(request, workspace)
    done_tasks = tasks_qs.filter(status='done').order_by('-completed_at', '-updated_at')
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
        steps = TaskStep.objects.filter(task=task).order_by('order', 'created_at')
        if steps.exists():
            lines.append("   - Etapas:")
            for step in steps[:limit_steps]:
                responsible = step.responsible or '-'
                email = step.responsible_email or '-'
                order_label = step.order if step.order else '?'
                lines.append(
                    f"     • Etapa {order_label}: {step.title} ({step.get_status_display()})"
                )
                lines.append(f"       Respons\u00e1vel: {responsible} | {email}")
        else:
            lines.append("   - Etapas: nenhuma cadastrada")
    if total_done > limit_tasks:
        lines.append(f"Mostrando {limit_tasks} de {total_done} tarefas conclu\u00eddas.")
    return "\n".join(lines)


def _assistant_reply(message, workspace, request, local_only: bool = False):
    """
    Usa LLM se configurado; fallback para resumo baseado em regras.
    Retorna (reply, usage, model_name).
    """
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
    return f"N\u00e3o consegui consultar o modelo agora. Aqui vai um resumo r\u00e1pido:\n{summary}", None, None


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


@login_required
def chat(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest and not request.user.is_superuser:
        messages.error(request, "Conta de convidado n\u00e3o tem acesso ao agente de IA.")
        return redirect('tracker:tasks_list')
    if request.user.is_superuser and workspace is None:
        # se superuser estiver em modo global, nao mostrar historico para evitar volume; nao bloqueia
        pass
    history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:30][::-1]

    if request.method == 'POST':
        content = (request.POST.get('message') or '').strip()
        local_only = request.POST.get('local_only') == 'on' or not os.getenv("OPENAI_API_KEY")
        if content:
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='user', content=content)
            reply, usage, model_name = _assistant_reply(content, workspace, request, local_only=local_only)
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='assistant', content=reply)
            _record_usage(request.user, workspace, 'chat', usage, model_name)
            record_metric('ai_request', user=request.user, workspace=workspace, metadata={'local_only': local_only})
        history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:30][::-1]

    return render(request, 'assistant/chat.html', {
        'history': history,
        'local_only_default': not os.getenv("OPENAI_API_KEY"),
    })


@login_required
@xframe_options_exempt
def chat_embed(request):
    workspace = getattr(request, "workspace", None)
    profile = getattr(request.user, "profile", None)
    if profile and profile.is_guest and not request.user.is_superuser:
        return render(request, 'assistant/embed.html', {'history': [], 'guest_blocked': True})
    history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:20][::-1]

    if request.method == 'POST':
        content = (request.POST.get('message') or '').strip()
        local_only = not os.getenv("OPENAI_API_KEY")
        if content:
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='user', content=content)
            reply, usage, model_name = _assistant_reply(content, workspace, request, local_only=local_only)
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='assistant', content=reply)
            _record_usage(request.user, workspace, 'chat', usage, model_name)
            record_metric('ai_request', user=request.user, workspace=workspace, metadata={'local_only': local_only})
        history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:20][::-1]

    return render(request, 'assistant/embed.html', {'history': history, 'current_workspace': workspace})
