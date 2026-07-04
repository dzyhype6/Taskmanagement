"""Build a task digest PDF and email it to all managers.

Run manually:
    python manage.py send_task_report
    python manage.py send_task_report --days 7          # completed-window = 7 days
    python manage.py send_task_report --to you@x.com     # override recipients (testing)
    python manage.py send_task_report --save-only        # write PDF to disk, don't email

Schedule it with Windows Task Scheduler / cron to make it automatic.
"""
from datetime import timedelta
from io import BytesIO

from django.core.management.base import BaseCommand
from django.core.mail import EmailMessage
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from tasks.models import Task, User


def build_pdf_bytes(context):
    """Render the digest template to PDF bytes using xhtml2pdf (Windows-friendly)."""
    from xhtml2pdf import pisa  # imported lazily so the app loads even if absent
    html = render_to_string("core/report_digest.html", context)
    buf = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError("xhtml2pdf failed to render the report PDF.")
    return buf.getvalue()


def build_context(window_days):
    now = timezone.now()
    today = timezone.localdate()
    since = now - timedelta(days=window_days)

    all_tasks = Task.objects.all()
    engineers = User.objects.filter(role="worker").order_by("username")

    per_engineer = []
    for e in engineers:
        et = all_tasks.filter(assigned_to=e)
        per_engineer.append({
            "name": e.get_full_name() or e.username,
            "pending": et.filter(status="pending").count(),
            "in_progress": et.filter(status="in_progress").count(),
            "completed": et.filter(status="completed").count(),
            "total": et.count(),
        })

    overdue = all_tasks.filter(
        due_date__lt=today,
    ).exclude(status="completed").order_by("due_date")

    recently_completed = all_tasks.filter(
        status="completed", completed_at__gte=since,
    ).order_by("-completed_at")

    return {
        "generated_at": now,
        "window_days": window_days,
        "total_tasks": all_tasks.count(),
        "pending": all_tasks.filter(status="pending").count(),
        "in_progress": all_tasks.filter(status="in_progress").count(),
        "completed": all_tasks.filter(status="completed").count(),
        "overdue": list(overdue),
        "per_engineer": per_engineer,
        "recently_completed": list(recently_completed),
    }


class Command(BaseCommand):
    help = "Generate a task digest PDF and email it to all managers."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1,
                            help="Window (days) for the 'recently completed' section. Default 1.")
        parser.add_argument("--to", action="append", default=None,
                            help="Override recipient(s). Repeatable. Defaults to all managers' emails.")
        parser.add_argument("--save-only", action="store_true",
                            help="Write the PDF to disk instead of emailing (for testing).")

    def handle(self, *args, **opts):
        window_days = opts["days"]
        context = build_context(window_days)

        try:
            pdf_bytes = build_pdf_bytes(context)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"PDF generation failed: {e}"))
            return

        stamp = timezone.localdate().isoformat()
        filename = f"task-digest-{stamp}.pdf"

        if opts["save_only"]:
            with open(filename, "wb") as fh:
                fh.write(pdf_bytes)
            self.stdout.write(self.style.SUCCESS(f"Saved {filename} ({len(pdf_bytes)} bytes)."))
            return

        # Recipients: explicit --to, else every manager with an email address.
        if opts["to"]:
            recipients = opts["to"]
        else:
            recipients = list(
                User.objects.filter(role="manager")
                .exclude(email="").values_list("email", flat=True)
            )

        if not recipients:
            self.stderr.write(self.style.WARNING("No recipient emails found — nothing sent."))
            return

        if not settings.EMAIL_HOST_PASSWORD:
            self.stderr.write(self.style.WARNING(
                "EMAIL_HOST_PASSWORD is not set — email is not configured. "
                "Use --save-only to test PDF generation."))
            return

        summary = (f"Total {context['total_tasks']} · Pending {context['pending']} · "
                   f"In Progress {context['in_progress']} · Completed {context['completed']} · "
                   f"Overdue {len(context['overdue'])}")

        email = EmailMessage(
            subject=f"CodeForge Task Digest — {stamp}",
            body=(f"Automated task digest for {stamp}.\n\n{summary}\n\n"
                  f"Full breakdown attached as PDF."),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        email.attach(filename, pdf_bytes, "application/pdf")
        sent = email.send(fail_silently=False)
        self.stdout.write(self.style.SUCCESS(
            f"Digest emailed to {', '.join(recipients)} (send()={sent})."))
