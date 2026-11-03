"""Print a password hash suitable for RIESZ_PASSWORD_HASH in the .env file."""

import getpass

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Hash a password for RIESZ_PASSWORD_HASH (prompts for the password)."

    def handle(self, *args, **options):
        first = getpass.getpass("New password: ")
        if len(first) < 10:
            raise CommandError("Use at least 10 characters.")
        if getpass.getpass("Repeat: ") != first:
            raise CommandError("Passwords do not match.")
        self.stdout.write("Put this line in .env (keep the single quotes):")
        self.stdout.write(f"RIESZ_PASSWORD_HASH='{make_password(first)}'")
