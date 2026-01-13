import datetime
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from .forms import StatementUploadForm
from .models import Category, Transaction
from .views import _user_can_view_finance, _parse_pdf_statement, _build_preview
from .notifications import notify_balance_threshold, check_category_budgets, check_balance_goals

@login_required
def transaction_import_pdf(request):
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
            to_create = []
            for idx, desc in enumerate(desc_list):
                desc = (desc or '').strip()
                if not desc:
                    continue
                try:
                    date_obj = datetime.date.fromisoformat(date_list[idx])
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
            if not name.endswith('.pdf'):
                parse_error = "Envie um arquivo PDF para esta opção."
            else:
                file_bytes = up_file.read()
                rows, parse_error = _parse_pdf_statement(file_bytes)
                preview_rows, ai_used = _build_preview(rows, categories)
                if not preview_rows and not parse_error:
                    parse_error = "Nenhuma linha válida encontrada no PDF."

    return render(request, 'tracker/transactions_import.html', {
        'upload_form': upload_form,
        'preview_rows': preview_rows,
        'categories': categories,
        'parse_error': parse_error,
        'mode': 'pdf',
        'ai_used': ai_used,
    })
