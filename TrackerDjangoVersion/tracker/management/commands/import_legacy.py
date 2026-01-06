import datetime
import sqlite3
from decimal import Decimal
from pathlib import Path
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.contrib.auth import get_user_model
from django.utils.text import slugify

from tracker.models import Category, Task, Transaction, Workspace

User = get_user_model()

def parse_ddmmyyyy(date_str):
    try:
        day, month, year = date_str.split('/')
        return datetime.date(int(year), int(month), int(day))
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        "Importa dados do banco SQLite antigo (money, tasks, categories) para os models atuais, "
        "associando tudo a um workspace."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--db',
            type=str,
            help='Caminho para o database.db legado. Padrao: <BASE_DIR>/Tracker/database.db',
        )
        parser.add_argument(
            '--workspace',
            type=str,
            help='Slug do workspace de destino (será criado se não existir). Padrão: import-<owner>',
        )
        parser.add_argument(
            '--owner',
            type=str,
            help='Username do dono do workspace (default: primeiro superuser, senão primeiro usuário).',
        )

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        default_path = 'Tracker/database.db'
        db_path = Path(options['db']) if options.get('db') else default_path

        if not db_path.exists():
            raise CommandError(f'Banco legado nao encontrado em {db_path}')

        self.stdout.write(f'Usando banco legado: {db_path}')

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            categories_rows = self._safe_query(conn, 'SELECT category FROM categories')
            tasks_rows = self._safe_query(conn, 'SELECT name, date, selected, status FROM tasks')
            money_rows = self._safe_query(conn, 'SELECT description, date, value, selected, category FROM money')

        owner = self._resolve_owner(options.get('owner'))
        workspace = self._resolve_workspace(options.get('workspace'), owner)

        self.stdout.write(f'Usando workspace destino: {workspace.name} (slug={workspace.slug})')

        imported_categories = self._import_categories(categories_rows, workspace)
        imported_transactions = self._import_transactions(money_rows, workspace)
        imported_tasks = self._import_tasks(tasks_rows, workspace)

        self.stdout.write(self.style.SUCCESS(
            f'Import finalizado para {workspace.slug}: {imported_categories} categorias, '
            f'{imported_transactions} transacoes, {imported_tasks} tarefas.'
        ))

    def _safe_query(self, conn, sql):
        try:
            return list(conn.execute(sql))
        except sqlite3.OperationalError:
            return []

    def _resolve_owner(self, username):
        if username:
            owner = User.objects.filter(username=username).first()
            if not owner:
                raise CommandError(f'Usuario owner "{username}" nao encontrado.')
            return owner

        owner = User.objects.filter(is_superuser=True).order_by('id').first()
        if owner:
            return owner
        owner = User.objects.order_by('id').first()
        if owner:
            return owner
        raise CommandError('Nenhum usuario encontrado para ser owner do workspace.')

    def _resolve_workspace(self, workspace_slug, owner):
        if workspace_slug:
            slug = slugify(workspace_slug)
        else:
            slug = f"import-{owner.username}"
        ws, created = Workspace.objects.get_or_create(
            slug=slug,
            defaults={'name': slug.replace('-', ' ').title(), 'owner': owner},
        )
        # se o workspace existir mas for de outro owner, avisar
        if ws.owner != owner:
            self.stdout.write(self.style.WARNING(
                f'Workspace {slug} já existe e pertence a {ws.owner}. Dados serão importados nele.'
            ))
        return ws

    def _import_categories(self, rows, workspace):
        created = 0
        for row in rows:
            name = row[0].strip() if row[0] else ''
            if not name:
                continue
            _, was_created = Category.objects.get_or_create(name=name, workspace=workspace)
            if was_created:
                created += 1

        Category.objects.get_or_create(name='Sem categoria', workspace=workspace)
        return created

    @transaction.atomic
    def _import_transactions(self, rows, workspace):
        if not rows:
            return 0

        cat_cache = {c.name: c for c in Category.objects.filter(workspace=workspace)}
        existing = set(
            Transaction.objects.filter(workspace=workspace).values_list('description', 'date', 'type', 'value', 'category_id')
        )

        to_create = []
        for row in rows:
            desc = (row[0] or '').strip()
            raw_date = row[1] or ''
            raw_value = row[2]
            category_name = (row[4] or '').strip() or 'Sem categoria'
            date_obj = parse_ddmmyyyy(raw_date)

            if not desc or date_obj is None or raw_value is None:
                continue

            t_type = 'income' if float(raw_value) >= 0 else 'expense'
            value_abs = Decimal(str(abs(raw_value)))

            category = cat_cache.get(category_name)
            if not category:
                category = Category.objects.create(name=category_name, workspace=workspace)
                cat_cache[category_name] = category

            key = (desc, date_obj, t_type, value_abs, category.id)
            if key in existing:
                continue

            to_create.append(Transaction(
                description=desc,
                date=date_obj,
                category=category,
                value=value_abs,
                type=t_type,
                selected=(row[3] == 'ok'),
                workspace=workspace,
            ))
            existing.add(key)

        if to_create:
            Transaction.objects.bulk_create(to_create, batch_size=500)
        return len(to_create)

    @transaction.atomic
    def _import_tasks(self, rows, workspace):
        if not rows:
            return 0

        existing = set(Task.objects.filter(workspace=workspace).values_list('title', 'due_date', 'status'))
        to_create = []
        for row in rows:
            title = (row[0] or '').strip()
            raw_date = row[1] or ''
            status_raw = row[3] or 'ongoing'

            due_date = parse_ddmmyyyy(raw_date)
            if not title or due_date is None:
                continue

            status = 'done' if status_raw == 'done' else 'ongoing'
            key = (title, due_date, status)
            if key in existing:
                continue

            to_create.append(Task(
                title=title,
                due_date=due_date,
                status=status,
                selected=(row[2] == 'ok'),
                workspace=workspace,
            ))
            existing.add(key)

        if to_create:
            Task.objects.bulk_create(to_create, batch_size=500)
        return len(to_create)
