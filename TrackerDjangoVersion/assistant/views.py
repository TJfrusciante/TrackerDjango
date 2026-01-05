import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Case, When, DecimalField, F
from django.shortcuts import render

from tracker.models import Transaction, Task, WorkspaceMembership
from .models import ChatMessage
from .services import llm_reply


def _finance_queryset(request, workspace):
    qs = Transaction.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    elif not request.user.is_superuser:
        return Transaction.objects.none()
    return qs


def _tasks_queryset(request, workspace):
    qs = Task.objects.all()
    if workspace and not request.user.is_superuser:
        qs = qs.filter(workspace=workspace)
    return qs


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
        f"Saídas: R$ {expense:.2f}",
        f"Saldo: R$ {balance:.2f}",
        f"Tarefas em andamento: {open_tasks}",
        f"Tarefas concluídas: {done_tasks}",
    ]
    if per_category:
        top = per_category[:3]
        summary_lines.append("Top categorias (net): " + ", ".join([f"{c['category__name']} ({c['total']:+.2f})" for c in top]))
    return "\n".join(summary_lines)


def _assistant_reply(message, workspace, request):
    """
    Usa LLM se configurado; fallback para resumo baseado em regras.
    """
    summary = _summarize(workspace, request)
    today = datetime.date.today().strftime("%d/%m/%Y")

    system_prompt = (
        "Você é o assistente financeiro do Tracker. Responda em português de forma curta. "
        "Use apenas os dados fornecidos no resumo. Se algo não estiver no resumo, diga que não sabe."
    )
    user_prompt = f"Data atual: {today}. Resumo disponível:\n{summary}\nPergunta: {message}"
    llm_answer = llm_reply(system_prompt, user_prompt)
    if llm_answer:
        return llm_answer
    # fallback
    return f"Não consegui consultar o modelo agora. Aqui vai um resumo rápido:\n{summary}"


@login_required
def chat(request):
    workspace = getattr(request, "workspace", None)
    if request.user.is_superuser and workspace is None:
        # se superuser estiver em modo global, não mostrar histórico para evitar volume; não bloqueia
        pass
    history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:30][::-1]

    if request.method == 'POST':
        content = (request.POST.get('message') or '').strip()
        if content:
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='user', content=content)
            reply = _assistant_reply(content, workspace, request)
            ChatMessage.objects.create(user=request.user, workspace=workspace, role='assistant', content=reply)
        history = ChatMessage.objects.filter(user=request.user).order_by('-created_at')[:30][::-1]

    return render(request, 'assistant/chat.html', {'history': history})
