"""Public landing page at the site root."""
from django.test import TestCase, override_settings

from .models import School, User


class LandingPageTests(TestCase):
    def setUp(self):
        self.ssc = School.objects.create(name='Singapore School Cebu', slug='ssc')
        self.closed = School.objects.create(name='Closed Academy', slug='closed', is_active=False)

    def test_signed_out_visitors_get_the_landing_page(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('aria-label="Exams on screen that still feel like paper."', html)
        self.assertIn('href="/s/ssc/login/"', html)              # active school listed
        self.assertNotIn('Closed Academy', html)                  # inactive one isn't
        self.assertIn('/static/digitalexams/landing/inter-latin-opsz-normal.woff2', html)
        self.assertIn('/static/digitalexams/landing/lenis.min.js', html)        # smooth scroll served locally
        self.assertIn('/static/digitalexams/landing/shots/pupil-exam.webp', html)
        self.assertNotIn('fonts.googleapis', html)                # nothing loaded from outside
        self.assertNotIn('cdn.', html)
        self.assertNotIn('(?<', html)                             # no regex lookbehind (old iPad Safari)

    @override_settings(CONTACT_EMAIL='hello@example.edu')
    def test_contact_button_uses_the_contact_address(self):
        self.assertIn('mailto:hello@example.edu', self.client.get('/').content.decode())

    @override_settings(CONTACT_EMAIL='')
    def test_no_contact_address_no_email_button(self):
        self.assertNotIn('mailto:', self.client.get('/').content.decode())

    def test_signed_in_people_still_go_straight_to_their_school(self):
        p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.ssc)
        self.client.force_login(p)
        self.assertRedirects(self.client.get('/'), '/s/ssc/', fetch_redirect_response=False)

    def test_school_sign_in_page_still_works(self):
        self.assertEqual(self.client.get('/s/ssc/login/').status_code, 200)

    def test_landing_assets_exist(self):
        from django.contrib.staticfiles import finders
        for name in ['inter-latin-opsz-normal.woff2', 'outfit-latin-wght-normal.woff2', 'lenis.min.js',
                     'shots/pupil-exam.webp', 'shots/notepad.webp', 'shots/resources.webp', 'shots/calculator.webp',
                     'shots/accessibility.webp', 'shots/monitor.webp', 'shots/marking.webp',
                     'shots/export-summary.webp', 'shots/export-paper.webp',
                     'shots/step-build.webp', 'shots/step-deliver.webp', 'shots/step-monitor.webp', 'shots/step-mark.webp']:
            self.assertIsNotNone(finders.find('digitalexams/landing/' + name), name)
        for name in ['examnook-icon.svg', 'examnook-mark.svg', 'examnook-mark-on-blue.svg', 'apple-touch-icon.png']:
            self.assertIsNotNone(finders.find('digitalexams/brand/' + name), name)

    def test_page_is_branded_examnook(self):
        html = self.client.get('/').content.decode()
        self.assertIn('<title>ExamNook', html)
        self.assertNotIn('ExamHub', html)
        self.assertIn('/static/digitalexams/brand/examnook-icon.svg', html)   # favicon
        for shot in ('notepad', 'resources', 'calculator'):
            self.assertIn(f'/static/digitalexams/landing/shots/{shot}.webp', html)

    def test_sign_in_page_has_the_examnook_favicon(self):
        html = self.client.get('/s/ssc/login/').content.decode()
        self.assertIn('/static/digitalexams/brand/examnook-icon.svg', html)
