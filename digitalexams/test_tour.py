from django.test import TestCase
from .models import School, User

class TourDoneTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('tea', 'tea@ssc.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.client.force_login(self.t)

    def test_tour_and_logout_urls_have_school_prefix(self):
        r = self.client.get('/s/ssc/')
        self.assertContains(r, "fetch('/s/ssc/api/account/tour-done/'")
        self.assertContains(r, 'action="/s/ssc/logout/"')

    def test_done_is_saved_and_tour_stays_off(self):
        self.assertContains(self.client.get('/s/ssc/'), 'const AUTO_START = true')
        r = self.client.post('/s/ssc/api/account/tour-done/')
        self.assertEqual(r.status_code, 200)
        for page in ('/s/ssc/', '/s/ssc/classes/', '/s/ssc/activity/'):
            self.assertContains(self.client.get(page), 'const AUTO_START = false', msg_prefix=page)
        # and after signing in again
        self.client.logout(); self.client.force_login(self.t)
        self.assertContains(self.client.get('/s/ssc/'), 'const AUTO_START = false')

    def test_unprefixed_post_was_the_bug(self):
        r = self.client.post('/api/account/tour-done/')
        self.assertEqual(r.status_code, 302)  # redirect -> browser re-sends as GET -> 405

    def test_logout_works(self):
        r = self.client.post('/s/ssc/logout/')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)
