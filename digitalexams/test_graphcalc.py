"""The graphing calculator: a teacher can allow it per exam, and the pupil's page carries it."""
from django.test import TestCase
from django.utils import timezone

from .models import Assignment, Recipient, School, User


class GraphingCalculatorTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.pupil = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school)
        self.exam = Assignment.objects.create(school=self.school, title='Maths', teacher_name='T', teacher_email='t@x.edu',
                                              opens_at=timezone.now(), allow_graphing_calculator=True)
        Recipient.objects.create(assignment=self.exam, pupil_name='Pia', pupil_email='p@x.edu')

    def test_page_has_calculator_and_exam_says_it_is_allowed(self):
        self.client.force_login(self.pupil)
        page = self.client.get('/s/ssc/')
        self.assertContains(page, 'id="graphCalcModal"')
        self.assertContains(page, 'id="openGraphCalcBtn"')
        self.assertNotContains(page, 'not yet available to pupils')
        self.assertNotContains(page, 'Graphing calculator for exams')   # the template's own note must not show
        a = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]
        self.assertTrue(a['allowGraphingCalculator'])
        self.assertFalse(a['allowCalculator'])
