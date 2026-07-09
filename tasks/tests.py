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
            pay_type='per_task', task_rate=Decimal('500'), mpesa_phone='0700000001')
        self.salaried = User.objects.create_user(
            username='salaried', password='pass12345', role='worker',
            pay_type='monthly', monthly_salary=Decimal('30000'), mpesa_phone='0700000002')
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


class ProgressTrackingTests(TestCase):
    def setUp(self):
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker')

    def test_status_syncs_progress(self):
        t = Task.objects.create(title='t', assigned_to=self.eng, status='pending', progress=40)
        self.assertEqual(t.progress, 0)          # pending forces 0
        t.status = 'in_progress'; t.progress = 60; t.save()
        self.assertEqual(t.progress, 60)         # in-progress keeps the estimate
        t.status = 'completed'; t.save()
        self.assertEqual(t.progress, 100)        # completed forces 100

    def test_in_progress_progress_capped_below_100(self):
        t = Task.objects.create(title='t', assigned_to=self.eng, status='in_progress', progress=150)
        self.assertEqual(t.progress, 99)         # never 100 unless completed

    def test_worker_updates_progress(self):
        t = Task.objects.create(title='t', assigned_to=self.eng, status='pending')
        self.client.login(username='eng', password='pw')
        # move to in_progress at 45%
        resp = self.client.post(reverse('worker_task_update', args=[t.pk]),
                                {'status': 'in_progress', 'progress': '45'})
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertEqual(t.status, 'in_progress')
        self.assertEqual(t.progress, 45)

    def test_manager_can_request_progress(self):
        t = Task.objects.create(title='t', assigned_to=self.eng, status='in_progress', progress=20)
        self.client.login(username='pm', password='pw')
        before = self.eng.notifications.count()
        self.client.post(reverse('request_progress', args=[t.pk]))
        self.assertEqual(self.eng.notifications.count(), before + 1)

    def test_report_average_progress(self):
        Task.objects.create(title='a', assigned_to=self.eng, status='completed')        # 100
        Task.objects.create(title='b', assigned_to=self.eng, status='in_progress', progress=50)  # 50
        Task.objects.create(title='c', assigned_to=self.eng, status='pending')          # 0
        self.client.login(username='pm', password='pw')
        resp = self.client.get(reverse('manager_report'))
        row = next(r for r in resp.context['rows'] if r['engineer'].id == self.eng.id)
        self.assertEqual(row['avg_progress'], 50)   # mean(100,50,0)


class SubtaskTests(TestCase):
    def setUp(self):
        from tasks.models import SubTask
        self.SubTask = SubTask
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker')
        self.task = Task.objects.create(title='t', assigned_to=self.eng, status='in_progress')

    def test_checklist_drives_progress(self):
        self.client.login(username='pm', password='pw')
        for name in ['a', 'b', 'c', 'd']:
            self.client.post(reverse('add_subtask', args=[self.task.pk]), {'title': name})
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress, 0)          # nothing ticked yet
        subs = list(self.task.subtasks.all())
        # tick two of four -> 50%
        self.client.post(reverse('toggle_subtask', args=[subs[0].pk]))
        self.client.post(reverse('toggle_subtask', args=[subs[1].pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress, 50)

    def test_all_ticked_caps_below_100_until_completed(self):
        s1 = self.SubTask.objects.create(task=self.task, title='x', is_done=True)
        s2 = self.SubTask.objects.create(task=self.task, title='y', is_done=True)
        self.task.save()
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress, 99)         # 100% checklist but still in progress
        self.task.status = 'completed'; self.task.save()
        self.assertEqual(self.task.progress, 100)

    def test_checklist_overrides_self_report(self):
        self.SubTask.objects.create(task=self.task, title='x', is_done=True)
        self.SubTask.objects.create(task=self.task, title='y', is_done=False)
        # worker tries to self-report 90%, but the checklist says 50%
        self.client.login(username='eng', password='pw')
        self.client.post(reverse('worker_task_update', args=[self.task.pk]),
                         {'status': 'in_progress', 'progress': '90'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.progress, 50)

    def test_only_assignee_or_manager_edits_checklist(self):
        other = User.objects.create_user(username='other', password='pw', role='worker')
        self.client.login(username='other', password='pw')
        self.client.post(reverse('add_subtask', args=[self.task.pk]), {'title': 'nope'})
        self.assertEqual(self.task.subtasks.count(), 0)


class WorkerPayVisibilityTests(TestCase):
    def test_completed_and_paid_shown_on_worker_dashboard(self):
        from tasks.models import Payment
        eng = User.objects.create_user(username='eng', password='pw', role='worker',
                                       pay_type='per_task', task_rate=Decimal('500'))
        pay = Payment.objects.create(engineer=eng, basis='per_task',
                                     amount=Decimal('500'), task_count=1)
        Task.objects.create(title='done job', assigned_to=eng, status='completed',
                            approved=True, pay_amount=Decimal('500'), payment=pay)
        self.client.login(username='eng', password='pw')
        resp = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'done job')
        self.assertContains(resp, 'Completed work')
        self.assertEqual(resp.context['total_paid'], Decimal('500'))

    def test_engineer_does_not_see_per_task_amount(self):
        eng = User.objects.create_user(username='e2', password='pw', role='worker',
                                       pay_type='per_task', task_rate=Decimal('500'))
        pm = User.objects.create_user(username='pm2', password='pw', role='manager')
        task = Task.objects.create(title='secret', assigned_to=eng, status='completed',
                                   approved=True, pay_amount=Decimal('12345'))
        # engineer must NOT see the per-task amount on the task page
        self.client.login(username='e2', password='pw')
        self.assertNotContains(self.client.get(reverse('task_detail', args=[task.pk])), '12345')
        # the manager DOES
        self.client.logout(); self.client.login(username='pm2', password='pw')
        self.assertContains(self.client.get(reverse('task_detail', args=[task.pk])), '12345')

    def test_engineer_payslip_shows_total_not_per_task(self):
        from tasks.models import Payment
        eng = User.objects.create_user(username='e3', password='pw', role='worker', pay_type='per_task')
        pay = Payment.objects.create(engineer=eng, basis='per_task', amount=Decimal('700'), task_count=2)
        Task.objects.create(title='p1', assigned_to=eng, status='completed', approved=True,
                            pay_amount=Decimal('321'), payment=pay)
        Task.objects.create(title='p2', assigned_to=eng, status='completed', approved=True,
                            pay_amount=Decimal('379'), payment=pay)
        self.client.login(username='e3', password='pw')
        resp = self.client.get(reverse('payment_detail', args=[pay.pk]))
        self.assertContains(resp, '700')      # the payslip total is a total -> visible
        self.assertNotContains(resp, '321')   # per-task amounts hidden
        self.assertNotContains(resp, '379')


class TimeTrackingTests(TestCase):
    def setUp(self):
        from tasks.models import TimeLog
        self.TimeLog = TimeLog
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker',
                                             pay_type='hourly', hourly_rate=Decimal('20'),
                                             mpesa_phone='0700000003')
        self.task = Task.objects.create(title='t', assigned_to=self.eng, status='in_progress',
                                        estimated_hours=Decimal('10'))

    def test_engineer_logs_time(self):
        self.client.login(username='eng', password='pw')
        resp = self.client.post(reverse('add_timelog', args=[self.task.pk]),
                                {'hours': '2.5', 'work_date': '2026-07-05', 'note': 'setup'})
        self.assertEqual(resp.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.logged_hours, Decimal('2.5'))

    def test_hours_reject_zero(self):
        self.client.login(username='eng', password='pw')
        self.client.post(reverse('add_timelog', args=[self.task.pk]),
                         {'hours': '0', 'work_date': '2026-07-05'})
        self.assertEqual(self.task.time_logs.count(), 0)

    def test_hourly_payment_pays_logged_hours(self):
        from tasks.models import Payment
        self.TimeLog.objects.create(task=self.task, user=self.eng, hours=Decimal('3'))
        self.TimeLog.objects.create(task=self.task, user=self.eng, hours=Decimal('1.5'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]), {'period': 'wk1'})
        pay = Payment.objects.get(engineer=self.eng)
        self.assertEqual(pay.basis, 'hourly')
        self.assertEqual(pay.hours, Decimal('4.5'))
        self.assertEqual(pay.amount, Decimal('90.00'))   # 4.5h × 20
        # logs are locked to the payslip
        self.assertTrue(all(l.payment_id == pay.id for l in self.task.time_logs.all()))
        # a second run finds nothing unpaid
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        self.assertEqual(Payment.objects.filter(engineer=self.eng).count(), 1)

    def test_paid_time_cannot_be_removed(self):
        from tasks.models import Payment
        log = self.TimeLog.objects.create(task=self.task, user=self.eng, hours=Decimal('2'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        # try to delete the now-paid log
        self.client.post(reverse('delete_timelog', args=[log.pk]))
        self.assertTrue(self.TimeLog.objects.filter(pk=log.pk).exists())

    def test_hourly_needs_rate(self):
        from tasks.models import Payment
        self.eng.hourly_rate = Decimal('0'); self.eng.save()
        self.TimeLog.objects.create(task=self.task, user=self.eng, hours=Decimal('2'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        self.assertFalse(Payment.objects.filter(engineer=self.eng).exists())


class LatePenaltyTests(TestCase):
    def setUp(self):
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker',
                                             pay_type='per_task', task_rate=Decimal('1000'),
                                             mpesa_phone='0700000004')

    def _completed_task(self, late, penalty=20):
        from datetime import timedelta
        due = timezone.localdate() - timedelta(days=1)  # due yesterday
        t = Task.objects.create(title='t', assigned_to=self.eng, status='in_progress',
                                due_date=due, pay_amount=Decimal('1000'),
                                late_penalty_percent=penalty)
        t.status = 'completed'
        t.save()  # stamps completed_at = now (which is after yesterday's deadline => late)
        if not late:
            # move the deadline into the future so it's not late
            t.due_date = timezone.localdate() + timedelta(days=1)
            t.save()
        return t

    def test_late_task_incurs_penalty(self):
        t = self._completed_task(late=True, penalty=20)
        self.assertTrue(t.was_late)
        self.assertEqual(t.penalty_amount, Decimal('200.00'))   # 20% of 1000
        self.assertEqual(t.net_pay, Decimal('800.00'))

    def test_on_time_task_full_pay(self):
        t = self._completed_task(late=False, penalty=20)
        self.assertFalse(t.was_late)
        self.assertEqual(t.penalty_amount, Decimal('0.00'))
        self.assertEqual(t.net_pay, Decimal('1000'))

    def test_payment_uses_net_after_penalty(self):
        from tasks.models import Payment
        t = self._completed_task(late=True, penalty=25)
        t.approved = True; t.approved_at = timezone.now(); t.save()
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        pay = Payment.objects.get(engineer=self.eng)
        self.assertEqual(pay.amount, Decimal('750.00'))   # 1000 − 25%

    def test_no_penalty_when_percent_zero(self):
        t = self._completed_task(late=True, penalty=0)
        self.assertTrue(t.was_late)
        self.assertEqual(t.net_pay, Decimal('1000'))


class AbandonmentFineTests(TestCase):
    def setUp(self):
        from datetime import timedelta
        self.timedelta = timedelta
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker',
                                             pay_type='per_task', task_rate=Decimal('1000'),
                                             mpesa_phone='0700000004')

    def _overdue_incomplete(self, penalty=30):
        return Task.objects.create(
            title='abandoned', assigned_to=self.eng, status='in_progress',
            due_date=timezone.localdate() - self.timedelta(days=1),
            pay_amount=Decimal('1000'), late_penalty_percent=penalty)

    def test_incomplete_overdue_has_fine(self):
        t = self._overdue_incomplete(30)
        self.assertEqual(t.abandon_fine, Decimal('300.00'))   # 30% of 1000

    def test_fine_deducted_from_other_pay(self):
        from tasks.models import Payment
        self._overdue_incomplete(30)                          # KES 300 fine
        good = Task.objects.create(title='good', assigned_to=self.eng, status='completed',
                                   approved=True, pay_amount=Decimal('1000'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        pay = Payment.objects.get(engineer=self.eng)
        self.assertEqual(pay.fine, Decimal('300.00'))
        self.assertEqual(pay.amount, Decimal('700.00'))       # 1000 earned − 300 fine

    def test_fine_charged_once(self):
        from tasks.models import Payment
        t = self._overdue_incomplete(30)
        Task.objects.create(title='g1', assigned_to=self.eng, status='completed',
                            approved=True, pay_amount=Decimal('1000'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        t.refresh_from_db()
        self.assertTrue(t.fine_settled)
        self.assertEqual(t.abandon_fine, Decimal('0.00'))     # settled -> no further fine
        # a later payment doesn't re-charge it
        Task.objects.create(title='g2', assigned_to=self.eng, status='completed',
                            approved=True, pay_amount=Decimal('500'))
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        second = Payment.objects.filter(engineer=self.eng).order_by('-id').first()
        self.assertEqual(second.fine, Decimal('0.00'))
        self.assertEqual(second.amount, Decimal('500.00'))

    def test_payslip_not_negative(self):
        from tasks.models import Payment
        self._overdue_incomplete(100)                         # KES 1000 fine
        Task.objects.create(title='small', assigned_to=self.eng, status='completed',
                            approved=True, pay_amount=Decimal('200'))
        self.client.login(username='pm', password='pw')
        self.client.post(reverse('run_payment', args=[self.eng.pk]))
        pay = Payment.objects.get(engineer=self.eng)
        self.assertEqual(pay.amount, Decimal('0.00'))         # floored, never negative


class DeadlineTimeTests(TestCase):
    def test_overdue_uses_time_of_day(self):
        from datetime import time as dtime
        eng = User.objects.create_user(username='e', password='p', role='worker')
        today = timezone.localdate()
        # due today but at 00:01 -> already past -> overdue
        early = Task.objects.create(title='early', assigned_to=eng, status='pending',
                                    due_date=today, due_time=dtime(0, 1))
        # due today at 23:59 -> not yet past -> not overdue
        late = Task.objects.create(title='late', assigned_to=eng, status='pending',
                                   due_date=today, due_time=dtime(23, 59))
        self.assertTrue(early.is_overdue)
        self.assertFalse(late.is_overdue)


class PayoutMethodTests(TestCase):
    def setUp(self):
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.client.login(username='pm', password='pw')

    def test_mpesa_payment_records_channel_and_ref(self):
        from tasks.models import Payment
        eng = User.objects.create_user(username='m', password='pw', role='worker',
                                       pay_type='monthly', monthly_salary=Decimal('30000'),
                                       payout_method='mpesa', mpesa_phone='0712345678')
        self.client.post(reverse('run_payment', args=[eng.pk]))
        pay = Payment.objects.get(engineer=eng)
        self.assertEqual(pay.method, 'mpesa')
        self.assertEqual(pay.destination, '0712345678')
        self.assertEqual(len(pay.reference), 10)     # M-Pesa-style code

    def test_bank_payment_records_account(self):
        from tasks.models import Payment
        eng = User.objects.create_user(username='bk', password='pw', role='worker',
                                       pay_type='monthly', monthly_salary=Decimal('30000'),
                                       payout_method='bank', bank_name='KCB', bank_account='998877')
        self.client.post(reverse('run_payment', args=[eng.pk]))
        pay = Payment.objects.get(engineer=eng)
        self.assertEqual(pay.method, 'bank')
        self.assertIn('998877', pay.destination)
        self.assertIn('KCB', pay.destination)
        self.assertTrue(pay.reference.startswith('BNK'))

    def test_payment_blocked_without_destination(self):
        from tasks.models import Payment
        eng = User.objects.create_user(username='nd', password='pw', role='worker',
                                       pay_type='monthly', monthly_salary=Decimal('30000'),
                                       payout_method='mpesa')  # no phone set
        self.client.post(reverse('run_payment', args=[eng.pk]))
        self.assertFalse(Payment.objects.filter(engineer=eng).exists())


class ReportCategoryTests(TestCase):
    def setUp(self):
        self.pm = User.objects.create_user(username='pm', password='pw', role='manager')
        self.eng = User.objects.create_user(username='eng', password='pw', role='worker')
        self.client.login(username='pm', password='pw')

    def test_daily_report_window_is_today(self):
        resp = self.client.get(reverse('manager_report'), {'period': 'daily'})
        today = timezone.localdate().isoformat()
        self.assertEqual(resp.context['start'], today)
        self.assertEqual(resp.context['end'], today)
        self.assertIn('Daily report', resp.context['period_label'])

    def test_weekly_report_window_is_this_week(self):
        from datetime import timedelta
        resp = self.client.get(reverse('manager_report'), {'period': 'weekly'})
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        self.assertEqual(resp.context['start'], monday.isoformat())
        self.assertEqual(resp.context['end'], today.isoformat())
        self.assertIn('Weekly report', resp.context['period_label'])

    def test_all_time_default(self):
        resp = self.client.get(reverse('manager_report'))
        self.assertEqual(resp.context['period_label'], 'All time')

    def test_daily_pdf_downloads(self):
        resp = self.client.get(reverse('manager_report'), {'period': 'daily', 'format': 'pdf'})
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertEqual(resp.content[:4], b'%PDF')


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
