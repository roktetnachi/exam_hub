from django.test import TestCase
from .models import School, SchoolClass, User

class SchoolScopingTests(TestCase):
    def setUp(self):
        self.demo = School.objects.create(name='Demo', slug='demo')
        self.new = School.objects.create(name='New', slug='new')
        self.p_demo = User.objects.create_user('pd', 'pd@x.edu', 'x', school=self.demo)
        self.t_demo = User.objects.create_user('td', 'td@x.edu', 'x', school=self.demo, role='teacher')
        c = SchoolClass.objects.create(name='Demo 7A', school=self.demo); c.pupils.add(self.p_demo); c.teachers.add(self.t_demo)
        self.p_new = User.objects.create_user('pn', 'pn@x.edu', 'x', school=self.new)
        SchoolClass.objects.create(name='New 7A', school=self.new).pupils.add(self.p_new)
        self.root = User.objects.create_superuser('root', 'root@x.edu', 'x')  # platform admin, no school

    def test_superuser_in_new_school_sees_only_that_school(self):
        self.client.force_login(self.root)
        pupils = self.client.get('/s/new/api/pupils/').json()['pupils']
        self.assertEqual([p['email'] for p in pupils], ['pn@x.edu'])
        data = self.client.get('/s/new/api/classes/').json()
        self.assertEqual([c['name'] for c in data['classes']], ['New 7A'])
        self.assertNotIn('td@x.edu', [t['email'] for t in data['teachers']])

    def test_school_admin_scoped(self):
        admin = User.objects.create_user('an', 'an@x.edu', 'x', school=self.new, role='teacher', is_school_admin=True)
        self.client.force_login(admin)
        self.assertEqual([c['name'] for c in self.client.get('/s/new/api/classes/').json()['classes']], ['New 7A'])

    def test_activity_hidden_for_plain_teacher(self):
        self.client.force_login(self.t_demo)
        home = self.client.get('/s/demo/')
        self.assertNotContains(home, '/s/demo/activity/')
        self.assertNotContains(self.client.get('/s/demo/subjects/'), 'View activity log')
        self.assertEqual(self.client.get('/s/demo/activity/').status_code, 302)
        self.assertEqual(self.client.get('/s/demo/api/activity-log/').status_code, 403)
        self.assertEqual([c['name'] for c in self.client.get('/s/demo/api/classes/').json()['classes']], ['Demo 7A'])

    def test_activity_visible_for_admin(self):
        admin = User.objects.create_user('ad', 'ad@x.edu', 'x', school=self.demo, role='teacher', is_school_admin=True)
        self.client.force_login(admin)
        self.assertContains(self.client.get('/s/demo/'), 'href="/s/demo/activity/"')
        self.assertEqual(self.client.get('/s/demo/activity/').status_code, 200)
        self.assertEqual(self.client.get('/s/demo/api/activity-log/').status_code, 200)
