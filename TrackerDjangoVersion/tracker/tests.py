import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from .management.commands.import_legacy import parse_ddmmyyyy
from .models import Category, Task, Transaction, Workspace, WorkspaceMembership
from .views import _user_can_view_finance

User = get_user_model()


class FinancePermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.member = User.objects.create_user(username="member", password="pass")
        self.superuser = User.objects.create_superuser(username="admin", email="admin@example.com", password="pass")
        self.workspace = Workspace.objects.create(name="WS", slug="ws", owner=self.owner)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.member, role="member", can_edit_tasks=False)

    def test_user_can_view_finance_only_owner_or_superuser(self):
        dummy_request = type("R", (), {"user": self.owner})
        self.assertTrue(_user_can_view_finance(dummy_request, self.workspace))

        dummy_request.user = self.superuser
        self.assertTrue(_user_can_view_finance(dummy_request, self.workspace))

        dummy_request.user = self.member
        dummy_request.workspace_role = "member"
        self.assertFalse(_user_can_view_finance(dummy_request, self.workspace))


class TaskPermissionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.member = User.objects.create_user(username="member", password="pass")
        self.workspace = Workspace.objects.create(name="WS", slug="ws", owner=self.owner)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.member, role="member", can_edit_tasks=False)

    def test_member_without_permission_cannot_create_task(self):
        self.client.login(username="member", password="pass")
        response = self.client.post(reverse("tracker:task_create"), {"title": "Task", "due_date": "2025-01-01"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Task.objects.count(), 0)


class ChartDataTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.workspace1 = Workspace.objects.create(name="WS1", slug="ws1", owner=self.owner)
        WorkspaceMembership.objects.create(workspace=self.workspace1, user=self.owner, role="owner")
        self.workspace2 = Workspace.objects.create(name="WS2", slug="ws2", owner=self.owner)
        cat1 = Category.objects.create(name="Cat1", workspace=self.workspace1)
        cat2 = Category.objects.create(name="Cat2", workspace=self.workspace2)
        Transaction.objects.create(description="A", date=parse_ddmmyyyy("01/01/2025"), category=cat1, value=Decimal("10"), type="income", workspace=self.workspace1)
        Transaction.objects.create(description="B", date=parse_ddmmyyyy("01/01/2025"), category=cat2, value=Decimal("99"), type="income", workspace=self.workspace2)

    def test_requires_login(self):
        response = self.client.get(reverse("tracker:chart_data"))
        self.assertEqual(response.status_code, 302)

    def test_filters_by_workspace(self):
        self.client.login(username="owner", password="pass")
        response = self.client.get(reverse("tracker:chart_data"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # só deve trazer a categoria do workspace da sessão
        self.assertEqual(data["labels"], ["Cat1"])
        self.assertEqual(len(data["labels"]), 1)


class ImportLegacyCommandTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="admin", email="admin@example.com", password="pass")

    def _create_legacy_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        cur = conn.cursor()
        cur.execute("CREATE TABLE categories (category TEXT)")
        cur.execute("CREATE TABLE tasks (name TEXT, date TEXT, selected TEXT, status TEXT)")
        cur.execute("CREATE TABLE money (description TEXT, date TEXT, value REAL, selected TEXT, category TEXT)")
        cur.execute("INSERT INTO categories (category) VALUES ('Moradia')")
        cur.execute("INSERT INTO tasks (name, date, selected, status) VALUES ('Comprar', '01/01/2024', 'ok', 'done')")
        cur.execute("INSERT INTO money (description, date, value, selected, category) VALUES ('Salario', '02/01/2024', 1000, 'ok', 'Moradia')")
        conn.commit()
        conn.close()
        return Path(tmp.name)

    def test_import_assigns_workspace(self):
        legacy_path = self._create_legacy_db()
        call_command("import_legacy", db=str(legacy_path), workspace="ws-import", owner="admin")

        workspace = Workspace.objects.get(slug="ws-import")
        self.assertEqual(workspace.owner, self.owner)
        self.assertEqual(Category.objects.filter(workspace=workspace).count(), 2)  # inclui "Sem categoria"
        self.assertEqual(Transaction.objects.filter(workspace=workspace).count(), 1)
        self.assertEqual(Task.objects.filter(workspace=workspace).count(), 1)
        legacy_path.unlink(missing_ok=True)

# Create your tests here.
