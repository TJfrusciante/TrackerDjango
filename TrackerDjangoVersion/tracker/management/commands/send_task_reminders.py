import datetime

from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from tracker.models import Notification, Task
from tracker.notifications import notify_task_due


class Command(BaseCommand):
    help = 'Send task due/overdue reminders.'

    def handle(self, *args, **options):
        today = timezone.localdate()
        soon_limit = today + datetime.timedelta(days=3)
        tasks = Task.objects.filter(status='ongoing', due_date__lte=soon_limit)
        sent = 0

        for task in tasks:
            if not task.workspace:
                continue
            owner = task.workspace.owner
            if not owner:
                continue
            action_url = reverse('tracker:task_update', args=[task.id]) + '?detail=1'
            already = Notification.objects.filter(
                user=owner,
                action_url=action_url,
                created_at__date=today,
                title__icontains='Tarefa',
            ).exists()
            if already:
                continue
            days_left = (task.due_date - today).days
            status = 'overdue' if days_left < 0 else 'soon'
            notify_task_due(task, status, days_left if status == 'soon' else None)
            sent += 1

        self.stdout.write(self.style.SUCCESS(f'Reminders enviados: {sent}'))
