from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.template.loader import render_to_string
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count
from datetime import date

from django.urls import reverse
from django.db import transaction
from django.db.models import Q, Sum, Avg
from django.utils import timezone
from decimal import Decimal
from .models import Task, User, Notification, Payment, SubTask, TimeLog
from .forms import (
    TaskForm, WorkerTaskStatusForm, ReportFilterForm,
    TaskCommentForm, TaskAttachmentForm, EngineerPayForm, TimeLogForm,
)
from .services import notify, notify_managers
from . import mpesa
from django.contrib import messages


def _is_manager(user):
    return user.is_superuser or user.role == 'manager'


def _mock_reference(method):
    """A fake transaction code for a simulated disbursement (M-Pesa-style code
    or a bank reference)."""
    import random, string
    if method == 'bank':
        return 'BNK' + ''.join(random.choices(string.digits, k=9))
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))


def _default_task_pay(task):
    """For a per-task engineer, fall back to their standing rate when a task
    was created without an explicit amount. Never lowers an amount already set."""
    eng = task.assigned_to
    if eng and eng.pay_type == 'per_task' and (task.pay_amount or 0) == 0:
        return eng.task_rate or Decimal('0')
    return task.pay_amount or Decimal('0')


# --- Role redirect and not member view ---
@login_required
def role_redirect(request):
    user = request.user
    # Superusers (e.g. created with `createsuperuser`, which sets no role)
    # count as managers everywhere — never bounce them between dashboards.
    if user.is_superuser or user.role == 'manager':
        return redirect('manager_dashboard')
    elif user.role == 'worker':
        return redirect('worker_dashboard')
    else:
        return redirect('not_member')

def not_member(request):
    return render(request, 'core/not_member.html')

# --- Dashboards ---
@login_required
def manager_dashboard(request):
    user = request.user
    if not (user.is_superuser or user.role == 'manager'):
        # a roleless non-superuser goes to not_member, NOT worker_dashboard
        # (which would bounce them straight back here in a redirect loop)
        return redirect('worker_dashboard' if user.role == 'worker' else 'not_member')
    # Single-organization app: managers see all tasks
    tasks = Task.objects.all()
    workers = User.objects.filter(role='worker')
    context = {
        'tasks': tasks,
        'workers': workers,
        'manager': user,
    }
    return render(request, 'core/manager_dashboard.html', context)

@login_required
def engineer_list(request):
    # Only managers/superusers can view the engineers panel
    if not (request.user.is_superuser or request.user.role == 'manager'):
        return redirect('worker_dashboard')
    engineers = User.objects.filter(role='worker').order_by('username')
    return render(request, 'core/engineer_list.html', {'engineers': engineers})

@login_required
def engineer_detail(request, pk):
    if not (request.user.is_superuser or request.user.role == 'manager'):
        return redirect('worker_dashboard')
    engineer = get_object_or_404(User, pk=pk, role='worker')
    tasks = Task.objects.filter(assigned_to=engineer).order_by('-created_at')
    # Payment snapshot for the header cards (per-task earnings, net of late penalties).
    approved_list = list(tasks.filter(approved=True))
    earned = sum((t.net_pay for t in approved_list), Decimal('0'))
    paid = sum((t.net_pay for t in approved_list if t.is_paid), Decimal('0'))
    payable_tasks = sum(1 for t in approved_list if not t.is_paid)
    # Completed work not yet approved — its pay is "pending" until the PM approves.
    pending_list = list(tasks.filter(status='completed', approved=False))
    pending_earnings = sum((t.net_pay for t in pending_list), Decimal('0'))
    pending_count = len(pending_list)
    # Time snapshot (all pay types accrue hours; only hourly staff bill them).
    logs = TimeLog.objects.filter(user=engineer)
    logged_hours = logs.aggregate(s=Sum('hours'))['s'] or Decimal('0')
    unpaid_hours = logs.filter(payment__isnull=True).aggregate(s=Sum('hours'))['s'] or Decimal('0')
    hourly_unpaid = unpaid_hours * (engineer.hourly_rate or Decimal('0'))
    return render(request, 'core/engineer_detail.html', {
        'engineer': engineer,
        'tasks': tasks,
        'pay_form': EngineerPayForm(instance=engineer),
        'earned': earned,
        'paid': paid,
        'unpaid': earned - paid,
        'payable_tasks': payable_tasks,
        'pending_earnings': pending_earnings,
        'pending_count': pending_count,
        'logged_hours': logged_hours,
        'unpaid_hours': unpaid_hours,
        'hourly_unpaid': hourly_unpaid,
        'payments': engineer.payments.all()[:10],
    })

@login_required
def worker_dashboard(request):
    user = request.user
    if user.role != 'worker':
        # managers/superusers to their dashboard; anyone roleless to not_member
        return redirect('manager_dashboard' if (user.is_superuser or user.role == 'manager') else 'not_member')
    tasks = Task.objects.filter(assigned_to=user)
    completed = tasks.filter(status='completed').select_related('payment').order_by('-completed_at')
    # What the engineer has actually been paid so far (their per-task payslips
    # plus any monthly salary payments recorded for them).
    total_paid = user.payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    # Their own logged effort (hours) — visible to the engineer.
    logged_hours = TimeLog.objects.filter(user=user).aggregate(s=Sum('hours'))['s'] or Decimal('0')
    context = {
        'tasks': tasks,
        'worker': user,
        'completed_tasks': completed,
        'total_paid': total_paid,
        'logged_hours': logged_hours,
        'pay_type': user.get_pay_type_display(),
    }
    return render(request, 'core/worker_dashboard.html', context)

# --- Task Views ---

class ManagerRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_superuser or user.role == 'manager'

class WorkerRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_superuser or user.role == 'worker'

class TaskListView(LoginRequiredMixin, ListView):
    model = Task
    template_name = 'core/task_list.html'
    context_object_name = 'tasks'
    paginate_by = 15

    # Whitelist of allowed sort options -> ORM ordering.
    SORT_OPTIONS = {
        'created': '-created_at',
        'due': 'due_date',
        'priority': '-priority',   # high > medium > low alphabetically reversed
        'status': 'status',
        'title': 'title',
    }

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or user.role == 'manager':
            qs = Task.objects.all()
        else:
            # workers see only their assigned tasks
            qs = Task.objects.filter(assigned_to=user)

        # --- Search (title / description / engineer username) ---
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(description__icontains=q)
                | Q(assigned_to__username__icontains=q)
            )

        # --- Filters ---
        status = self.request.GET.get('status', '').strip()
        if status in dict(Task.STATUS_CHOICES):
            qs = qs.filter(status=status)
        priority = self.request.GET.get('priority', '').strip()
        if priority in dict(Task.PRIORITY_CHOICES):
            qs = qs.filter(priority=priority)

        # --- Sort ---
        sort = self.request.GET.get('sort', 'created')
        return qs.order_by(self.SORT_OPTIONS.get(sort, '-created_at'))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Echo current filter state back to the template (for inputs + paging links).
        ctx['q'] = self.request.GET.get('q', '')
        ctx['status_filter'] = self.request.GET.get('status', '')
        ctx['priority_filter'] = self.request.GET.get('priority', '')
        ctx['sort'] = self.request.GET.get('sort', 'created')
        ctx['status_choices'] = Task.STATUS_CHOICES
        ctx['priority_choices'] = Task.PRIORITY_CHOICES
        # Preserve filters across pagination links.
        params = self.request.GET.copy()
        params.pop('page', None)
        ctx['querystring'] = params.urlencode()
        return ctx

class TaskDetailView(LoginRequiredMixin, DetailView):
    model = Task
    template_name = 'core/task_detail.html'
    context_object_name = 'task'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['comments'] = self.object.comments.select_related('author')
        ctx['attachments'] = self.object.attachments.select_related('uploaded_by')
        ctx['subtasks'] = self.object.subtasks.all()
        ctx['subtask_counts'] = self.object.subtask_counts
        ctx['can_edit_subtasks'] = _can_access_task(self.request.user, self.object)
        ctx['time_logs'] = self.object.time_logs.select_related('user')
        ctx['logged_hours'] = self.object.logged_hours
        ctx['timelog_form'] = TimeLogForm()
        ctx['can_log_time'] = _can_access_task(self.request.user, self.object)
        # "Cost of this work" (managers only) — what this task is paying.
        eng = self.object.assigned_to
        if eng.pay_type == 'hourly':
            ctx['work_cost_kind'] = 'hourly'
            ctx['work_cost'] = self.object.logged_hours * (eng.hourly_rate or Decimal('0'))
        elif eng.pay_type == 'per_task':
            ctx['work_cost_kind'] = 'per_task'
            ctx['work_cost'] = self.object.net_pay
        else:
            ctx['work_cost_kind'] = 'monthly'
            ctx['work_cost'] = None
        ctx['comment_form'] = TaskCommentForm()
        ctx['attachment_form'] = TaskAttachmentForm()
        return ctx

class TaskCreateView(LoginRequiredMixin, ManagerRequiredMixin, CreateView):
    model = Task
    form_class = TaskForm
    template_name = 'core/task_form.html'
    success_url = reverse_lazy('task_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        # Notify (and email) the engineer the task was assigned to.
        task = self.object
        # Fill in the per-task pay from the engineer's standing rate if the
        # manager left the amount at 0.
        default_pay = _default_task_pay(task)
        if default_pay != (task.pay_amount or 0):
            task.pay_amount = default_pay
            task.save(update_fields=['pay_amount'])
        link = reverse('task_detail', args=[task.pk])
        notify(
            task.assigned_to,
            f'New task assigned to you: "{task.title}"',
            link,
        )
        messages.success(self.request, "Task created and engineer notified.")
        return response

class TaskUpdateView(LoginRequiredMixin, ManagerRequiredMixin, UpdateView):
    model = Task
    form_class = TaskForm
    template_name = 'core/task_form.html'
    success_url = reverse_lazy('task_list')

    def form_valid(self, form):
        # Detect reassignment so we can notify the newly-assigned engineer.
        old_assignee_id = Task.objects.get(pk=self.object.pk).assigned_to_id if self.object else None
        response = super().form_valid(form)
        task = self.object
        # Keep the per-task default in step if the amount was left at 0.
        default_pay = _default_task_pay(task)
        if default_pay != (task.pay_amount or 0):
            task.pay_amount = default_pay
            task.save(update_fields=['pay_amount'])
        if task.assigned_to_id != old_assignee_id:
            link = reverse('task_detail', args=[task.pk])
            notify(task.assigned_to, f'A task was assigned to you: "{task.title}"', link)
        messages.success(self.request, "Task updated successfully.")
        return response

    def get_queryset(self):
        # A task locked to a payslip must not be edited (amount/assignee changes
        # would corrupt what was already paid).
        return Task.objects.filter(payment__isnull=True)

class TaskDeleteView(LoginRequiredMixin, ManagerRequiredMixin, DeleteView):
    model = Task
    template_name = 'core/task_confirm_delete.html'
    success_url = reverse_lazy('task_list')

    def get_queryset(self):
        # A paid task is part of a payslip and cannot be deleted.
        return Task.objects.filter(payment__isnull=True)

    def delete(self, request, *args, **kwargs):
        messages.success(self.request, "Task deleted successfully.")
        return super().delete(request, *args, **kwargs)

def _can_access_task(user, task):
    """Managers/superusers can access any task; workers only their own."""
    return user.is_superuser or user.role == 'manager' or task.assigned_to_id == user.id


@login_required
def add_comment(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        form = TaskCommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.task = task
            comment.author = request.user
            comment.save()
            # Notify the "other side": engineer comments -> managers; manager comments -> engineer.
            link = reverse('task_detail', args=[task.pk])
            if request.user.role == 'worker':
                notify_managers(f'{request.user.username} commented on "{task.title}".', link)
            else:
                if task.assigned_to_id != request.user.id:
                    notify(task.assigned_to, f'New comment on your task "{task.title}".', link)
            messages.success(request, "Comment added.")
        else:
            messages.error(request, "Comment cannot be empty.")
    return redirect('task_detail', pk=task.pk)


@login_required
def add_attachment(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        form = TaskAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            att = form.save(commit=False)
            att.task = task
            att.uploaded_by = request.user
            att.save()
            messages.success(request, f'File "{att.filename}" attached.')
        else:
            messages.error(request, "Please choose a valid file to upload.")
    return redirect('task_detail', pk=task.pk)


# --- Subtasks / checklist (drives progress when present) ---

@login_required
def add_subtask(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        title = (request.POST.get('title') or '').strip()
        if title:
            SubTask.objects.create(task=task, title=title[:255])
            task.save()  # recompute progress from the checklist
            messages.success(request, "Checklist item added.")
        else:
            messages.error(request, "Enter a checklist item.")
    return redirect('task_detail', pk=task.pk)


@login_required
def toggle_subtask(request, pk):
    sub = get_object_or_404(SubTask, pk=pk)
    task = sub.task
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        sub.is_done = not sub.is_done
        sub.save(update_fields=['is_done'])
        task.save()  # recompute progress from the checklist
    return redirect('task_detail', pk=task.pk)


@login_required
def delete_subtask(request, pk):
    sub = get_object_or_404(SubTask, pk=pk)
    task = sub.task
    # Only a manager or the assigned engineer may remove checklist items.
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        sub.delete()
        task.save()  # recompute progress from the remaining checklist
    return redirect('task_detail', pk=task.pk)


# --- Time logging ---

@login_required
def add_timelog(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not _can_access_task(request.user, task):
        return redirect('task_detail', pk=task.pk)
    if request.method == 'POST':
        form = TimeLogForm(request.POST)
        if form.is_valid():
            log = form.save(commit=False)
            log.task = task
            log.user = request.user
            log.save()
            # Tell managers when an engineer logs time.
            if request.user.role == 'worker':
                notify_managers(
                    f'{request.user.username} logged {log.hours}h on "{task.title}".',
                    reverse('task_detail', args=[task.pk]),
                )
            messages.success(request, f"Logged {log.hours}h.")
        else:
            messages.error(request, form.errors.get('hours', ['Please enter valid hours.'])[0])
    return redirect('task_detail', pk=task.pk)


@login_required
def delete_timelog(request, pk):
    log = get_object_or_404(TimeLog, pk=pk)
    task = log.task
    # The person who logged it, or a manager, may remove it — unless it's paid.
    can = _is_manager(request.user) or log.user_id == request.user.id
    if request.method == 'POST' and can:
        if log.is_paid:
            messages.error(request, "That time entry has been paid and can't be removed.")
        else:
            log.delete()
            messages.success(request, "Time entry removed.")
    return redirect('task_detail', pk=task.pk)


@login_required
def notifications_view(request):
    notes = request.user.notifications.all()
    # Mark all as read once the user opens the panel.
    request.user.notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'core/notifications.html', {'notes': notes})

@login_required
def notification_open(request, pk):
    note = get_object_or_404(Notification, pk=pk, recipient=request.user)
    note.is_read = True
    note.save()
    return redirect(note.link or 'role_redirect')


@login_required
def worker_task_update(request, pk):
    # Ensure the task is assigned to the requesting worker
    task = get_object_or_404(Task, pk=pk, assigned_to=request.user)
    # Capture the original status BEFORE the form binds (ModelForm mutates the
    # instance during validation, so reading it later would give the new value).
    old_status = task.status
    old_progress = task.progress
    if request.method == 'POST':
        form = WorkerTaskStatusForm(request.POST, instance=task)
        if form.is_valid():
            # Workers may update status and their progress estimate only.
            task.status = form.cleaned_data['status']
            task.progress = form.cleaned_data['progress']
            task.save()  # save() reconciles progress with the final status
            # Notify managers when the engineer moves the task forward or
            # reports fresh progress on it.
            link = reverse('task_detail', args=[task.pk])
            if task.status != old_status:
                notify_managers(
                    f'{request.user.username} marked "{task.title}" as {task.get_status_display()}.',
                    link,
                )
            elif task.progress != old_progress:
                notify_managers(
                    f'{request.user.username} updated progress on "{task.title}" to {task.progress}%.',
                    link,
                )
            messages.success(request, "Task updated. Manager notified.")
            return redirect('task_detail', pk=task.pk)
        else:
            messages.error(request, "Please correct the error below.")
    else:
        form = WorkerTaskStatusForm(instance=task)
    return render(request, 'core/worker_task_update.html', {'form': form, 'task': task})


@login_required
def request_progress(request, pk):
    """Manager pings the assigned engineer to ask how close a task is."""
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    task = get_object_or_404(Task, pk=pk)
    if request.method == 'POST':
        link = reverse('worker_task_update', args=[task.pk])
        notify(task.assigned_to,
               f'{request.user.username} is asking how close you are on "{task.title}". Please update your progress.',
               link)
        messages.success(request, f"Progress update requested from {task.assigned_to.username}.")
    return redirect(request.POST.get('next') or reverse('task_detail', args=[task.pk]))


# --- Payment: approval, engineer pay profile, and running payslips ---

@login_required
def task_approve(request, pk):
    """Manager approves a *completed* task so it counts for payment."""
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    task = get_object_or_404(Task, pk=pk)
    if request.method != 'POST':
        return redirect('task_detail', pk=task.pk)
    if task.status != 'completed':
        messages.error(request, "Only a completed task can be approved for payment.")
    elif task.approved:
        messages.info(request, "That task is already approved.")
    else:
        task.approved = True
        task.approved_at = timezone.now()
        task.save(update_fields=['approved', 'approved_at'])
        link = reverse('task_detail', args=[task.pk])
        notify(task.assigned_to, f'Your task "{task.title}" was approved for payment.', link)
        messages.success(request, f'"{task.title}" approved for payment.')
    return redirect(request.POST.get('next') or reverse('task_detail', args=[task.pk]))


@login_required
def approvals(request):
    """One place to approve completed work across ALL workers."""
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    tasks = (Task.objects.filter(status='completed', approved=False)
             .select_related('assigned_to')
             .order_by('assigned_to__username', '-completed_at'))
    return render(request, 'core/approvals.html', {'tasks': tasks})


@login_required
def approve_all(request):
    """Approve every completed, unapproved task (optionally for one worker)."""
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    if request.method == 'POST':
        qs = Task.objects.filter(status='completed', approved=False)
        worker_id = request.POST.get('worker')
        if worker_id:
            qs = qs.filter(assigned_to_id=worker_id)
        count = 0
        for task in qs:
            task.approved = True
            task.approved_at = timezone.now()
            task.save(update_fields=['approved', 'approved_at'])
            notify(task.assigned_to, f'Your task "{task.title}" was approved for payment.',
                   reverse('task_detail', args=[task.pk]))
            count += 1
        messages.success(request, f"Approved {count} task(s) for payment." if count
                         else "No completed tasks were waiting for approval.")
    return redirect('approvals')


@login_required
def engineer_pay_edit(request, pk):
    """Manager sets how an engineer is paid (monthly salary or per-task rate)."""
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    engineer = get_object_or_404(User, pk=pk, role='worker')
    if request.method == 'POST':
        form = EngineerPayForm(request.POST, instance=engineer)
        if form.is_valid():
            form.save()
            messages.success(request, f"Pay settings updated for {engineer.username}.")
            return redirect('engineer_detail', pk=engineer.pk)
        messages.error(request, "Please correct the errors below.")
    else:
        form = EngineerPayForm(instance=engineer)
    return render(request, 'core/engineer_pay_edit.html', {'form': form, 'engineer': engineer})


@login_required
def run_payment(request, pk):
    """Record a payment for an engineer.

    per-task engineer: pays every approved, not-yet-paid task (locks them).
    monthly engineer:  records a payment of their monthly salary.
    """
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    engineer = get_object_or_404(User, pk=pk, role='worker')
    if request.method != 'POST':
        return redirect('engineer_detail', pk=engineer.pk)
    period = (request.POST.get('period') or '').strip()

    with transaction.atomic():
        # Penalty fines deducted from this payslip: a task abandoned while
        # overdue, or (for monthly/hourly engineers) one completed late.
        # Assessed once per task, then settled so it's never charged twice.
        fine_tasks = [
            t for t in Task.objects.select_for_update().filter(
                assigned_to=engineer, fine_settled=False, late_penalty_percent__gt=0)
            if t.payslip_fine > 0
        ]
        fines = sum((t.payslip_fine for t in fine_tasks), Decimal('0'))

        if engineer.pay_type == 'per_task':
            unpaid = list(
                Task.objects.select_for_update()
                .filter(assigned_to=engineer, approved=True, payment__isnull=True)
            )
            if not unpaid:
                messages.error(request, f"{engineer.username} has no approved, unpaid tasks to pay.")
                return redirect('engineer_detail', pk=engineer.pk)
            gross = sum((t.net_pay for t in unpaid), Decimal('0'))  # net of late penalties
            basis, extra = 'per_task', {'task_count': len(unpaid)}
            paid_msg = f"for {len(unpaid)} task(s)"
        elif engineer.pay_type == 'hourly':
            logs = list(
                TimeLog.objects.select_for_update()
                .filter(user=engineer, payment__isnull=True)
            )
            if not logs:
                messages.error(request, f"{engineer.username} has no unpaid logged hours to pay.")
                return redirect('engineer_detail', pk=engineer.pk)
            rate = engineer.hourly_rate or Decimal('0')
            if rate <= 0:
                messages.error(request, f"Set an hourly rate for {engineer.username} first.")
                return redirect('engineer_detail', pk=engineer.pk)
            hours = sum((l.hours or Decimal('0')) for l in logs)
            gross = hours * rate
            basis, extra = 'hourly', {'hours': hours}
            paid_msg = f"for {hours}h"
        else:
            gross = engineer.monthly_salary or Decimal('0')
            if gross <= 0:
                messages.error(request, f"Set a monthly salary for {engineer.username} first.")
                return redirect('engineer_detail', pk=engineer.pk)
            basis, extra = 'monthly', {}
            paid_msg = "monthly salary"

        # Where the money is sent.
        destination = engineer.payout_destination
        if not destination:
            where = "M-Pesa number" if engineer.payout_method == 'mpesa' else "bank account"
            messages.error(request, f"Set {engineer.username}'s {where} before paying (Engineer → pay settings).")
            return redirect('engineer_detail', pk=engineer.pk)

        # Apply the fine (never let a payslip go negative).
        amount = gross - fines
        if amount < 0:
            amount = Decimal('0.00')

        # Disburse: a real Daraja B2C payout if M-Pesa is configured, else simulated.
        pay_status, provider_ref, reference = 'simulated', '', ''
        if engineer.payout_method == 'mpesa':
            res = mpesa.send_b2c(destination, amount,
                                 remarks=f"CodeForge {basis} pay", occasion=period or basis)
            if res.get('mode') == 'live':
                if not res.get('ok'):
                    messages.error(request, f"M-Pesa payout could not be initiated: {res.get('error')}")
                    return redirect('engineer_detail', pk=engineer.pk)  # rolls back the transaction
                pay_status, provider_ref = 'pending', res.get('conversation_id', '')
            else:
                reference = _mock_reference('mpesa')
        else:  # bank — no API, recorded as simulated
            reference = _mock_reference('bank')

        payment = Payment.objects.create(
            engineer=engineer, basis=basis, period=period,
            amount=amount, fine=fines, created_by=request.user,
            method=engineer.payout_method, destination=destination, reference=reference,
            status=pay_status, provider_ref=provider_ref,
            **extra,
        )
        if basis == 'per_task':
            Task.objects.filter(pk__in=[t.pk for t in unpaid]).update(payment=payment)
        elif basis == 'hourly':
            TimeLog.objects.filter(pk__in=[l.pk for l in logs]).update(payment=payment)
        if fine_tasks:
            Task.objects.filter(pk__in=[t.pk for t in fine_tasks]).update(fine_settled=True)
        via = f" via {payment.method_label} to {destination}"
        if pay_status == 'pending':
            msg = (f"M-Pesa payout of KES {amount} to {destination} initiated — "
                   f"awaiting Safaricom confirmation (the real code will appear on the payslip).")
        else:
            msg = (f"Paid {engineer.username} KES {amount} {paid_msg}"
                   + (f" (after KES {fines} in fines)" if fines else "")
                   + via + f" · Ref {reference} (simulated).")

    link = reverse('payment_detail', args=[payment.pk])
    notify(engineer, f'A payment of KES {payment.amount} was recorded for you.', link)
    messages.success(request, msg)
    return redirect('payment_detail', pk=payment.pk)


@csrf_exempt
def mpesa_result(request):
    """Daraja B2C result callback — Safaricom POSTs the final outcome of a payout
    here, including the real M-Pesa transaction code. Matched to a Payment by its
    ConversationID. Always returns 200 so Safaricom doesn't retry forever."""
    import json
    try:
        data = json.loads(request.body or b'{}')
        result = data.get('Result', {})
        conv = result.get('ConversationID') or result.get('OriginatorConversationID')
        payment = Payment.objects.filter(provider_ref=conv).first() if conv else None
        if payment:
            if str(result.get('ResultCode')) == '0':
                payment.status = 'sent'
                # the real M-Pesa code is TransactionID (or in ResultParameters)
                payment.reference = result.get('TransactionID') or payment.reference
            else:
                payment.status = 'failed'
            payment.save(update_fields=['status', 'reference'])
    except Exception:
        pass
    return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@csrf_exempt
def mpesa_timeout(request):
    """Daraja B2C queue-timeout callback. Nothing to do but acknowledge."""
    return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})


@login_required
def payments_list(request):
    """Manager: all payments. Engineer: only their own payslips."""
    if _is_manager(request.user):
        payments = Payment.objects.select_related('engineer', 'created_by').all()
    else:
        payments = Payment.objects.select_related('created_by').filter(engineer=request.user)
    total = payments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return render(request, 'core/payments_list.html', {
        'payments': payments, 'total': total, 'is_manager': _is_manager(request.user),
    })


@login_required
def payment_detail(request, pk):
    """A single payslip. Engineers may only open their own."""
    payment = get_object_or_404(Payment.objects.select_related('engineer', 'created_by'), pk=pk)
    if not _is_manager(request.user) and payment.engineer_id != request.user.id:
        return redirect('payments_list')
    tasks = payment.tasks.all().order_by('-completed_at')
    time_logs = payment.time_logs.select_related('task').all()
    return render(request, 'core/payment_detail.html', {
        'payment': payment, 'tasks': tasks, 'time_logs': time_logs,
        'gross_before_fine': payment.amount + payment.fine,
    })


# ... existing code above unchanged ...

@login_required
def reports_view(request):
    user = request.user
    form = ReportFilterForm(request.GET or None)

    # Build base queryset depending on role
    if user.is_superuser or user.role == 'manager':
        # managers see all tasks, optionally filter by worker
        tasks_qs = Task.objects.all().order_by('-created_at')
    else:
        # workers see only their own tasks
        tasks_qs = Task.objects.filter(assigned_to=user).order_by('-created_at')

    if form.is_valid():
        start_date = form.cleaned_data.get('start_date')
        end_date = form.cleaned_data.get('end_date')
        worker = form.cleaned_data.get('worker')

        if start_date:
            tasks_qs = tasks_qs.filter(created_at__date__gte=start_date)
        if end_date:
            tasks_qs = tasks_qs.filter(created_at__date__lte=end_date)
        # only managers may use the worker filter
        if worker and (user.is_superuser or user.role == 'manager'):
            tasks_qs = tasks_qs.filter(assigned_to=worker)

    # summary counts by status
    summary = tasks_qs.values('status').annotate(count=Count('id'))

    # convenience totals
    total_tasks = tasks_qs.count()
    completed = tasks_qs.filter(status='completed').count()
    in_progress = tasks_qs.filter(status='in_progress').count()
    pending = tasks_qs.filter(status='pending').count()

    context = {
        'form': form,
        'tasks': tasks_qs,
        'summary': summary,
        'total_tasks': total_tasks,
        'completed': completed,
        'in_progress': in_progress,
        'pending': pending,
        'report_user': user,
    }

    # If user requested PDF download: ?format=pdf (xhtml2pdf — Windows-friendly)
    if request.GET.get('format') == 'pdf':
        try:
            from xhtml2pdf import pisa
            from io import BytesIO
        except Exception:
            messages.error(request, "PDF support (xhtml2pdf) is not installed on the server.")
            return redirect('reports')
        html_string = render_to_string('core/report_pdf.html', context, request=request)
        buf = BytesIO()
        result = pisa.CreatePDF(src=html_string, dest=buf, encoding='utf-8')
        if result.err:
            messages.error(request, "Could not generate the PDF report.")
            return redirect('reports')
        filename = f"report-{user.username}-{date.today().isoformat()}.pdf"
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Otherwise show HTML report page with link to download
    return render(request, 'core/reports.html', context)


def _build_manager_report(request):
    """Assemble the on-demand progress + payment report for the PM.

    Computed live from current task/payment state each time it is requested —
    there is no schedule. Can be categorised as a Daily or Weekly report, or a
    custom date window; filters on task creation date.
    """
    from datetime import timedelta
    start = (request.GET.get('start') or '').strip()
    end = (request.GET.get('end') or '').strip()
    period = (request.GET.get('period') or '').strip()   # 'daily' | 'weekly' | ''
    today = timezone.localdate()

    if period == 'daily':
        start = end = today.isoformat()
        period_label = f"Daily report · {today:%A, %d %b %Y}"
    elif period == 'weekly':
        week_start = today - timedelta(days=today.weekday())   # Monday
        start, end = week_start.isoformat(), today.isoformat()
        period_label = f"Weekly report · {week_start:%d %b} – {today:%d %b %Y}"
    elif start or end:
        period_label = f"Custom · {start or '…'} → {end or '…'}"
    else:
        period_label = "All time"

    tasks_qs = Task.objects.all()
    if start:
        tasks_qs = tasks_qs.filter(created_at__date__gte=start)
    if end:
        tasks_qs = tasks_qs.filter(created_at__date__lte=end)

    engineers = User.objects.filter(role='worker').order_by('username')
    rows = []
    tot_earned = tot_paid = Decimal('0')
    tot_est = tot_logged = Decimal('0')
    for e in engineers:
        et = tasks_qs.filter(assigned_to=e)
        total = et.count()
        completed = et.filter(status='completed').count()
        approved = et.filter(approved=True).count()
        # earned = approved tasks' pay (net of late penalties); paid = those on a payslip
        approved_list = list(et.filter(approved=True))
        earned = sum((t.net_pay for t in approved_list), Decimal('0'))
        paid = sum((t.net_pay for t in approved_list if t.is_paid), Decimal('0'))
        overdue = sum(1 for t in et if t.is_overdue)
        # Overall progress = average of every task's progress % (completed=100,
        # pending=0, in-progress = the engineer's reported estimate). This is a
        # truer "how far along" than the done/total count alone.
        avg_progress = et.aggregate(a=Avg('progress'))['a']
        # Time: hours logged against this engineer's tasks vs the estimates.
        est_hours = et.aggregate(s=Sum('estimated_hours'))['s'] or Decimal('0')
        log_hours = TimeLog.objects.filter(task__in=et).aggregate(s=Sum('hours'))['s'] or Decimal('0')
        rows.append({
            'engineer': e,
            'pay_type': e.get_pay_type_display(),
            'total': total,
            'pending': et.filter(status='pending').count(),
            'in_progress': et.filter(status='in_progress').count(),
            'completed': completed,
            'approved': approved,
            'overdue': overdue,
            'completion': round(100 * completed / total) if total else None,
            'avg_progress': round(avg_progress) if avg_progress is not None else None,
            'estimated_hours': est_hours,
            'logged_hours': log_hours,
            'earned': earned,
            'paid': paid,
            'unpaid': earned - paid,
        })
        tot_earned += earned
        tot_paid += paid
        tot_est += est_hours
        tot_logged += log_hours

    total_tasks = tasks_qs.count()
    total_completed = tasks_qs.filter(status='completed').count()
    return {
        'generated_at': timezone.now(),
        'start': start,
        'end': end,
        'period': period,
        'period_label': period_label,
        'rows': rows,
        'total_tasks': total_tasks,
        'total_completed': total_completed,
        'total_completion': round(100 * total_completed / total_tasks) if total_tasks else None,
        'total_earned': tot_earned,
        'total_paid': tot_paid,
        'total_unpaid': tot_earned - tot_paid,
        'total_estimated_hours': tot_est,
        'total_logged_hours': tot_logged,
    }


@login_required
def manager_report(request):
    """On-demand PM report: per-engineer task progress + payment.

    Generated the moment the manager asks for it (a page load / PDF button),
    NOT on a timer. `?format=pdf` streams a PDF built with xhtml2pdf, which
    works on Windows without native libraries.
    """
    if not _is_manager(request.user):
        return redirect('worker_dashboard')
    context = _build_manager_report(request)

    if request.GET.get('format') == 'pdf':
        try:
            from xhtml2pdf import pisa
            from io import BytesIO
        except Exception:
            messages.error(request, "PDF support (xhtml2pdf) is not installed on the server.")
            return redirect('manager_report')
        html = render_to_string('core/manager_report_pdf.html', context, request=request)
        buf = BytesIO()
        result = pisa.CreatePDF(src=html, dest=buf, encoding='utf-8')
        if result.err:
            messages.error(request, "Could not generate the PDF report.")
            return redirect('manager_report')
        filename = f"codeforge-progress-{date.today().isoformat()}.pdf"
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    return render(request, 'core/manager_report.html', context)