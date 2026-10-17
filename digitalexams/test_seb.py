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
        self.key = seb_config_key('testserver', 'http')   # Andam's own lockdown config, as downloaded here

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


class SebQuitCodeTests(TestCase):
    """Lockdown exams get a quit code: SEB won't close until someone types it (the teacher reads it
    from the Monitor tab), and only a SEB opened with that exam's own launch file is let in."""

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        self.exam, self.rec = self.make('Locked', '482913')

    def make(self, title, quit_code, mode='lockdown'):
        a = Assignment.objects.create(school=self.school, title=title, teacher_name='T', teacher_email='t@x.edu',
                                      delivery_mode=mode, opens_at=timezone.now(), seb_quit_code=quit_code)
        AssignmentItem.objects.create(assignment=a, item_type='long_answer', label='Q1', prompt='Why?')
        return a, Recipient.objects.create(assignment=a, pupil_name='Pia', pupil_email='p@x.edu')

    def launch(self, exam):
        """Open `exam` the way the Open button does; returns the launch file's Config Key and enter path."""
        self.client.force_login(self.p)
        seb_url = self.client.post('/s/ssc/api/seb/launch/', json.dumps({'assignmentId': exam.id}),
                                   content_type='application/json').json()['sebUrl']
        token = seb_url.split('/seb/launch/')[1].split('/')[0]
        self.client.logout()
        cfg = self.client.get(f'/seb/launch/{token}/config.seb').content.decode()
        enter = f'/seb/enter/{token}/'
        return cfg, enter, seb_config_key('testserver', 'http', enter, exam.seb_quit_code)

    def test_new_lockdown_exam_gets_a_quit_code_only_teachers_see(self):
        self.client.force_login(self.t)
        base = {'title': 'Mock', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
                'recipients': json.dumps([{'name': 'Pia', 'email': 'p@x.edu'}])}
        r = self.client.post('/s/ssc/api/assignments/create/', {**base, 'delivery_mode': 'lockdown'})
        self.assertEqual(r.status_code, 201, r.content)
        exam = Assignment.objects.get(id=r.json()['id'])
        self.assertRegex(exam.seb_quit_code, r'^\d{6}$')
        other = Assignment.objects.get(id=self.client.post('/s/ssc/api/assignments/create/', base).json()['id'])
        self.assertEqual(other.seb_quit_code, '')
        mine = {a['id']: a for a in self.client.get('/s/ssc/api/assignments/').json()['assignments']}
        self.assertEqual(mine[exam.id]['sebQuitCode'], exam.seb_quit_code)
        self.client.force_login(self.p)
        theirs = {a['id']: a for a in self.client.get('/s/ssc/api/assignments/').json()['assignments']}
        self.assertIsNone(theirs[exam.id]['sebQuitCode'])
        self.assertTrue(theirs[exam.id]['hasSebQuitCode'])
        self.assertNotIn(exam.seb_quit_code, json.dumps(theirs[exam.id]))

    def test_launch_file_carries_the_hashed_quit_code(self):
        cfg, _, _ = self.launch(self.exam)
        self.assertRegex(cfg, r'<key>hashedQuitPassword</key>\s*<string>%s</string>' % hashlib.sha256(b'482913').hexdigest())
        self.assertRegex(cfg, r'<key>quitURLConfirm</key>\s*<false/>')   # submitting still closes SEB
        self.client.force_login(self.t)
        shared = self.client.get('/s/ssc/api/seb/config/').content.decode()
        self.assertRegex(shared, r'<key>hashedQuitPassword</key>\s*<string></string>')

    def test_shared_config_is_not_enough_for_an_exam_with_a_quit_code(self):
        self.client.force_login(self.p)
        site_key = seb_config_key('testserver', 'http')
        start = f'/s/ssc/api/submissions/{self.rec.id}/start/'
        r = self.client.post(start, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + start, site_key))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['reason'], 'other_exam')
        check = f'/s/ssc/api/seb/check/?assignment={self.exam.id}'
        r = self.client.get(check, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + check, site_key))
        self.assertEqual(r.json(), {'ok': False, 'sebHeaders': True, 'otherExam': True})
        # The questions stay held back too.
        url = 'http://testserver/s/ssc/api/assignments/'
        mine = self.client.get('/s/ssc/api/assignments/', HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h(url, site_key)).json()
        self.assertTrue(next(a for a in mine['assignments'] if a['id'] == self.exam.id)['contentWithheld'])
        # Verifying through the JavaScript API with the shared config doesn't count either.
        page = 'http://testserver/s/ssc/'
        self.assertTrue(self.client.post('/s/ssc/api/seb/verify/', json.dumps({'urls': [page], 'configKey': h(page, site_key)}),
                                         content_type='application/json').json()['ok'])
        self.assertEqual(self.client.post(start).status_code, 403)

    def test_launch_with_headers_lets_the_pupil_in(self):
        _, enter, key = self.launch(self.exam)
        r = self.client.get(enter, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + enter, key))
        self.assertRedirects(r, f'/s/ssc/?open={self.exam.id}', fetch_redirect_response=False)
        start = f'/s/ssc/api/submissions/{self.rec.id}/start/'
        self.assertEqual(self.client.post(start, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + start, key)).status_code, 200)
        # A key built without the quit code (an old or hand-edited file) isn't this exam's.
        _, enter2, _ = self.launch(self.exam)
        wrong = seb_config_key('testserver', 'http', enter2)
        self.assertEqual(self.client.get(enter2, HTTP_X_SAFEEXAMBROWSER_CONFIGKEYHASH=h('http://testserver' + enter2, wrong)).status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)   # got the JS fallback page, not signed in

    def test_launch_with_javascript_api_only(self):
        _, enter, key = self.launch(self.exam)
        r = self.client.post(enter, json.dumps({'url': 'http://testserver' + enter, 'configKey': h('http://testserver' + enter, key)}),
                             content_type='application/json')
        self.assertEqual(r.json(), {'ok': True, 'redirect': f'/s/ssc/?open={self.exam.id}'})
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{self.rec.id}/start/').status_code, 200)
        # The shared config's key on the enter page is refused: it must be the launch file itself.
        _, enter2, _ = self.launch(self.exam)
        site = seb_config_key('testserver', 'http')
        r = self.client.post(enter2, json.dumps({'url': 'http://testserver' + enter2, 'configKey': h('http://testserver' + enter2, site)}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_seb_opened_for_one_exam_does_not_open_another(self):
        other, other_rec = self.make('Other', '111222')
        plain, plain_rec = self.make('Old lockdown', '')
        _, enter, key = self.launch(self.exam)
        self.client.post(enter, json.dumps({'url': 'http://testserver' + enter, 'configKey': h('http://testserver' + enter, key)}),
                         content_type='application/json')
        r = self.client.post(f'/s/ssc/api/submissions/{other_rec.id}/start/')
        self.assertEqual((r.status_code, r.json()['reason']), (403, 'other_exam'))
        self.assertEqual(self.client.get(f'/s/ssc/api/seb/check/?assignment={other.id}').json()['otherExam'], True)
        # A Lockdown exam without a quit code still opens in any Andam SEB, as before.
        self.assertEqual(self.client.post(f'/s/ssc/api/submissions/{plain_rec.id}/start/').status_code, 200)

    def test_editing_adds_a_code_only_before_anyone_starts_and_never_changes_one(self):
        self.client.force_login(self.t)
        post = lambda a, mode: self.client.post(f'/s/ssc/api/assignments/{a.id}/update/', json.dumps(
            {'title': a.title, 'deliveryMode': mode, 'dueAt': None}), content_type='application/json')
        a, _ = self.make('Was browser', '', mode='browser')
        self.assertEqual(post(a, 'lockdown').status_code, 200)
        a.refresh_from_db(); self.assertRegex(a.seb_quit_code, r'^\d{6}$')
        code = a.seb_quit_code
        post(a, 'lockdown'); a.refresh_from_db(); self.assertEqual(a.seb_quit_code, code)
        b, b_rec = self.make('Running', '')
        Submission.objects.create(recipient=b_rec, started_at=timezone.now(), status='draft')
        post(b, 'lockdown'); b.refresh_from_db(); self.assertEqual(b.seb_quit_code, '')

    def test_monitor_and_gate_markup(self):
        self.client.force_login(self.t)
        html = self.client.get('/s/ssc/').content.decode()
        for marker in ('id="monitorQuitCode"', 'function renderMonitorQuitCode', 'data-quit-toggle', 'id="sebGateHelp"',
                       'id="sebGateQuitRule"', 'function setSebGate', 'function closeSebAfterExam', 'id="sebLeaveBar"'):
            self.assertIn(marker, html)
