from django.urls import path
from .views import (
    manager_dashboard,
    worker_dashboard,
    engineer_list,
    engineer_detail,
    TaskListView,
    TaskDetailView,
    TaskCreateView,
    TaskUpdateView,
    TaskDeleteView,
    worker_task_update,
    role_redirect,
    not_member,
    reports_view,
    notifications_view,
    notification_open,
    add_comment,
    add_attachment,
    add_subtask,
    toggle_subtask,
    delete_subtask,
    task_approve,
    engineer_pay_edit,
    run_payment,
    payments_list,
    payment_detail,
    manager_report,
    request_progress,
)

urlpatterns = [
    path('', role_redirect, name='role_redirect'),
    path('not-member/', not_member, name='not_member'),

    # Dashboards
    path('dashboard/manager/', manager_dashboard, name='manager_dashboard'),
    path('dashboard/worker/', worker_dashboard, name='worker_dashboard'),

    # Engineers panel (managers only)
    path('engineers/', engineer_list, name='engineer_list'),
    path('engineers/<int:pk>/', engineer_detail, name='engineer_detail'),

    # Task Views
    path('tasks/', TaskListView.as_view(), name='task_list'),
    path('tasks/<int:pk>/', TaskDetailView.as_view(), name='task_detail'),
    path('tasks/create/', TaskCreateView.as_view(), name='task_create'),
    path('tasks/<int:pk>/update/', TaskUpdateView.as_view(), name='task_update'),
    path('tasks/<int:pk>/delete/', TaskDeleteView.as_view(), name='task_delete'),

    # Worker can update task status + progress only
    path('tasks/<int:pk>/worker-update/', worker_task_update, name='worker_task_update'),
    # Manager asks the assigned engineer how close a task is
    path('tasks/<int:pk>/request-progress/', request_progress, name='request_progress'),

    # Task collaboration: comments & file attachments
    path('tasks/<int:pk>/comment/', add_comment, name='add_comment'),
    path('tasks/<int:pk>/attach/', add_attachment, name='add_attachment'),

    # Subtasks / checklist
    path('tasks/<int:pk>/subtask/add/', add_subtask, name='add_subtask'),
    path('subtasks/<int:pk>/toggle/', toggle_subtask, name='toggle_subtask'),
    path('subtasks/<int:pk>/delete/', delete_subtask, name='delete_subtask'),

    # Notifications
    path('notifications/', notifications_view, name='notifications'),
    path('notifications/<int:pk>/open/', notification_open, name='notification_open'),

    # Reports (HTML view and PDF export via ?format=pdf)
    path('reports/', reports_view, name='reports'),

    # Payment: approve a completed task, set engineer pay, run payslips
    path('tasks/<int:pk>/approve/', task_approve, name='task_approve'),
    path('engineers/<int:pk>/pay-settings/', engineer_pay_edit, name='engineer_pay_edit'),
    path('engineers/<int:pk>/pay/', run_payment, name='run_payment'),
    path('payments/', payments_list, name='payments_list'),
    path('payments/<int:pk>/', payment_detail, name='payment_detail'),

    # On-demand PM progress + payment report (HTML, and PDF via ?format=pdf)
    path('pm-report/', manager_report, name='manager_report'),
]