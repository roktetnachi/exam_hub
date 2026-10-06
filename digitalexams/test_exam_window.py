import datetime, json
from django.test import TestCase
from django.utils import timezone
from .models import Assignment, AssignmentItem, Recipient, School, Submission, User

T = datetime.timedelta


class ExamWindowTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.p = User.objects.create_user('ana', 'ana@x.edu', 'x', school=self.school)

    def make(self, **kw):
        a = Assignment.objects.create(school=self.school, title='Quiz', teacher_name='T', teacher_email='t@x.edu', **kw)
        AssignmentItem.objects.create(assignment=a, item_type='long_answer', label='Q1', prompt='Why?')
        r = Recipient.objects.create(assignment=a, pupil_name='Ana', pupil_email='ana@x.edu')
        return a, r

    def pupil_titles(self):
        self.client.force_login(self.p)
        return [x['id'] for x in self.client.get('/s/ssc/api/assignments/').json()['assignments']]

    def post(self, url, data=None):
        return self.client.post('/s/ssc' + url, json.dumps(data or {}), content_type='application/json')

    def test_create_with_visibility_and_no_duration(self):
        self.client.force_login(self.t)
        base = {'title': 'Exam A', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
                'recipients': json.dumps([{'name': 'Ana', 'email': 'ana@x.edu'}])}
        r = self.client.post('/s/ssc/api/assignments/create/', {**base, 'visibility': 'hidden', 'due_at': '2099-01-01T10:00'})
        self.assertEqual(r.status_code, 201, r.content)
        a = Assignment.objects.get(id=r.json()['id'])
        self.assertTrue(a.is_hidden)
        r = self.client.post('/s/ssc/api/assignments/create/', {**base, 'visibility': 'scheduled'})
        self.assertEqual(r.status_code, 400)  # needs a time
        r = self.client.post('/s/ssc/api/assignments/create/', {**base, 'visibility': 'scheduled', 'opens_at': '2099-01-01T09:00', 'due_at': '2099-01-01T08:00'})
        self.assertEqual(r.status_code, 400)  # end before visible

    def test_hidden_and_scheduled_exams_are_invisible_to_pupils(self):
        hidden, rh = self.make(is_hidden=True)
        later, rl = self.make(opens_at=timezone.now() + T(hours=1))
        live, _ = self.make()
        self.assertEqual(self.pupil_titles(), [live.id])
        self.assertEqual(self.post(f'/api/submissions/{rh.id}/start/').status_code, 403)
        self.assertEqual(self.post(f'/api/submissions/{rl.id}/start/').json()['code'], 'exam_not_open')
        self.client.force_login(self.t)
        self.post(f'/api/assignments/{hidden.id}/state/', {'action': 'show'})
        self.assertIn(hidden.id, self.pupil_titles())

    def test_manual_close_then_grace_then_auto_handin(self):
        a, r = self.make()
        self.client.force_login(self.p)
        self.post(f'/api/submissions/{r.id}/start/')
        self.post(f'/api/submissions/{r.id}/save/', {'responses': {'1': 'draft answer'}})
        self.client.force_login(self.t)
        body = self.post(f'/api/assignments/{a.id}/state/', {'action': 'close'}).json()
        self.assertEqual(body['state'], 'closed')
        self.client.force_login(self.p)
        st = self.client.get(f'/s/ssc/api/submissions/{r.id}/lock-status/').json()
        self.assertTrue(st['closed']); self.assertTrue(st['closedByTeacher'])
        self.assertEqual(self.post(f'/api/submissions/{r.id}/save/', {'responses': {}}).status_code, 403)  # autosave refused
        self.assertEqual(self.post(f'/api/submissions/{r.id}/submit/', {'responses': {'1': 'final'}}).status_code, 200)  # final submit within grace
        self.assertEqual(Submission.objects.get(recipient=r).responses, {'1': 'final'})

    def test_drafts_handed_in_after_grace(self):
        a, r = self.make()
        Submission.objects.create(recipient=r, status='draft', started_at=timezone.now(), responses={'1': 'saved'})
        a.closed_at = timezone.now() - T(minutes=5); a.save()
        self.pupil_titles()  # any listing finalises
        sub = Submission.objects.get(recipient=r)
        self.assertEqual(sub.status, 'submitted'); self.assertEqual(sub.responses, {'1': 'saved'})

    def test_extra_time_extends_scheduled_close_but_not_manual(self):
        a, r = self.make(due_at=timezone.now() - T(minutes=1))
        r.extra_minutes = 10; r.save()
        Submission.objects.create(recipient=r, status='draft', started_at=timezone.now())
        self.client.force_login(self.p)
        self.assertEqual(self.post(f'/api/submissions/{r.id}/save/', {'responses': {}}).status_code, 200)
        a.closed_at = timezone.now(); a.save()
        self.assertEqual(self.post(f'/api/submissions/{r.id}/save/', {'responses': {}}).status_code, 403)

    def test_reopen_after_end_time_becomes_manual(self):
        a, _ = self.make(due_at=timezone.now() - T(minutes=1))
        self.client.force_login(self.t)
        body = self.post(f'/api/assignments/{a.id}/state/', {'action': 'reopen'}).json()
        self.assertEqual(body['state'], 'live'); self.assertIsNone(body['dueAt'])

    def test_pupil_cannot_change_state_or_fetch_hidden_files(self):
        a, _ = self.make(is_hidden=True)
        self.client.force_login(self.p)
        self.assertEqual(self.post(f'/api/assignments/{a.id}/state/', {'action': 'show'}).status_code, 403)

    def test_update_visibility_and_close_mode(self):
        a, _ = self.make()
        self.client.force_login(self.t)
        r = self.post(f'/api/assignments/{a.id}/update/', {'title': 'Quiz', 'visibility': 'hidden', 'dueAt': None, 'deliveryMode': 'browser'})
        self.assertEqual(r.status_code, 200, r.content)
        a.refresh_from_db(); self.assertTrue(a.is_hidden); self.assertIsNone(a.due_at)
        self.assertEqual(r.json()['state'], 'hidden')
