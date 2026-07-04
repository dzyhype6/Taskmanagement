from django import forms
from django.forms.widgets import DateInput
from .models import Task, User, TaskComment, TaskAttachment

class ReportFilterForm(forms.Form):
    start_date = forms.DateField(required=False, widget=DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(required=False, widget=DateInput(attrs={'type': 'date'}))
    # manager can filter by worker; for workers this will be ignored
    worker = forms.ModelChoiceField(queryset=User.objects.filter(role='worker'), required=False)


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ['title', 'description', 'assigned_to', 'status', 'priority', 'due_date']
        widgets = {
            'due_date': forms.DateInput(attrs={'type': 'date'}),
        }


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

    class Meta:
        model = Task
        fields = ['status']

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