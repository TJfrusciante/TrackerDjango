from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.mail import send_mail


class Command(BaseCommand):
    help = "Send a test email using the configured SMTP backend."

    def add_arguments(self, parser):
        parser.add_argument("--to", dest="to_email", help="Recipient email")
        parser.add_argument("--from", dest="from_email", help="Sender email")
        parser.add_argument("--subject", dest="subject", default="iTracker test email")

    def handle(self, *args, **options):
        to_email = options.get("to_email") or settings.DEFAULT_FROM_EMAIL
        from_email = options.get("from_email") or settings.DEFAULT_FROM_EMAIL
        subject = options.get("subject") or "iTracker test email"
        if not to_email:
            self.stderr.write("Missing --to email.")
            return
        try:
            send_mail(subject, "Test email from iTracker.", from_email, [to_email], fail_silently=False)
            self.stdout.write(f"Test email sent to {to_email}.")
        except Exception as exc:
            self.stderr.write(f"Failed to send: {exc}")
