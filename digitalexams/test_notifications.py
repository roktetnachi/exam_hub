import datetime, json, shutil, tempfile
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import Assignment, AssignmentItem, Message, Recipient, School, Submission, User

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class NotificationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass(); shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.teacher = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', first_name='Tess')
        self.other = User.objects.create_user('o', 'o@x.edu', 'x', school=self.school, role='teacher')
        self.admin = User.objects.create_user('ad', 'ad@x.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.pupil = User.objects.create_user('ana', 'ana@x.edu', 'x', school=self.school, first_name='Ana')
        self.a = Assignment.objects.create(school=self.school, title='Essay', teacher_name='Tess', teacher_email='t@x.edu',
                                           delivery_mode='lockdown')
        self.item = AssignmentItem.objects.create(assignment=self.a, item_type='pdf', label='Text 1')
        self.item.pdf.save('t.pdf', ContentFile(b'%PDF-1.4 fake'))
        self.r = Recipient.objects.create(assignment=self.a, pupil_name='Ana', pupil_email='ana@x.edu')
        self.sub = Submission.objects.create(recipient=self.r)

    def notes(self, user):
        self.client.force_login(user)
        return self.client.get('/s/ssc/api/notifications/').json()['items']

    def test_lock_shows_for_owner_and_admin_not_other_teacher(self):
        self.sub.locked_at = timezone.now(); self.sub.lock_reason = 'You left the exam window'
        self.sub.resume_code = 'maple'; self.sub.lock_count = 2; self.sub.save()
        items = self.notes(self.teacher)
        self.assertEqual([(i['kind'], i['key'], i['resumeCode']) for i in items], [('lock', f'lock:{self.r.id}:2', 'maple')])
        self.assertEqual(len(self.notes(self.admin)), 1)
        self.assertEqual(self.notes(self.other), [])
        self.assertEqual(self.notes(self.pupil), [])  # pupils never see lock alerts / codes

    def test_unlock_clears_lock(self):
        self.sub.locked_at = timezone.now(); self.sub.save()
        self.client.force_login(self.teacher)
        self.client.post(f'/s/ssc/api/submissions/{self.r.id}/unlock/')
        self.assertEqual(self.notes(self.teacher), [])

    def test_pupil_message_until_teacher_reads(self):
        Message.objects.create(recipient=self.r, sender_role='pupil', sender_name='Ana', text='Can I have more time?')
        items = self.notes(self.teacher)
        self.assertEqual(items[0]['kind'], 'message'); self.assertEqual(items[0]['text'], 'Can I have more time?')
        self.client.get(f'/s/ssc/api/recipients/{self.r.id}/messages/')  # teacher opens the thread
        self.assertEqual(self.notes(self.teacher), [])
        Message.objects.create(recipient=self.r, sender_role='pupil', sender_name='Ana', text='Thanks')
        self.assertEqual(self.notes(self.teacher)[0]['text'], 'Thanks')

    def test_pupil_graded_notification_and_seen(self):
        self.sub.status = 'submitted'; self.sub.graded_at = timezone.now(); self.sub.total_score = 8; self.sub.max_score = 10
        self.sub.save()
        self.assertEqual(self.notes(self.pupil), [])  # graded but not released
        self.sub.published = True; self.sub.save()
        items = self.notes(self.pupil)
        self.assertEqual((items[0]['kind'], items[0]['totalScore']), ('graded', 8))
        self.client.post(f'/s/ssc/api/submissions/{self.r.id}/grade-seen/')
        self.assertEqual(self.notes(self.pupil), [])
        self.sub.graded_at = timezone.now() + datetime.timedelta(seconds=5); self.sub.save()  # re-graded
        self.assertEqual(len(self.notes(self.pupil)), 1)

    def test_other_pupil_cannot_mark_seen(self):
        User.objects.create_user('ben', 'ben@x.edu', 'x', school=self.school)
        self.client.force_login(User.objects.get(username='ben'))
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{self.r.id}/grade-seen/').status_code, 403)

    def test_lockdown_pdf_viewable_in_normal_browser_after_submit(self):
        self.client.force_login(self.pupil)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.item.id}/pdf/').status_code, 403)  # needs SEB while live
        self.sub.status = 'submitted'; self.sub.save()
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.item.id}/pdf/').status_code, 200)

    def test_bell_on_every_teacher_page(self):
        self.client.force_login(self.admin)
        for page in ('/s/ssc/', '/s/ssc/classes/', '/s/ssc/activity/', '/s/ssc/roles/'):
            self.assertContains(self.client.get(page), "api('/api/notifications/')", msg_prefix=page)
