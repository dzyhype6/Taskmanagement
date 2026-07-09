"""Populate the system with demo engineers, tasks, logged time and PAID payslips
so you can demonstrate the whole pay flow live.

    python manage.py seed_demo            # (re)create the demo data
    python manage.py seed_demo --reset    # remove the demo data and stop

Demo engineers (password: demo1234): amina (per-task), brian (hourly),
carol (monthly). Running it again refreshes the demo cleanly.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from tasks.models import User, Task, SubTask, TimeLog, Payment

DEMO_USERS = ['amina', 'brian', 'carol']
PW = 'demo1234'


class Command(BaseCommand):
    help = "Create demo engineers, tasks and paid payslips for a live demo."

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help="Delete the demo data and exit.")

    def handle(self, *args, **opts):
        # Wipe any previous demo users (cascades their tasks, logs and payments).
        User.objects.filter(username__in=DEMO_USERS).delete()
        if opts['reset']:
            self.stdout.write(self.style.SUCCESS("Demo data removed."))
            return

        today = timezone.localdate()
        yesterday = today - timedelta(days=1)

        # A manager to record the payments: reuse an existing one, else make one.
        pm = User.objects.filter(role='manager').first()
        if not pm:
            pm = User.objects.create_user(username='demo_pm', password=PW, role='manager')

        # ---- 1) Per-task engineer: paid per approved task, with a fine + penalty ----
        amina = User.objects.create_user(
            username='amina', password=PW, role='worker',
            first_name='Amina', pay_type='per_task', task_rate=Decimal('1500'))

        a = Task.objects.create(title='Build the login page', assigned_to=amina,
                                status='completed', approved=True, pay_amount=Decimal('1500'))
        # completed LATE (due yesterday) with a 20% penalty -> net 1200
        b = Task.objects.create(title='Wire up the payments API', assigned_to=amina,
                                status='completed', approved=True, pay_amount=Decimal('1500'),
                                due_date=yesterday, late_penalty_percent=20)
        # completed & approved but NOT yet paid -> shows as "unpaid"
        Task.objects.create(title='Write unit tests', assigned_to=amina,
                            status='completed', approved=True, pay_amount=Decimal('1500'))
        # overdue & INCOMPLETE with a 20% penalty -> an abandonment fine of 300
        e = Task.objects.create(title='Migrate the old database', assigned_to=amina,
                                status='in_progress', pay_amount=Decimal('1500'),
                                due_date=yesterday, late_penalty_percent=20, progress=40)
        # an in-progress task with a checklist (progress demo)
        d = Task.objects.create(title='Design the dashboard', assigned_to=amina,
                                status='in_progress', pay_amount=Decimal('1500'),
                                estimated_hours=Decimal('6'))
        for title, done in [('Sketch layout', True), ('Build cards', True),
                            ('Add charts', False), ('Review', False)]:
            SubTask.objects.create(task=d, title=title, is_done=done)
        d.save()  # recompute progress from the checklist (2/4 = 50%)

        # Pay Amina for the two on-time/late completed tasks, minus the abandonment fine.
        gross = a.net_pay + b.net_pay               # 1500 + 1200 = 2700
        fine = e.abandon_fine                       # 300
        p1 = Payment.objects.create(
            engineer=amina, basis='per_task', period='Demo — this month',
            amount=gross - fine, task_count=2, fine=fine, created_by=pm)
        Task.objects.filter(pk__in=[a.pk, b.pk]).update(payment=p1)
        Task.objects.filter(pk=e.pk).update(fine_settled=True)

        # ---- 2) Hourly engineer: paid for logged hours ----
        brian = User.objects.create_user(
            username='brian', password=PW, role='worker',
            first_name='Brian', pay_type='hourly', hourly_rate=Decimal('800'))
        f = Task.objects.create(title='Optimise the API queries', assigned_to=brian,
                                status='in_progress', estimated_hours=Decimal('10'))
        g = Task.objects.create(title='Fix the deployment script', assigned_to=brian,
                                status='completed', approved=True)
        TimeLog.objects.create(task=f, user=brian, hours=Decimal('3'), note='Profiling', work_date=today)
        TimeLog.objects.create(task=g, user=brian, hours=Decimal('4.5'), note='Rewrote pipeline', work_date=yesterday)
        # Pay those 7.5h at 800/h = 6000, then leave a fresh unpaid log for the demo.
        paid_logs = list(TimeLog.objects.filter(user=brian, payment__isnull=True))
        hours = sum((l.hours for l in paid_logs), Decimal('0'))
        p2 = Payment.objects.create(
            engineer=brian, basis='hourly', period='Demo — week 1',
            amount=hours * brian.hourly_rate, hours=hours, created_by=pm)
        TimeLog.objects.filter(pk__in=[l.pk for l in paid_logs]).update(payment=p2)
        TimeLog.objects.create(task=f, user=brian, hours=Decimal('2'), note='More profiling (unpaid)', work_date=today)

        # ---- 3) Monthly engineer: paid a salary ----
        carol = User.objects.create_user(
            username='carol', password=PW, role='worker',
            first_name='Carol', pay_type='monthly', monthly_salary=Decimal('45000'))
        Task.objects.create(title='Support & maintenance', assigned_to=carol, status='in_progress')
        Payment.objects.create(engineer=carol, basis='monthly', period='Demo — this month',
                               amount=carol.monthly_salary, created_by=pm)

        total_paid = Payment.objects.filter(
            engineer__in=[amina, brian, carol]).aggregate(s=Sum('amount'))['s']

        self.stdout.write(self.style.SUCCESS("Demo data created. Log in (password: demo1234):"))
        self.stdout.write("  amina — per-task engineer (paid KES 2,400 net; 1 task still unpaid; 1 fine)")
        self.stdout.write("  brian — hourly engineer (paid KES 6,000 for 7.5h; 2h unpaid)")
        self.stdout.write("  carol — monthly engineer (paid KES 45,000 salary)")
        self.stdout.write(self.style.SUCCESS(
            f"Total demo money paid out: KES {total_paid}. "
            f"Open Payments / a worker dashboard / the PM report to show it."))
