from django import forms
from django.forms.widgets import DateInput
from .models import Task, User, TaskComment, TaskAttachment, TimeLog

class ReportFilterForm(forms.Form):
    start_date = forms.DateField(required=False, widget=DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(required=False, widget=DateInput(attrs={'type': 'date'}))
    # manager can filter by worker; for workers this will be ignored
    worker = forms.ModelChoiceField(queryset=User.objects.filter(role='worker'), required=False)


class TaskForm(forms.ModelForm):
    # Optional in the UI: left blank/0 it falls back to the engineer's task rate.
    pay_amount = forms.DecimalField(
        required=False, min_value=0, initial=0, max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': '0'}),
        help_text="Pay for this task (per-task engineers). Leave blank to use the engineer's default task rate.",
    )

    estimated_hours = forms.DecimalField(
        required=False, min_value=0, initial=0, max_digits=7, decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.25', 'min': '0', 'placeholder': '0'}),
        help_text="Estimated hours to complete (optional).",
    )

    late_penalty_percent = forms.IntegerField(
        required=False, min_value=0, max_value=100, initial=0,
        widget=forms.NumberInput(attrs={'min': '0', 'max': '100', 'placeholder': '0'}),
        help_text="Percent deducted from this task's pay if finished after the deadline (0 = none).",
    )

    class Meta:
        model = Task
        fields = ['title', 'description', 'assigned_to', 'status', 'priority',
                  'due_date', 'due_time', 'estimated_hours', 'pay_amount', 'late_penalty_percent']
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'due_time': forms.TimeInput(attrs={'type': 'time'}),
        }

    def clean_late_penalty_percent(self):
        return self.cleaned_data.get('late_penalty_percent') or 0

    def clean_pay_amount(self):
        # The model column is NOT NULL; coerce a blank entry to 0.
        return self.cleaned_data.get('pay_amount') or 0

    def clean_estimated_hours(self):
        return self.cleaned_data.get('estimated_hours') or 0


class EngineerPayForm(forms.ModelForm):
    """Manager-only form to set how an engineer is paid."""
    class Meta:
        model = User
        fields = ['pay_type', 'monthly_salary', 'task_rate', 'hourly_rate']
        widgets = {
            'monthly_salary': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'border rounded px-3 py-2'}),
            'task_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'border rounded px-3 py-2'}),
            'hourly_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'class': 'border rounded px-3 py-2'}),
            'pay_type': forms.Select(attrs={'class': 'border rounded px-3 py-2'}),
        }


class TimeLogForm(forms.ModelForm):
    """An engineer (or the PM) logs hours worked on a task."""
    class Meta:
        model = TimeLog
        fields = ['hours', 'work_date', 'note']
        widgets = {
            'hours': forms.NumberInput(attrs={'step': '0.25', 'min': '0.25', 'placeholder': 'e.g. 2.5',
                                              'class': 'border rounded px-3 py-2 w-full'}),
            'work_date': forms.DateInput(attrs={'type': 'date', 'class': 'border rounded px-3 py-2 w-full'}),
            'note': forms.TextInput(attrs={'placeholder': 'What did you work on? (optional)',
                                           'class': 'border rounded px-3 py-2 w-full'}),
        }

    def clean_hours(self):
        h = self.cleaned_data.get('hours')
        if h is None or h <= 0:
            raise forms.ValidationError("Enter a positive number of hours.")
        return h


class TaskCommentForm(forms.ModelForm):
    class Meta:
        model = TaskComment
        fields = ['body']
        widgets = {
            'body': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Write a comment…',
                'class': 'w-full border rounded px-3 py-2',
            }),
        }


class TaskAttachmentForm(forms.ModelForm):
    class Meta:
        model = TaskAttachment
        fields = ['file']

class WorkerTaskStatusForm(forms.ModelForm):
    # Forward-only lifecycle: pending -> in_progress -> completed (no skipping).
    ALLOWED_TRANSITIONS = {
        'pending': {'in_progress'},
        'in_progress': {'completed'},
        'completed': set(),
    }

    # How close to finishing (0–100). Rendered as a slider on the update page.
    progress = forms.IntegerField(
        required=False, min_value=0, max_value=100, initial=0,
        widget=forms.NumberInput(attrs={
            'type': 'range', 'min': '0', 'max': '100', 'step': '5',
            'class': 'w-full', 'oninput': "document.getElementById('progressVal').textContent=this.value+'%'",
        }),
        help_text="Only used while a task is In Progress. Completed = 100%.",
    )

    class Meta:
        model = Task
        fields = ['status', 'progress']

    def clean_progress(self):
        return self.cleaned_data.get('progress') or 0

    def clean_status(self):
        new_status = self.cleaned_data['status']
        # instance still holds the DB value here (clean runs before the instance
        # is mutated), so this is the task's current status.
        current = self.instance.status if self.instance and self.instance.pk else 'pending'

        labels = dict(Task.STATUS_CHOICES)
        if new_status == current:
            return new_status
        if new_status not in self.ALLOWED_TRANSITIONS.get(current, set()):
            raise forms.ValidationError(
                "Invalid status change: a task that is '%s' cannot be moved to "
                "'%s'. Tasks must progress in order: Pending → In Progress → Completed."
                % (labels.get(current, current), labels.get(new_status, new_status))
            )
        return new_status