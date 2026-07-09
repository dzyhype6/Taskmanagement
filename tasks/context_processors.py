def notifications(request):
    """Expose unread notifications + count to every template (for the navbar bell)."""
    if not request.user.is_authenticated:
        return {}
    qs = request.user.notifications.all()
    ctx = {
        'nav_notifications': qs[:8],
        'nav_unread_count': qs.filter(is_read=False).count(),
    }
    # For managers: how many completed tasks are waiting for approval (nav badge).
    if request.user.is_superuser or getattr(request.user, 'role', '') == 'manager':
        from .models import Task
        ctx['nav_pending_approvals'] = Task.objects.filter(
            status='completed', approved=False).count()
    return ctx
