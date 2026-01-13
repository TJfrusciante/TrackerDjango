from django.core.management.base import BaseCommand

from tracker.models import Workspace
from tracker.notifications import check_balance_goals, check_category_budgets


class Command(BaseCommand):
    help = 'Check category budgets and balance goals for all workspaces.'

    def handle(self, *args, **options):
        total = 0
        for workspace in Workspace.objects.filter(is_active=True):
            check_category_budgets(workspace)
            check_balance_goals(workspace)
            total += 1
        self.stdout.write(self.style.SUCCESS(f'Workspaces verificados: {total}'))
