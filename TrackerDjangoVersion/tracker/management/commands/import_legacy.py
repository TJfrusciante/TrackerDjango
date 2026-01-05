import datetime
import sqlite3
from decimal import Decimal
from pathlib import Path
import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracker.models import Category, Task, Transaction


def parse_ddmmyyyy(date_str):
    try:
        day, month, year = date_str.split('/')
        return datetime.date(int(year), int(month), int(day))
    except Exception:
        return None


class Command(BaseCommand):
    help = "Importa dados do banco SQLite antigo (money, tasks, categories) para os models atuais."

    def add_arguments(self, parser):
        parser.add_argument(
            '--db',
            type=str,
            help='Caminho para o database.db legado. Padrao: <BASE_DIR>/Tracker/database.db',
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

        imported_categories = self._import_categories(categories_rows)
        imported_transactions = self._import_transactions(money_rows)
        imported_tasks = self._import_tasks(tasks_rows)

        self.stdout.write(self.style.SUCCESS(
            f'Import finalizado: {imported_categories} categorias, {imported_transactions} transacoes, {imported_tasks} tarefas.'
        ))

    def _safe_query(self, conn, sql):
        try:
            return list(conn.execute(sql))
        except sqlite3.OperationalError:
            return []

    def _import_categories(self, rows):
        created = 0
        for row in rows:
            name = row[0].strip() if row[0] else ''
            if not name:
                continue
            _, was_created = Category.objects.get_or_create(name=name)
            if was_created:
                created += 1

        Category.objects.get_or_create(name='Sem categoria')
        return created

    @transaction.atomic
    def _import_transactions(self, rows):
        if not rows:
            return 0

        cat_cache = {c.name: c for c in Category.objects.all()}
        existing = set(
            Transaction.objects.values_list('description', 'date', 'type', 'value', 'category_id')
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
                category = Category.objects.create(name=category_name)
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
            ))
            existing.add(key)

        if to_create:
            Transaction.objects.bulk_create(to_create, batch_size=500)
        return len(to_create)

    @transaction.atomic
    def _import_tasks(self, rows):
        if not rows:
            return 0

        existing = set(Task.objects.values_list('title', 'due_date', 'status'))
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
            ))
            existing.add(key)

        if to_create:
            Task.objects.bulk_create(to_create, batch_size=500)
        return len(to_create)
