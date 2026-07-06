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
        ('hourly', 'Paid per hour'),
    )
    # organization removed — single-organization app
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    # How an engineer is paid: a fixed monthly salary, a rate for each approved
    # task, or an hourly rate against logged time. Managers default to monthly
    # and simply ignore this.
    pay_type = models.CharField(max_length=10, choices=PAY_TYPE_CHOICES, default='monthly')
    monthly_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    task_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Default pay for each approved task (used when a task has no explicit amount).",
    )
    hourly_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Pay per hour of logged time (for per-hour engineers).",
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
    due_time = models.TimeField(null=True, blank=True, help_text="Optional time of day the task is due.")
    completed_at = models.DateTimeField(null=True, blank=True)
    # PM's estimate of how long the task should take (hours). Compared against
    # the hours engineers actually log.
    estimated_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)

    # --- Payment ---
    # What this task pays its engineer once the manager approves it (per-task
    # engineers). For monthly engineers it is simply informational.
    pay_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # % deducted from this task's pay if it is completed after the deadline.
    # The same rate is used to fine an engineer who leaves the task INCOMPLETE
    # past its deadline (deducted from their other pay on the next payslip).
    late_penalty_percent = models.PositiveSmallIntegerField(
        default=0, help_text="Percent of pay deducted if finished after the deadline (0 = none).")
    # True once an abandonment fine for this task has been charged on a payslip,
    # so it is never charged twice (and no late penalty is added on top).
    fine_settled = models.BooleanField(default=False)
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
        # not yet started is 0%. While in progress, progress is driven by the
        # checklist if one exists (objective), otherwise by the engineer's own
        # self-reported estimate. Either way it is capped at 99 (100 = done).
        if self.status == 'completed':
            self.progress = 100
        elif self.status == 'pending':
            self.progress = 0
        else:  # in_progress
            from_checklist = self.subtask_progress if self.pk else None
            value = from_checklist if from_checklist is not None else int(self.progress or 0)
            self.progress = max(0, min(99, value))
        # Approval only makes sense for a completed task. If the task is moved
        # back out of completed, drop the approval too (unless already paid).
        if self.status != 'completed' and self.approved and self.payment_id is None:
            self.approved = False
            self.approved_at = None
        super().save(*args, **kwargs)

    @property
    def subtask_progress(self):
        """% of checklist items done, or None if the task has no checklist."""
        subs = list(self.subtasks.all())
        if not subs:
            return None
        done = sum(1 for s in subs if s.is_done)
        return round(100 * done / len(subs))

    @property
    def subtask_counts(self):
        subs = list(self.subtasks.all())
        return {'done': sum(1 for s in subs if s.is_done), 'total': len(subs)}

    @property
    def has_subtasks(self):
        return self.subtasks.exists()

    @property
    def deadline(self):
        """Aware datetime for the due date/time, or None. A missing time means
        end of that day."""
        if not self.due_date:
            return None
        from datetime import datetime, time as dtime
        t = self.due_time or dtime(23, 59, 59)
        dt = datetime.combine(self.due_date, t)
        return timezone.make_aware(dt, timezone.get_current_timezone())

    @property
    def is_overdue(self):
        """Past its deadline (date, and time if given) and not yet completed."""
        if not self.due_date or self.status == 'completed':
            return False
        return self.deadline < timezone.now()

    @property
    def logged_hours(self):
        """Total hours engineers have logged against this task."""
        from decimal import Decimal
        total = self.time_logs.aggregate(s=models.Sum('hours'))['s']
        return total or Decimal('0')

    @property
    def was_late(self):
        """True if the task was completed after its deadline."""
        return bool(self.completed_at and self.deadline and self.completed_at > self.deadline)

    def _penalty(self):
        from decimal import Decimal, ROUND_HALF_UP
        amt = (self.pay_amount or Decimal('0')) * Decimal(self.late_penalty_percent) / Decimal('100')
        return amt.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def penalty_amount(self):
        """KES deducted from this task's pay for finishing late. (Not applied if
        an abandonment fine was already charged for it.)"""
        from decimal import Decimal
        if self.was_late and self.late_penalty_percent and not self.fine_settled:
            return self._penalty()
        return Decimal('0.00')

    @property
    def net_pay(self):
        """Task pay after any late penalty (what a per-task engineer is paid)."""
        from decimal import Decimal
        return (self.pay_amount or Decimal('0')) - self.penalty_amount

    @property
    def abandon_fine(self):
        """KES fined for leaving this task incomplete past its deadline — charged
        against the engineer's other pay. Zero once settled or if completed."""
        from decimal import Decimal
        if (self.status != 'completed' and self.is_overdue
                and self.late_penalty_percent and not self.fine_settled):
            return self._penalty()
        return Decimal('0.00')

    @property
    def is_paid(self):
        return self.payment_id is not None

    def __str__(self):
        return f"{self.title} - {self.status}"


class SubTask(models.Model):
    """A checklist item that breaks a task into smaller steps. When a task has
    subtasks, its progress is the fraction of these that are done."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='subtasks')
    title = models.CharField(max_length=255)
    is_done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']  # keep the order they were added

    def __str__(self):
        return f"[{'x' if self.is_done else ' '}] {self.title}"


class TimeLog(models.Model):
    """Hours an engineer logged working on a task. For per-hour engineers these
    feed payroll (hours × hourly rate); for everyone they track effort."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='time_logs')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='time_logs')
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    work_date = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set once these hours have been paid on a payslip; locks the entry.
    payment = models.ForeignKey(
        'Payment', null=True, blank=True, on_delete=models.SET_NULL, related_name='time_logs',
    )

    class Meta:
        ordering = ['-work_date', '-id']

    @property
    def is_paid(self):
        return self.payment_id is not None

    def __str__(self):
        return f"{self.user.username}: {self.hours}h on #{self.task_id}"


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
    - hourly: pays a batch of not-yet-paid time logs (hours × hourly rate);
      each TimeLog is linked back via TimeLog.payment and locked.
    """
    BASIS_CHOICES = (
        ('monthly', 'Monthly salary'),
        ('per_task', 'Per task'),
        ('hourly', 'Per hour'),
    )
    engineer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES)
    period = models.CharField(max_length=40, blank=True, help_text="e.g. a month, sprint or free text.")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    task_count = models.PositiveIntegerField(default=0)
    hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)  # for hourly payslips
    fine = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # abandonment fines deducted
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='payments_made',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.engineer.username} — {self.amount} ({self.get_basis_display()})"