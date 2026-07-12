"""Trigger an STK Push (Lipa na M-Pesa Online) — the PIN-prompt flow.

    python manage.py mpesa_stk --phone 254708374149 --amount 1

This initiates a real Daraja STK Push and prints Safaricom's response. A
ResponseCode of 0 ("Success. Request accepted for processing") is synchronous
proof that your app authenticated to Daraja and Safaricom accepted the payment
request — i.e. the API you connected works end to end.

SANDBOX vs PRODUCTION:
- On the sandbox (default), the request succeeds but NO dialog reaches a real
  phone; Safaricom simulates it with test number 254708374149. Pass that number.
- On production (go-live credentials), a real PIN prompt appears on the phone
  you pass to --phone.
"""
from django.core.management.base import BaseCommand
from django.conf import settings

from tasks import mpesa


class Command(BaseCommand):
    help = "Send an STK Push (Lipa na M-Pesa Online) and print Daraja's response."

    def add_arguments(self, parser):
        parser.add_argument('--phone', default='254708374149',
                            help='Phone in 2547XXXXXXXX form '
                                 '(sandbox: use the test number 254708374149).')
        parser.add_argument('--amount', default='1',
                            help='Amount in KES (default 1).')
        parser.add_argument('--ref', default='TaskPay',
                            help='Account reference (max 12 chars).')

    def handle(self, *args, **opts):
        env = getattr(settings, 'MPESA_ENV', 'sandbox') or 'sandbox'
        self.stdout.write(f"M-Pesa environment: {env}")

        missing = [k for k in mpesa.STK_REQUIRED
                   if not (getattr(settings, k, '') or '')]
        if missing:
            self.stdout.write(self.style.WARNING(
                "STK Push is not fully configured."))
            self.stdout.write("Missing settings: " + ", ".join(missing))
            return

        phone = mpesa.normalise_phone(opts['phone'])
        self.stdout.write(
            f"Sending STK Push: KES {opts['amount']} to {phone} "
            f"via shortcode {getattr(settings, 'MPESA_STK_SHORTCODE', '')}…")

        res = mpesa.stk_push(phone, opts['amount'], account_ref=opts['ref'],
                            description='TaskPay demo')

        if res.get('ok'):
            self.stdout.write(self.style.SUCCESS(
                "STK Push ACCEPTED by Safaricom."))
            self.stdout.write(f"  CustomerMessage : {res.get('customer_message')}")
            self.stdout.write(f"  CheckoutRequestID: {res.get('checkout_id')}")
            self.stdout.write(f"  MerchantRequestID: {res.get('merchant_id')}")
            if env != 'production':
                self.stdout.write(self.style.WARNING(
                    "Sandbox: the API works, but no dialog reaches a real phone. "
                    "A real PIN prompt requires production (go-live) credentials."))
        else:
            self.stderr.write(self.style.ERROR(
                f"STK Push FAILED: {res.get('error')}"))
            if res.get('raw'):
                self.stderr.write(f"  Raw response: {res['raw']}")
            self.stderr.write(
                "If this says the app is not authorised for the product, enable "
                "'Lipa Na M-Pesa Sandbox' on your Daraja app.")
