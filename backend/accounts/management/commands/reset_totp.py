"""Remove the TOTP authenticator and recovery codes (for a lost device)."""

from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.services import disable_totp


class Command(BaseCommand):
    help = "Disable two-factor authentication for the configured account."

    def handle(self, *args, **options):
        disable_totp(settings.RIESZ_USERNAME)
        self.stdout.write("Two-factor authentication has been disabled. Log in and set it up again.")
