"""Helpers for creating in-app notifications and (optionally) emailing them."""
from django.conf import settings
from django.core.mail import send_mail

from .models import Notification, User


def notify(recipient, message, link=""):
    """Create an in-app notification and email it if email is configured."""
    Notification.objects.create(recipient=recipient, message=message, link=link)

    # Only attempt email if the user has an address AND Gmail creds are set up.
    if recipient.email and settings.EMAIL_HOST_PASSWORD:
        try:
            send_mail(
                subject="CodeForge Engineering — Task Update",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient.email],
                fail_silently=True,
            )
        except Exception:
            # Never let an email failure break the request.
            pass


def notify_managers(message, link=""):
    """Send a notification to every manager (e.g. when a worker finishes a task)."""
    for manager in User.objects.filter(role="manager"):
        notify(manager, message, link)
