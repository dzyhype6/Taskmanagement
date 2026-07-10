"""Check the Safaricom Daraja (M-Pesa) configuration and connection.

    python manage.py mpesa_check

- With no credentials configured: reports that payouts are SIMULATED and lists
  which MPESA_* settings are missing.
- With credentials: performs a real OAuth token request against Daraja
  (sandbox or production, per MPESA_ENV) and reports success or the exact
  error — a synchronous proof that the integration talks to Safaricom,
  without needing a public callback URL.
"""
from django.core.management.base import BaseCommand
from django.conf import settings

from tasks import mpesa


class Command(BaseCommand):
    help = "Verify M-Pesa Daraja credentials by requesting an OAuth token."

    def handle(self, *args, **opts):
        env = getattr(settings, 'MPESA_ENV', 'sandbox') or 'sandbox'
        self.stdout.write(f"M-Pesa environment: {env}")

        missing = [k for k in mpesa.REQUIRED if not (getattr(settings, k, '') or '')]
        if missing:
            self.stdout.write(self.style.WARNING(
                "Payouts are SIMULATED — Daraja is not configured."))
            self.stdout.write("Missing settings: " + ", ".join(missing))
            self.stdout.write(
                "To go live: copy TaskManagement/mpesa_config.example.py to "
                "TaskManagement/mpesa_config.py and fill it in "
                "(see that file for where each value comes from).")
            return

        self.stdout.write("All MPESA_* settings present — contacting Daraja…")
        try:
            token = mpesa.get_access_token()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"OAuth FAILED: {e}"))
            self.stderr.write(
                "Check MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET and that "
                "your Daraja app has the B2C product enabled.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"OAuth OK — received an access token ({token[:6]}…). "
            "The system can talk to Safaricom; B2C payouts will run LIVE "
            f"against the {env} environment."))
