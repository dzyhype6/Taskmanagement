from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from tasks.models import User, Task, Payment


class PaymentFlowTests(TestCase):
    """End-to-end coverage for per-task / monthly pay, approval and payslips."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username='pm', password='pass12345', role='manager')
        self.casual = User.objects.create_user(
            username='casual', password='pass12345', role='worker',
            pay_type='per_task', task_rate=Decimal('500'))
        self.salaried = User.objects.create_user(
            username='salaried', password='pass12345', role='worker',
            pay_type='monthly', monthly_salary=Decimal('30000'))
        self.client.login(username='pm', password='pass12345')

    # --- task creation fills per-task pay from the engineer's rate ---
    def test_create_task_defaults_pay_to_rate(self):
        resp = self.client.post(reverse('task_create'), {
            'title': 'Fix build', 'description': '', 'assigned_to': self.casual.id,
            'status': 'pending', 'priority': 'medium', 'due_date': '', 'pay_amount': '0',
        })
        self.assertEqual(resp.status_code, 302)
        task = Task.objects.get(title='Fix build')
        self.assertEqual(task.pay_amount, Decimal('500'))

    def test_create_task_without_pay_field_submits(self):
        # Regression: the form must accept a submission that omits pay_amount
        # entirely (blank on the page) and fall back to the engineer's rate.
        resp = self.client.post(reverse('task_create'), {
            'title': 'No pay field', 'description': '', 'assigned_to': self.casual.id,
            'status': 'pending', 'priority': 'medium', 'due_date': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Task.objects.get(title='No pay field').pay_amount, Decimal('500'))

    def test_create_task_keeps_explicit_pay(self):
        self.client.post(reverse('task_create'), {
            'title': 'Big job', 'description': '', 'assigned_to': self.casual.id,
            'status': 'pending', 'priority': 'high', 'due_date': '', 'pay_amount': '1200',
        })
        self.assertEqual(Task.objects.get(title='Big job').pay_amount, Decimal('1200'))

    # --- approval rules ---
    def test_only_completed_tasks_can_be_approved(self):
        task = Task.objects.create(title='T', assigned_to=self.casual,
                                   status='pending', pay_amount=Decimal('500'))
        self.client.post(reverse('task_approve', args=[task.pk]))
        task.refresh_from_db()
        self.assertFalse(task.approved)  # pending cannot be approved

        task.status = 'completed'
        task.save()
        self.client.post(reverse('task_approve', args=[task.pk]))
        task.refresh_from_db()
        self.assertTrue(task.approved)
        self.assertIsNotNone(task.approved_at)

    def test_worker_cannot_approve(self):
        task = Task.objects.create(title='T', assigned_to=self.casual,
                                   status='completed', pay_amount=Decimal('500'))
        self.client.logout()
        self.client.login(username='casual', password='pass12345')
        self.client.post(reverse('task_approve', args=[task.pk]))
        task.refresh_from_db()
        self.assertFalse(task.approved)

    def test_moving_out_of_completed_drops_unpaid_approval(self):
        task = Task.objects.create(title='T', assigned_to=self.casual,
                                   status='completed', approved=True,
                                   approved_at=timezone.now(), pay_amount=Decimal('500'))
        task.status = 'in_progress'
        task.save()
        task.refresh_from_db()
        self.assertFalse(task.approved)
        self.assertIsNone(task.completed_at)

    # --- per-task payment pays approved unpaid tasks and locks them ---
    def test_per_task_payment(self):
        t1 = Task.objects.create(title='A', assigned_to=self.casual,
                                 status='completed', approved=True, pay_amount=Decimal('500'))
        t2 = Task.objects.create(title='B', assigned_to=self.casual,
                                 status='completed', approved=True, pay_amount=Decimal('1200'))
        # an approved task for a DIFFERENT engineer must not be swept in
        Task.objects.create(title='C', assigned_to=self.salaried,
                            status='completed', approved=True, pay_amount=Decimal('999'))

        self.client.post(reverse('run_payment', args=[self.casual.pk]),
                         {'period': 'July 2026'})
        payment = Payment.objects.get(engineer=self.casual)
        self.assertEqual(payment.basis, 'per_task')
        self.assertEqual(payment.amount, Decimal('1700'))
        self.assertEqual(payment.task_count, 2)
        t1.refresh_from_db(); t2.refresh_from_db()
        self.assertEqual(t1.payment_id, payment.id)
        self.assertEqual(t2.payment_id, payment.id)

        # a second run finds nothing left to pay
        self.client.post(reverse('run_payment', args=[self.casual.pk]))
        self.assertEqual(Payment.objects.filter(engineer=self.casual).count(), 1)

    def test_unapproved_tasks_are_not_paid(self):
        Task.objects.create(title='A', assigned_to=self.casual,
                            status='completed', approved=False, pay_amount=Decimal('500'))
        self.client.post(reverse('run_payment', args=[self.casual.pk]))
        self.assertFalse(Payment.objects.filter(engineer=self.casual).exists())

    def test_paid_task_cannot_be_edited_or_deleted(self):
        task = Task.objects.create(title='A', assigned_to=self.casual,
                                   status='completed', approved=True, pay_amount=Decimal('500'))
        self.client.post(reverse('run_payment', args=[self.casual.pk]))
        task.refresh_from_db()
        self.assertTrue(task.is_paid)
        # update/delete views are scoped to unpaid tasks -> 404
        self.assertEqual(self.client.get(reverse('task_update', args=[task.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('task_delete', args=[task.pk])).status_code, 404)

    # --- monthly payment pays the salary, no tasks attached ---
    def test_monthly_payment(self):
        self.client.post(reverse('run_payment', args=[self.salaried.pk]),
                         {'period': 'July 2026'})
        payment = Payment.objects.get(engineer=self.salaried)
        self.assertEqual(payment.basis, 'monthly')
        self.assertEqual(payment.amount, Decimal('30000'))
        self.assertEqual(payment.task_count, 0)

    def test_monthly_payment_requires_salary(self):
        self.salaried.monthly_salary = Decimal('0')
        self.salaried.save()
        self.client.post(reverse('run_payment', args=[self.salaried.pk]))
        self.assertFalse(Payment.objects.filter(engineer=self.salaried).exists())

    # --- the on-demand PM report aggregates correctly ---
    def test_manager_report_aggregates(self):
        Task.objects.create(title='A', assigned_to=self.casual,
                            status='completed', approved=True, pay_amount=Decimal('500'))
        Task.objects.create(title='B', assigned_to=self.casual,
                            status='in_progress', pay_amount=Decimal('500'))
        Task.objects.create(title='C', assigned_to=self.casual, status='pending')
        resp = self.client.get(reverse('manager_report'))
        self.assertEqual(resp.status_code, 200)
        row = next(r for r in resp.context['rows'] if r['engineer'].id == self.casual.id)
        self.assertEqual(row['total'], 3)
        self.assertEqual(row['completed'], 1)
        self.assertEqual(row['approved'], 1)
        self.assertEqual(row['completion'], 33)  # 1 of 3
        self.assertEqual(row['earned'], Decimal('500'))
        self.assertEqual(row['unpaid'], Decimal('500'))

    def test_manager_report_pdf_downloads(self):
        resp = self.client.get(reverse('manager_report'), {'format': 'pdf'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertEqual(resp.content[:4], b'%PDF')

    def test_worker_blocked_from_report_and_pay(self):
        self.client.logout()
        self.client.login(username='casual', password='pass12345')
        # redirected away (302) rather than seeing the manager report / pay endpoints
        self.assertEqual(self.client.get(reverse('manager_report')).status_code, 302)
        self.assertEqual(self.client.post(reverse('run_payment', args=[self.casual.pk])).status_code, 302)


class TaskOverdueTests(TestCase):
    def test_is_overdue(self):
        eng = User.objects.create_user(username='e', password='p', role='worker')
        past = timezone.localdate() - timezone.timedelta(days=2)
        future = timezone.localdate() + timezone.timedelta(days=2)
        overdue = Task.objects.create(title='o', assigned_to=eng, status='pending', due_date=past)
        done = Task.objects.create(title='d', assigned_to=eng, status='completed', due_date=past)
        upcoming = Task.objects.create(title='u', assigned_to=eng, status='pending', due_date=future)
        self.assertTrue(overdue.is_overdue)
        self.assertFalse(done.is_overdue)     # completed is never overdue
        self.assertFalse(upcoming.is_overdue)
