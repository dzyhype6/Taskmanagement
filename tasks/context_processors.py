def notifications(request):
    """Expose unread notifications + count to every template (for the navbar bell)."""
    if not request.user.is_authenticated:
        return {}
    qs = request.user.notifications.all()
    return {
        'nav_notifications': qs[:8],
        'nav_unread_count': qs.filter(is_read=False).count(),
    }
