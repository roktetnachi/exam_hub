"""Typing addresses by hand: what a signed-out visitor, a pupil, or a user from another school can
reach under /media/, /s/<slug>/ and the exam APIs."""
import hashlib
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Assignment, AssignmentItem, ExamResource, Recipient, School, Submission, User
from .views import seb_config_key

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class UrlAccessTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.other = School.objects.create(name='Other', slug='other')
        self.teacher = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher')
        self.pupil = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school)
        self.exam = Assignment.objects.create(school=self.school, title='Term exam', teacher_name='T',
                                              teacher_email='t@x.edu', opens_at=timezone.now())
        self.pdf = AssignmentItem.objects.create(assignment=self.exam, item_type='pdf', label='Paper')
        self.pdf.pdf.save('Term_1_Exam.pdf', ContentFile(b'%PDF-1.4 secret paper'))
        AssignmentItem.objects.create(assignment=self.exam, item_type='long_answer', label='Q1', prompt='Explain osmosis.')
        ExamResource.objects.create(assignment=self.exam, kind='text', title='Formula sheet', text='E = mc^2')
        self.rec = Recipient.objects.create(assignment=self.exam, pupil_name='Pia', pupil_email='p@x.edu')

    def my_exam(self):
        return self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]

    # ---- /media/ ----
    def test_uploaded_exam_files_are_not_public_under_media(self):
        url = '/media/' + self.pdf.pdf.name
        self.assertEqual(self.client.get(url).status_code, 404)           # signed out
        self.client.force_login(self.pupil)
        self.assertEqual(self.client.get(url).status_code, 404)           # signed in: still only via the API
        self.assertEqual(self.client.get('/media/school_logos/../' + self.pdf.pdf.name).status_code, 404)

    def test_school_logo_is_still_public(self):
        self.school.logo.save('logo.png', ContentFile(b'\\x89PNG fake'))
        r = self.client.get('/media/' + self.school.logo.name)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'image/png')

    # ---- /s/<slug>/ ----
    def test_account_without_a_school_cannot_open_a_school(self):
        drifter = User.objects.create_user('d', 'd@x.edu', 'x', role='teacher')   # no school
        self.client.force_login(drifter)
        self.assertEqual(self.client.get('/s/ssc/api/assignments/').status_code, 403)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/').status_code, 403)

    def test_other_school_user_cannot_switch_slug(self):
        outsider = User.objects.create_user('o', 'o@x.edu', 'x', school=self.other, role='teacher', is_school_admin=True)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get('/s/ssc/').status_code, 403)
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/').status_code, 403)
        # Their own slug + another school's object id is refused too.
        self.assertEqual(self.client.get(f'/s/other/api/items/{self.pdf.id}/pdf/').status_code, 403)

    def test_signed_out_visitor_is_sent_to_login(self):
        r = self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])

    def test_pupil_cannot_reach_teacher_pages_or_other_pupils_work(self):
        other = User.objects.create_user('q', 'q@x.edu', 'x', school=self.school)
        theirs = Recipient.objects.create(assignment=self.exam, pupil_name='Quinn', pupil_email='q@x.edu')
        self.client.force_login(self.pupil)
        self.assertEqual(self.client.get('/s/ssc/roles/').status_code, 302)
        self.assertEqual(self.client.get('/s/ssc/api/pupils/').status_code, 403)
        self.assertEqual(self.client.get(f'/s/ssc/api/assignments/{self.exam.id}/status/').status_code, 403)
        self.assertEqual(self.client.get(f'/s/ssc/api/recipients/{theirs.id}/messages/').status_code, 403)
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{theirs.id}/save/', '{}',
                                          content_type='application/json').status_code, 403)
        self.assertEqual(self.client.get('/s/ssc/superadmin/').status_code, 403)
        self.assertNotIn('q@x.edu', str(self.client.get('/s/ssc/api/assignments/').json()))

    # ---- join code ----
    def test_join_code_paper_is_held_back_until_the_code_is_entered(self):
        self.exam.require_join_code, self.exam.join_code = True, 'ABC123'
        self.exam.save()
        self.client.force_login(self.pupil)
        a = self.my_exam()
        self.assertTrue(a['contentWithheld'])
        self.assertNotIn('osmosis', str(a))
        self.assertNotIn('mc^2', str(a))
        self.assertIsNone(a['joinCode'])
        r = self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/')
        self.assertEqual((r.status_code, r.json()['code']), (403, 'join_code_required'))
        self.client.post(f'/s/ssc/api/submissions/{self.rec.id}/start/', '{"joinCode": "abc123"}',
                         content_type='application/json')
        a = self.my_exam()
        self.assertFalse(a['contentWithheld'])
        self.assertIn('osmosis', str(a))
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/').status_code, 200)

    def test_teacher_still_sees_everything(self):
        self.exam.require_join_code, self.exam.join_code = True, 'ABC123'
        self.exam.delivery_mode = 'lockdown'
        self.exam.save()
        self.client.force_login(self.teacher)
        a = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]
        self.assertIn('osmosis', str(a))
        self.assertFalse(a['contentWithheld'])
        preview = self.client.get('/s/ssc/api/assignments/?pupil_email=p@x.edu').json()['assignments'][0]
        self.assertIn('osmosis', str(preview))

    # ---- lockdown ----
    def test_lockdown_paper_questions_only_inside_seb(self):
        self.exam.delivery_mode = 'lockdown'
        self.exam.save()
        self.client.force_login(self.pupil)
        a = self.my_exam()
        self.assertTrue(a['contentWithheld'])
        self.assertNotIn('osmosis', str(a))
        key = seb_config_key('testserver', 'http')
        url = 'http://testserver/s/ssc/api/assignments/'
        r = self.client.get('/s/ssc/api/assignments/',
                            HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=hashlib.sha256((url + key).encode()).hexdigest())
        a = r.json()['assignments'][0]
        self.assertFalse(a['contentWithheld'])
        self.assertIn('osmosis', str(a))

    def test_after_hand_in_pupil_can_read_their_paper_back(self):
        self.exam.delivery_mode = 'lockdown'
        self.exam.require_join_code, self.exam.join_code = True, 'ABC123'
        self.exam.save()
        Submission.objects.create(recipient=self.rec, status='submitted', submitted_at=timezone.now())
        self.client.force_login(self.pupil)
        self.assertIn('osmosis', str(self.my_exam()))
        self.assertEqual(self.client.get(f'/s/ssc/api/items/{self.pdf.id}/pdf/').status_code, 200)
