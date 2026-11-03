"""Log out every browser session."""

from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand

from accounts.models import SessionInfo


class Command(BaseCommand):
    help = "Delete all login sessions (every device will have to log in again)."

    def handle(self, *args, **options):
        count = Session.objects.count()
        Session.objects.all().delete()
        SessionInfo.objects.all().delete()
        self.stdout.write(f"Revoked {count} session(s).")
