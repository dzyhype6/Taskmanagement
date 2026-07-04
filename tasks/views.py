from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.template.loader import render_to_string
from django.http import HttpResponse
from django.db.models import Count
from datetime import date

try:
    from weasyprint import HTML, CSS
except Exception:  # pragma: no cover
    HTML = None
    CSS = None


from django.urls import reverse
from django.db.models import Q
from .models import Task, User, Notification
from .forms import (
    TaskForm, WorkerTaskStatusForm, ReportFilterForm,
    TaskCommentForm, TaskAttachmentForm,
)
from .services import notify, notify_managers
from django.contrib import messages


# --- Role redirect and not member view ---
@login_required
def role_redirect(request):
    user = request.user
    if user.role == 'manager':
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
    if user.role != 'manager':
        return redirect('worker_dashboard')
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
    return render(request, 'core/engineer_detail.html', {'engineer': engineer, 'tasks': tasks})

@login_required
def worker_dashboard(request):
    user = request.user
    if user.role != 'worker':
        return redirect('manager_dashboard')
    tasks = Task.objects.filter(assigned_to=user)
    context = {
        'tasks': tasks,
        'worker': user,
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
        if task.assigned_to_id != old_assignee_id:
            link = reverse('task_detail', args=[task.pk])
            notify(task.assigned_to, f'A task was assigned to you: "{task.title}"', link)
        messages.success(self.request, "Task updated successfully.")
        return response

class TaskDeleteView(LoginRequiredMixin, ManagerRequiredMixin, DeleteView):
    model = Task
    template_name = 'core/task_confirm_delete.html'
    success_url = reverse_lazy('task_list')

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
    if request.method == 'POST':
        form = WorkerTaskStatusForm(request.POST, instance=task)
        if form.is_valid():
            # Only allow status update for workers
            task.status = form.cleaned_data['status']
            task.save()
            # Notify managers when the engineer moves the task forward.
            if task.status != old_status:
                status_label = task.get_status_display()
                link = reverse('task_detail', args=[task.pk])
                notify_managers(
                    f'{request.user.username} marked "{task.title}" as {status_label}.',
                    link,
                )
            messages.success(request, "Task status updated. Manager notified.")
            return redirect('task_detail', pk=task.pk)
        else:
            messages.error(request, "Please correct the error below.")
    else:
        form = WorkerTaskStatusForm(instance=task)
    return render(request, 'core/worker_task_update.html', {'form': form, 'task': task})



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

    # If user requested PDF download: ?format=pdf
    if request.GET.get('format') == 'pdf' and HTML is not None:
        # Render a printable HTML version
        html_string = render_to_string('core/report_pdf.html', context, request=request)
        html = HTML(string=html_string, base_url=request.build_absolute_uri('/'))
        css = CSS(string='''
            @page { size: A4; margin: 1cm; }
            body { font-family: "Helvetica", Arial, sans-serif; font-size: 12px; }
            h1, h2 { color: #0b5cff; }
            table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
            th, td { padding: 6px 8px; border: 1px solid #ddd; }
            th { background: #f0f8ff; }
        ''')
        pdf_bytes = html.write_pdf(stylesheets=[css])

        filename = f"report-{user.username}-{date.today().isoformat()}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Otherwise show HTML report page with link to download
    return render(request, 'core/reports.html', context)