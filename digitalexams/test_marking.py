"""Teacher marking on pupils' work, and submission notifications (instead of email)."""
import json

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from .models import Assignment, AssignmentItem, Recipient, School, Subject, Submission, User


class MarkingAndSubmissionNoticeTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', first_name='Tess')
        self.other_t = User.objects.create_user('o', 'o@x.edu', 'x', school=self.school, role='teacher')
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        subj = Subject.objects.create(school=self.school, name='English')
        subj.teachers.add(self.t); subj.pupils.add(self.p)
        self.exam = Assignment.objects.create(school=self.school, title='Essay', teacher_name='Tess', teacher_email='t@x.edu')
        self.item = AssignmentItem.objects.create(assignment=self.exam, item_type='long_answer', label='Q1', prompt='Why?', points=10)
        self.rec = Recipient.objects.create(assignment=self.exam, pupil_name='Pia', pupil_email='p@x.edu')
        self.sub = Submission.objects.create(recipient=self.rec, status='draft', started_at=timezone.now(),
                                             responses={str(self.item.id): '<p>The cat sat on the mat.</p>'})

    def post(self, url, data=None):
        return self.client.post('/s/ssc' + url, json.dumps(data or {}), content_type='application/json')

    def marks(self):
        return {'answers': {str(self.item.id): [
                    {'id': 'a1', 'start': 4, 'end': 7, 'quote': 'cat', 'style': 'circle', 'comment': 'Which cat?'},
                    {'id': 'bad', 'start': 5, 'end': 2, 'quote': '', 'style': 'underline'},          # dropped
                    {'id': 'bad2', 'start': 0, 'end': 3, 'quote': 'The', 'style': 'scribble'}]},      # dropped
                'pdf': {'999': [{'id': 'p1', 'page': 1, 'shape': 'pin', 'x': 10, 'y': 20, 'w': 0, 'h': 0, 'comment': 'Nice'}]},
                'annComments': {'0': {'fp': 'x|1|highlight|1|2', 'comment': 'Good evidence'}, '1': {'fp': '', 'comment': '  '}}}

    def test_submit_sends_no_email_and_shows_as_new(self):
        self.client.force_login(self.p)
        r = self.post(f'/api/submissions/{self.rec.id}/submit/', {'responses': {str(self.item.id): '<p>The cat sat.</p>'}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(mail.outbox), 0)
        self.client.force_login(self.t)
        rec = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertTrue(rec['newSubmission'])
        items = self.client.get('/s/ssc/api/notifications/').json()['items']
        self.assertEqual([i['kind'] for i in items], ['submitted'])
        self.assertEqual(items[0]['pupilName'], 'Pia')
        self.assertTrue(self.client.get(f'/s/ssc/api/assignments/{self.exam.id}/status/').json()['recipients'][0]['newSubmission'])
        # Another teacher can't clear it; the owner can.
        self.client.force_login(self.other_t)
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/seen/').status_code, 403)
        self.client.force_login(self.t)
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/seen/').status_code, 200)
        self.assertEqual(self.client.get('/s/ssc/api/notifications/').json()['items'], [])
        self.assertFalse(self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]['newSubmission'])
        # Bulk "mark all as seen".
        Submission.objects.filter(id=self.sub.id).update(teacher_seen_at=None)
        self.assertEqual(self.post('/api/submissions/seen/', {'assignmentId': self.exam.id}).json()['seen'], 1)

    def test_teacher_marks_saved_cleaned_and_hidden_until_release(self):
        self.client.force_login(self.t)
        r = self.post(f'/api/submissions/{self.rec.id}/marks/', {'teacherMarks': self.marks()})
        self.assertEqual(r.status_code, 200, r.content)
        saved = Submission.objects.get(id=self.sub.id).teacher_marks
        self.assertEqual([m['id'] for m in saved['answers'][str(self.item.id)]], ['a1'])
        self.assertEqual(saved['pdf']['999'][0]['comment'], 'Nice')
        self.assertEqual(list(saved['annComments']), ['0'])
        # The pupil's own answer is untouched.
        self.assertEqual(Submission.objects.get(id=self.sub.id).responses[str(self.item.id)], '<p>The cat sat on the mat.</p>')
        # Pupil can't see the marks until the grade is released…
        self.client.force_login(self.p)
        rec = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertEqual(rec['teacherMarks'], {})
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/marks/', {'teacherMarks': {}}).status_code, 403)
        # …then they can.
        Submission.objects.filter(id=self.sub.id).update(published=True, status='submitted')
        rec = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['recipients'][0]
        self.assertEqual(rec['teacherMarks']['answers'][str(self.item.id)][0]['comment'], 'Which cat?')

    def test_only_the_exams_teacher_can_mark(self):
        self.client.force_login(self.other_t)
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/marks/', {'teacherMarks': self.marks()}).status_code, 403)
        self.client.force_login(self.t)
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/marks/', {'teacherMarks': 'nope'}).status_code, 400)
        huge = {'answers': {str(self.item.id): [{'id': 'x', 'start': 0, 'end': 1, 'quote': 'T', 'style': 'underline', 'comment': 'y' * 500_000}]}}
        self.assertEqual(self.post(f'/api/submissions/{self.rec.id}/marks/', {'teacherMarks': huge}).status_code, 400)

    def test_review_page_has_marking_tools(self):
        self.client.force_login(self.t)
        html = self.client.get('/s/ssc/').content.decode()
        for needle in ('id="tmTools"', 'function tmApply', 'Mark this answer', 'qnav-current', 'expandAllRes'):
            self.assertIn(needle, html)
