from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from tracker.models import Category, Transaction, Workspace
from django.contrib.auth import get_user_model

from .models import WhatsAppMessage, WhatsAppProfile
from .services import extract_amount, infer_type, parse_text_to_result, process_incoming_text

User = get_user_model()


class WhatsAppParserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="12345678!")
        self.workspace = Workspace.objects.create(name="Main", slug="main", owner=self.user)

    def test_extract_amount(self):
        self.assertEqual(extract_amount("Gastei R$ 500 na conta de agua"), Decimal("500"))
        self.assertEqual(extract_amount("Almoco R$ 45,50 no restaurante"), Decimal("45.50"))

    def test_infer_type(self):
        self.assertEqual(infer_type("Salario de R$ 5000 recebido hoje"), "income")
        self.assertEqual(infer_type("Paguei 150 de mercado agora"), "expense")

    def test_parse_text_result(self):
        result = parse_text_to_result(self.user, self.workspace, "Paguei 150 de mercado agora")
        self.assertIsNotNone(result)
        self.assertEqual(result.amount, Decimal("150"))
        self.assertEqual(result.tx_type, "expense")
        self.assertEqual(result.tx_date, timezone.localdate())

    @override_settings(WHATSAPP_CONFIRMATION_THRESHOLD=1000)
    def test_confirmation_threshold(self):
        result = parse_text_to_result(self.user, self.workspace, "Gastei 1500 no aluguel")
        self.assertIsNotNone(result)
        self.assertEqual(result.amount, Decimal("1500"))
        self.assertTrue(result.requires_confirmation)

    def test_examples_match(self):
        expense = parse_text_to_result(self.user, self.workspace, "Gastei R$ 500 na conta de agua")
        self.assertIsNotNone(expense)
        self.assertEqual(expense.amount, Decimal("500"))
        self.assertEqual(expense.tx_type, "expense")
        income = parse_text_to_result(self.user, self.workspace, "Salario de R$ 5000 recebido hoje")
        self.assertIsNotNone(income)
        self.assertEqual(income.amount, Decimal("5000"))
        self.assertEqual(income.tx_type, "income")

    def test_command_summary(self):
        category = Category.objects.create(name="Receitas", workspace=self.workspace, color="#10b981")
        Transaction.objects.create(
            description="Salario",
            date=timezone.localdate(),
            category=category,
            value=Decimal("1000"),
            type="income",
            workspace=self.workspace,
            responsible=self.user,
        )
        profile = WhatsAppProfile.objects.create(user=self.user, workspace=self.workspace, phone_number="+5511999999999")
        message = WhatsAppMessage.objects.create(
            user=self.user,
            workspace=self.workspace,
            direction="in",
            from_number=profile.phone_number,
            to_number="+5511888888888",
            body="/resumo",
        )
        response = process_incoming_text(profile, message, "/resumo")
        self.assertIn("Resumo do mes", response)
