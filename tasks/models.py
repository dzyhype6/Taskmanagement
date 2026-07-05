from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone

class User(AbstractUser):
    ROLE_CHOICES = (
        ('manager', 'Project Manager'),
        ('worker', 'Engineer'),
    )
    PAY_TYPE_CHOICES = (
        ('monthly', 'Monthly salary'),
        ('per_task', 'Paid per task'),
    )
    # organization removed — single-organization app
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    # How an engineer is paid: a fixed monthly salary, or a rate for each
    # approved task. Managers are 'monthly' by default and simply ignore this.
    pay_type = models.CharField(max_length=10, choices=PAY_TYPE_CHOICES, default='monthly')
    monthly_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    task_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Default pay for each approved task (used when a task has no explicit amount).",
    )

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

class Task(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    )
    PRIORITY_CHOICES = (
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tasks')
    # organization removed — single-organization app
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    # How close the engineer is to finishing (0–100). The engineer sets this as
    # they work; the PM reads it to see how far along an in-progress task is.
    progress = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # --- Payment ---
    # What this task pays its engineer once the manager approves it (per-task
    # engineers). For monthly engineers it is simply informational.
    pay_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # A completed task must be approved by a manager before it counts for pay.
    approved = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True, blank=True)
    # Set once the task has been included in a payment (payslip); locks it.
    payment = models.ForeignKey(
        'Payment', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks',
    )

    def save(self, *args, **kwargs):
        # Stamp the moment the task first reaches 'completed'; clear it if the
        # task is moved back out of completed. Keeps an accurate finish time
        # independent of updated_at (which changes on any edit).
        if self.status == 'completed' and self.completed_at is None:
            self.completed_at = timezone.now()
        elif self.status != 'completed':
            self.completed_at = None
        # Keep progress in step with status: a completed task is 100%, a task
        # not yet started is 0%. While in progress the engineer's own value is
        # kept (clamped to a sensible 0–100, and never a misleading 0 or 100).
        if self.status == 'completed':
            self.progress = 100
        elif self.status == 'pending':
            self.progress = 0
        else:  # in_progress — keep the engineer's estimate, but 100% means "done"
            self.progress = max(0, min(99, int(self.progress or 0)))
        # Approval only makes sense for a completed task. If the task is moved
        # back out of completed, drop the approval too (unless already paid).
        if self.status != 'completed' and self.approved and self.payment_id is None:
            self.approved = False
            self.approved_at = None
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        """Past its due date and not yet completed."""
        return bool(
            self.due_date
            and self.status != 'completed'
            and self.due_date < timezone.localdate()
        )

    @property
    def is_paid(self):
        return self.payment_id is not None

    def __str__(self):
        return f"{self.title} - {self.status}"


class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"To {self.recipient.username}: {self.message[:40]}"


class TaskComment(models.Model):
    """A discussion comment left on a task by a manager or engineer."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='task_comments')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']  # oldest first, like a thread

    def __str__(self):
        return f"{self.author.username} on #{self.task_id}: {self.body[:30]}"


class TaskAttachment(models.Model):
    """A file attached to a task (spec, screenshot, deliverable, etc.)."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='task_attachments/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='uploads')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    @property
    def filename(self):
        import os
        return os.path.basename(self.file.name)

    def __str__(self):
        return f"{self.filename} on #{self.task_id}"


class Payment(models.Model):
    """A payment (payslip) a manager records for an engineer.

    - per_task: pays a batch of approved, not-yet-paid tasks (each task is
      linked back via Task.payment and locked from further edits/deletes).
    - monthly: pays the engineer's standing monthly salary; no tasks attached.
    """
    BASIS_CHOICES = (
        ('monthly', 'Monthly salary'),
        ('per_task', 'Per task'),
    )
    engineer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES)
    period = models.CharField(max_length=40, blank=True, help_text="e.g. a month, sprint or free text.")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    task_count = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='payments_made',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.engineer.username} — {self.amount} ({self.get_basis_display()})"