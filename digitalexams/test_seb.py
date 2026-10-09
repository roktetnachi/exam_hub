"""Safe Exam Browser lockdown: the Browser Exam Key / Config Key hash checks behind /s/<school>/
URLs, the one-time launch link, and SEB's JavaScript API for versions that don't send headers."""
import hashlib
import json

from django.test import TestCase
from django.utils import timezone

from .models import Assignment, AssignmentItem, Recipient, School, Subject, Submission, User
from .views import make_seb_launch_token, seb_config_key


def h(url, key):
    return hashlib.sha256((url + key).encode()).hexdigest()


class SebLockdownTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher')
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        subj = Subject.objects.create(school=self.school, name='Maths')
        subj.teachers.add(self.t); subj.pupils.add(self.p)
        self.exam = Assignment.objects.create(school=self.school, title='Locked', teacher_name='T', teacher_email='t@x.edu',
                                              delivery_mode='lockdown', opens_at=timezone.now())
        AssignmentItem.objects.create(assignment=self.exam, item_type='long_answer', label='Q1', prompt='Why?')
        self.rec = Recipient.objects.create(assignment=self.exam, pupil_name='Pia', pupil_email='p@x.edu')
        self.key = seb_config_key('testserver', 'http')   # ExamHub's own lockdown config, as downloaded here

    def test_header_hash_is_checked_against_the_full_school_url(self):
        """The bug: the school middleware strips /s/ssc from the path, so a hash SEB built from the
        real URL never matched and pupils were sent back to the "Open in SEB" dialog."""
        self.client.force_login(self.p)
        url = 'http://testserver/s/ssc/api/seb/check/'
        r = self.client.get('/s/ssc/api/seb/check/', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h(url, self.key))
        self.assertEqual(r.json(), {'ok': True, 'sebHeaders': True})
        # A hash of the wrong (slug-less) URL is not accepted, and no headers = not SEB.
        r = self.client.get('/s/ssc/api/seb/check/', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver/api/seb/check/', self.key))
        self.assertFalse(r.json()['ok'])
        self.assertEqual(self.client.get('/s/ssc/api/seb/check/').json(), {'ok': False, 'sebHeaders': False})
        # Same rule on the real enforcement point: starting the exam.
        start = f'/s/ssc/api/submissions/{self.rec.id}/start/'
        self.assertEqual(self.client.post(start).status_code, 403)
        r = self.client.post(start, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + start, self.key))
        self.assertEqual(r.status_code, 200, r.content)
        # Query strings are part of the hashed URL too.
        r = self.client.get('/s/ssc/api/seb/check/?x=1', HTTP_X_SAFEEXAMBROWSER_REQUESTHASH=h(url + '?x=1', self.key))
        self.assertTrue(r.json()['ok'])

    def test_javascript_api_verification_for_newer_seb(self):
        self.client.force_login(self.p)
        page = 'http://testserver/s/ssc/?open=%d' % self.exam.id
        bad = self.client.post('/s/ssc/api/seb/verify/', json.dumps({'urls': [page], 'configKey': h(page, 'nope')}),
                               content_type='application/json')
        self.assertFalse(bad.json()['ok'])
        foreign = 'http://evil.example/s/ssc/'
        r = self.client.post('/s/ssc/api/seb/verify/', json.dumps({'urls': [foreign], 'configKey': h(foreign, self.key)}),
                             content_type='application/json')
        self.assertFalse(r.json()['ok'])
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{self.rec.id}/start/').status_code, 403)
        # Current URL (after ?open was tidied away) doesn't match, the first-loaded one does.
        r = self.client.post('/s/ssc/api/seb/verify/', json.dumps({'urls': ['http://testserver/s/ssc/', page],
                                                                    'configKey': h(page, self.key), 'browserExamKey': ''}),
                             content_type='application/json')
        self.assertTrue(r.json()['ok'])
        # This SEB session is now verified for background requests without headers.
        self.assertTrue(self.client.get('/s/ssc/api/seb/check/').json()['ok'])
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{self.rec.id}/start/').status_code, 200)

    def test_launch_link_with_headers(self):
        self.client.force_login(self.p)
        seb_url = self.client.post('/s/ssc/api/seb/launch/', json.dumps({'assignmentId': self.exam.id}),
                                   content_type='application/json').json()['sebUrl']
        self.assertTrue(seb_url.startswith('seb://testserver/seb/launch/'))
        token = seb_url.split('/seb/launch/')[1].split('/')[0]
        self.client.logout()   # SEB has its own cookies
        cfg = self.client.get(f'/seb/launch/{token}/config.seb')
        self.assertEqual(cfg.status_code, 200)
        self.assertIn(b'/seb/enter/', cfg.content)
        enter = f'/seb/enter/{token}/'
        launch_key = seb_config_key('testserver', 'http', enter)
        r = self.client.get(enter, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + enter, launch_key))
        self.assertRedirects(r, f'/s/ssc/?open={self.exam.id}', fetch_redirect_response=False)
        # Inside SEB on the school page, background requests carry headers for the school URL.
        url = 'http://testserver/s/ssc/api/seb/check/'
        self.assertTrue(self.client.get('/s/ssc/api/seb/check/', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h(url, launch_key)).json()['ok'])

    def test_launch_link_with_javascript_api_only(self):
        token = make_seb_launch_token(self.p, self.exam.id)
        enter = f'/seb/enter/{token}/'
        page = self.client.get(enter)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'SafeExamBrowser')
        launch_key = seb_config_key('testserver', 'http', enter)
        r = self.client.post(enter, json.dumps({'url': 'http://testserver' + enter, 'configKey': 'wrong'}), content_type='application/json')
        self.assertEqual(r.status_code, 403)
        r = self.client.post(enter, json.dumps({'url': 'http://testserver' + enter, 'configKey': h('http://testserver' + enter, launch_key)}),
                             content_type='application/json')
        self.assertEqual(r.json(), {'ok': True, 'redirect': f'/s/ssc/?open={self.exam.id}'})
        # Signed in as the pupil, verified for the exam's background requests.
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{self.rec.id}/start/').status_code, 200)
        self.assertTrue(Submission.objects.get(recipient=self.rec).started_at)

    def test_quit_address_and_exam_focus_markup(self):
        # The launch config tells SEB to quit, without asking, when it reaches /?sebquit=1.
        token = make_seb_launch_token(self.p, self.exam.id)
        cfg = self.client.get(f'/seb/launch/{token}/config.seb').content.decode()
        self.assertIn('<string>http://testserver/?sebquit=1</string>', cfg)
        self.assertRegex(cfg, r'<key>quitURLConfirm</key>\s*<false/>')
        # Outside SEB the quit address still lands on the "you can close SEB" page.
        self.client.force_login(self.p)
        self.assertRedirects(self.client.get('/?sebquit=1'), '/s/ssc/?sebquit=1', fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/'), '/s/ssc/', fetch_redirect_response=False)
        html = self.client.get('/s/ssc/').content.decode()
        for marker in ('body.exam-focus #backFromTask', 'id="examFocusNote"', 'function setExamFocus', "location.href = '/?sebquit=1'"):
            self.assertIn(marker, html)


class CalculatorAndGradingMarkupTests(TestCase):
    def test_scientific_calculator_and_grade_tab_marking_markup(self):
        school = School.objects.create(name='SSC', slug='ssc')
        t = User.objects.create_user('t', 't@x.edu', 'x', school=school, role='teacher')
        self.client.force_login(t)
        html = self.client.get('/s/ssc/').content.decode()
        for marker in ('const SciCalc', 'data-calc="sin" data-shift="asin"', 'data-calc="angle"', 'data-calc="fact"', 'data-calc="EE"',
                       'function setupWorkspace', 'function openGrading', "'Mark & grade this pupil'", 'class="rv-grade"'):
            self.assertIn(marker, html)
        self.assertNotIn('eval(', html.split('const SciCalc')[1].split('SciCalc.wire()')[0])

    def test_intro_toolbar_navigator_and_builder_markup(self):
        school = School.objects.create(name='SSD', slug='ssd')
        t = User.objects.create_user('t2', 't2@x.edu', 'x', school=school, role='teacher')
        self.client.force_login(t)
        html = self.client.get('/s/ssd/').content.decode()
        for marker in ('id="examIntroBackdrop"', 'function showExamIntro', 'function restoreEditorSelection', 'span[style*="xxx-large"]',
                       'id="answerWordCount"', 'function renumberBuilderItems', 'function moveBuilderItem', 'class="item-drag" draggable="true"',
                       'function refreshPupilViewAfterRelease'):
            self.assertIn(marker, html)
        self.assertNotIn("addItemRow('pdf', 'Text 1'", html)
        self.assertNotIn('of ${items.length}</span>', html)
