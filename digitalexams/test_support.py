import json

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings

from .models import School, SupportRequest, User


@override_settings(SUPPORT_EMAIL='digitalexamhub@gmail.com', DEFAULT_FROM_EMAIL='Andam <digitalexamhub@gmail.com>',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SupportChatTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.teacher = User.objects.create_user('tea', 'tea@ssc.edu', 'x', school=self.school, role='teacher',
                                                first_name='Tess', last_name='Cher', tour_completed_at='2026-01-01T00:00Z')
        self.pupil = User.objects.create_user('pup', 'pup@ssc.edu', 'x', school=self.school, role='pupil',
                                              tour_completed_at='2026-01-01T00:00Z')

    def post(self, **data):
        return self.client.post('/support/message/', json.dumps(data), content_type='application/json')

    def test_signed_out_message_is_emailed_and_saved(self):
        r = self.post(name='Ana Reyes', email='ana@school.ph', role='prospective school', topic='school_enquiry',
                      message='We have 600 pupils and want to start in January.', page='/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['emailed'])
        self.assertRegex(body['reference'], r'^AND-\d{5}$')
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ['digitalexamhub@gmail.com'])
        self.assertEqual(m.reply_to, ['ana@school.ph'])
        self.assertIn('[Andam support]', m.subject)
        self.assertIn('Bringing Andam to a school', m.subject)
        self.assertIn('600 pupils', m.body)
        req = SupportRequest.objects.get()
        self.assertEqual(req.topic, 'school_enquiry')
        self.assertIsNone(req.user)
        self.assertTrue(req.email_sent)

    def test_signed_in_fills_in_name_email_school(self):
        self.client.force_login(self.teacher)
        r = self.post(topic='exam', message='Pupils cannot see the calculator.', page='/s/ssc/')
        self.assertEqual(r.status_code, 200, r.content)
        req = SupportRequest.objects.get()
        self.assertEqual((req.name, req.email, req.user, req.school, req.role), ('Tess Cher', 'tea@ssc.edu', self.teacher, self.school, 'teacher'))
        self.assertIn('(SSC)', mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].reply_to, ['tea@ssc.edu'])

    def test_validation(self):
        r = self.post(name='', email='nope', message='hi')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()['fields']), {'name', 'email', 'message'})
        self.assertEqual(SupportRequest.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_unknown_topic_becomes_other_and_subject_has_no_newlines(self):
        self.post(name='Bad\r\nBcc: x@y.z', email='a@b.co', topic='hack', message='hello there')
        req = SupportRequest.objects.get()
        self.assertEqual(req.topic, 'other')
        self.assertNotIn('\n', mail.outbox[0].subject)

    def test_honeypot_pretends_success_but_sends_nothing(self):
        r = self.post(name='Bot', email='bot@spam.io', message='Buy things now', website='http://spam')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SupportRequest.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(SUPPORT_MESSAGES_PER_HOUR=2)
    def test_rate_limited(self):
        for _ in range(2):
            self.assertEqual(self.post(name='A', email='a@b.co', message='hello there').status_code, 200)
        r = self.post(name='A', email='a@b.co', message='hello there')
        self.assertEqual(r.status_code, 429)
        self.assertEqual(SupportRequest.objects.count(), 2)

    @override_settings(EMAIL_BACKEND='digitalexams.test_support.BrokenBackend')
    def test_email_failure_still_saves(self):
        r = self.post(name='A', email='a@b.co', message='hello there')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['emailed'])
        self.assertFalse(SupportRequest.objects.get().email_sent)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get('/support/message/').status_code, 405)

    def test_csrf_enforced(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        r = c.post('/support/message/', json.dumps({'name': 'A', 'email': 'a@b.co', 'message': 'hello there'}),
                   content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_widget_on_pages(self):
        for url in ('/', '/login/', '/guide/'):
            r = self.client.get(url)
            self.assertContains(r, 'id="scRoot"', msg_prefix=url)
            self.assertContains(r, "sendUrl: \"/support/message/\"", msg_prefix=url)
        self.client.force_login(self.teacher)
        for url in ('/s/ssc/', '/s/ssc/subjects/'):
            r = self.client.get(url)
            self.assertContains(r, 'id="scRoot"', msg_prefix=url)
            self.assertContains(r, 'href="/guide/"', msg_prefix=url)
        # hidden while a pupil is sitting an exam
        self.assertContains(self.client.get('/s/ssc/'), 'body.exam-focus .sc-root{display:none !important;}')


class StarterGuideTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')

    def test_public_and_has_all_roles(self):
        r = self.client.get('/guide/')
        self.assertEqual(r.status_code, 200)
        for panel in ('id="admin"', 'id="teacher"', 'id="pupil"', 'id="schools"', 'id="faq"'):
            self.assertContains(r, panel)
        self.assertContains(r, 'var DEFAULT_ROLE = "admin"')
        self.assertContains(r, 'href="/login/"')

    def test_signed_in_gets_own_role_and_back_link(self):
        pupil = User.objects.create_user('pup', 'pup@ssc.edu', 'x', school=self.school, role='pupil')
        self.client.force_login(pupil)
        r = self.client.get('/guide/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'var DEFAULT_ROLE = "pupil"')
        self.assertContains(r, 'href="/s/ssc/"')
        teacher = User.objects.create_user('tea', 'tea@ssc.edu', 'x', school=self.school, role='teacher')
        self.client.force_login(teacher)
        self.assertContains(self.client.get('/guide/'), 'var DEFAULT_ROLE = "teacher"')
        admin = User.objects.create_user('adm', 'adm@ssc.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.client.force_login(admin)
        self.assertContains(self.client.get('/guide/'), 'var DEFAULT_ROLE = "admin"')
        self.assertContains(self.client.get('/guide/?role=pupil'), 'var DEFAULT_ROLE = "pupil"')

    def test_school_prefixed_address_works_too(self):
        teacher = User.objects.create_user('tea', 'tea@ssc.edu', 'x', school=self.school, role='teacher')
        self.client.force_login(teacher)
        self.assertEqual(self.client.get('/s/ssc/guide/').status_code, 200)

    def test_landing_links_to_guide(self):
        r = self.client.get('/')
        self.assertContains(r, 'href="/guide/"')
        self.assertContains(r, 'data-open-support="school_enquiry"')


class BrokenBackend:
    def __init__(self, *a, **k):
        pass

    def send_messages(self, messages):
        raise OSError('SMTP down')
