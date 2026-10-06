import json, re
from unittest import mock
from django.core import mail
from django.test import TestCase
from .models import School, User, EmailLoginCode

class EmailLoginTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.pupil = User.objects.create_user('ana', 'ana@ssc.edu', 'OldPass!2345', school=self.school)
        self.url = '/s/ssc/login/'

    def send(self, ident='ana', as_='pupil'):
        return self.client.post(self.url, {'action': 'send_code', 'identifier': ident, 'login_as': as_})

    def code(self):
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)

    def test_password_login_still_works(self):
        r = self.client.post(self.url, {'action': 'password', 'username': 'ana', 'password': 'OldPass!2345', 'login_as': 'pupil'})
        self.assertEqual(r.status_code, 302)

    def test_login_page_renders(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'Email me a code')

    def test_code_login_by_email_and_username(self):
        for ident in ('ANA@ssc.edu', 'ana'):
            self.client.logout(); EmailLoginCode.objects.all().delete()
            r = self.send(ident)
            self.assertContains(r, '6-digit code')
            r = self.client.post(self.url, {'action': 'verify_code', 'code': self.code(), 'login_as': 'pupil'})
            self.assertEqual(r.status_code, 302, ident)
            self.assertEqual(r['Location'], '/s/ssc/')

    def test_code_single_use_and_wrong_code(self):
        self.send()
        c = self.code()
        r = self.client.post(self.url, {'action': 'verify_code', 'code': '000000' if c != '000000' else '111111', 'login_as': 'pupil'})
        self.assertContains(r, 'incorrect or has expired')
        r = self.client.post(self.url, {'action': 'verify_code', 'code': c, 'login_as': 'pupil'})
        self.assertEqual(r.status_code, 302)
        self.client.logout()
        self.client.post(self.url, {'action': 'send_code', 'identifier': 'nobody', 'login_as': 'pupil'})
        r = self.client.post(self.url, {'action': 'verify_code', 'code': c, 'login_as': 'pupil'})
        self.assertContains(r, 'incorrect or has expired')

    def test_attempt_limit(self):
        self.send(); c = self.code()
        for _ in range(5):
            self.client.post(self.url, {'action': 'verify_code', 'code': '999999' if c != '999999' else '888888', 'login_as': 'pupil'})
        r = self.client.post(self.url, {'action': 'verify_code', 'code': c, 'login_as': 'pupil'})
        self.assertContains(r, 'incorrect or has expired')

    def test_unknown_account_same_reply_no_email(self):
        r = self.send('ghost')
        self.assertContains(r, 'If that account exists')
        self.assertEqual(len(mail.outbox), 0)

    def test_cooldown(self):
        self.send(); self.send()
        self.assertEqual(len(mail.outbox), 1)

    def test_role_mismatch(self):
        self.send(as_='teacher')
        r = self.client.post(self.url, {'action': 'verify_code', 'code': self.code(), 'login_as': 'teacher'})
        self.assertContains(r, 'This is a pupil account')

    def test_other_school_not_found(self):
        School.objects.create(name='SSB', slug='ssb')
        self.client.post('/s/ssb/login/', {'action': 'send_code', 'identifier': 'ana', 'login_as': 'pupil'})
        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure(self):
        with mock.patch('digitalexams.email_login.send_mail', side_effect=OSError('smtp down')):
            r = self.send()
        self.assertContains(r, "couldn&#x27;t send")

    def test_forgot_flow_change_password_without_current(self):
        self.client.post(self.url, {'action': 'send_code', 'identifier': 'ana', 'login_as': 'pupil', 'reset': '1'})
        r = self.client.post(self.url, {'action': 'verify_code', 'code': self.code(), 'login_as': 'pupil', 'reset': '1'})
        self.assertEqual(r['Location'], '/s/ssc/?set_password=1')
        home = self.client.get('/s/ssc/')
        self.assertContains(home, "You signed in with an email code")
        r = self.client.post('/s/ssc/api/account/change-password/', json.dumps({'newPassword': 'BrandNew!789xyz'}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.pupil.refresh_from_db(); self.assertTrue(self.pupil.check_password('BrandNew!789xyz'))
        # one use only: second change needs the current password again
        r = self.client.post('/s/ssc/api/account/change-password/', json.dumps({'newPassword': 'Another!789xyz'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_password_login_still_needs_current_password(self):
        self.client.force_login(self.pupil)
        r = self.client.post('/s/ssc/api/account/change-password/', json.dumps({'newPassword': 'BrandNew!789xyz'}), content_type='application/json')
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/s/ssc/api/account/change-password/', json.dumps({'currentPassword': 'OldPass!2345', 'newPassword': 'BrandNew!789xyz'}), content_type='application/json')
        self.assertEqual(r.status_code, 200)
