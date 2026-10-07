import json, re
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from .models import School, SchoolClass, User


class InviteAndFirstLoginTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.admin = User.objects.create_user('ad', 'ad@x.edu', 'x', school=self.school, role='teacher', is_school_admin=True)
        self.teacher = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher')
        self.c7 = SchoolClass.objects.create(name='Grade 7 - Newton', school=self.school)
        self.c8 = SchoolClass.objects.create(name='Grade 8 - Curie', school=self.school)

    def post(self, url, data):
        return self.client.post('/s/ssc' + url, json.dumps(data), content_type='application/json')

    def test_add_pupil_with_invite_then_forced_password_then_tour(self):
        self.client.force_login(self.admin)
        r = self.post('/api/pupils/create/', {'firstName': 'Ana', 'email': 'ana@x.edu', 'invite': True})
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertTrue(data['emailed']); self.assertEqual(data['status'], 'invited'); self.assertEqual(data['classes'], [])
        body = mail.outbox[-1].body
        temp = re.search(r'Temporary password: (\S+)', body).group(1)
        self.assertEqual(temp, data['tempPassword'])
        self.assertIn('/s/ssc/login/?as=pupil', body)
        # first sign-in with the temporary password
        self.client.logout()
        r = self.client.post('/s/ssc/login/', {'action': 'password', 'username': data['username'], 'password': temp, 'login_as': 'pupil'})
        self.assertEqual(r.status_code, 302)
        r = self.client.get('/s/ssc/')
        self.assertRedirects(r, '/s/ssc/welcome/password/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/s/ssc/api/assignments/').status_code, 403)
        r = self.client.post('/s/ssc/welcome/password/', {'password1': temp, 'password2': temp})
        self.assertContains(r, 'not the temporary one')
        r = self.client.post('/s/ssc/welcome/password/', {'password1': 'Sunflower!2026x', 'password2': 'Sunflower!2026x'})
        self.assertRedirects(r, '/s/ssc/', fetch_redirect_response=False)
        home = self.client.get('/s/ssc/')
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, 'const AUTO_START = true')  # tour only starts now
        u = User.objects.get(email='ana@x.edu')
        self.assertFalse(u.must_change_password); self.assertEqual(u.account_status, 'active')

    def test_add_teacher_with_classes(self):
        self.client.force_login(self.admin)
        r = self.post('/api/teachers/create/', {'firstName': 'Ben', 'email': 'ben@x.edu', 'role': 'teacher', 'classIds': [self.c7.id]})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['classes'], ['Grade 7 - Newton'])
        self.assertTrue(User.objects.get(email='ben@x.edu').must_change_password)
        self.assertEqual(len(mail.outbox), 0)  # no invite unless asked

    def test_bulk_invite_not_yet_invited_and_permissions(self):
        p1 = User.objects.create_user('p1', 'p1@x.edu', 'x', school=self.school, must_change_password=True)
        p2 = User.objects.create_user('p2', 'p2@x.edu', 'x', school=self.school, must_change_password=True)
        self.client.force_login(self.teacher)
        self.assertEqual(self.post('/api/accounts/invite/', {'userIds': [p1.id]}).status_code, 403)
        self.client.force_login(self.admin)
        r = self.post('/api/accounts/invite/', {'all': 'pupils', 'onlyNotInvited': True}).json()
        self.assertEqual(r['sent'], 2); self.assertEqual(len(r['credentials']), 2)
        r = self.post('/api/accounts/invite/', {'all': 'pupils', 'onlyNotInvited': True}).json()
        self.assertEqual(r['sent'], 0)  # already invited
        pupils = self.client.get('/s/ssc/api/accounts/pupils/').json()['pupils']
        self.assertEqual({p['status'] for p in pupils}, {'invited'})

    def test_bulk_teachers_import_with_roles_and_classes(self):
        self.client.force_login(self.admin)
        csv = ('First name,Last name,Email,Role,Classes\n'
               'Carla,Lim,carla@x.edu,Admin,Grade 7 - Newton; Grade 8 - Curie\n'
               'Dan,Ong,dan@x.edu,,\n'
               ',,t@x.edu,Admin,Grade 8 - Curie\n'
               'Eve,Tan,eve@x.edu,Teacher,Grade 9 - Nowhere\n')
        up = lambda: SimpleUploadedFile('t.csv', csv.encode())
        prev = self.client.post('/s/ssc/api/bulk/teachers/import/', {'file': up(), 'commit': '0'}).json()
        self.assertEqual(prev['summary'], {'create': 2, 'update': 1, 'unchanged': 0, 'error': 1})
        res = self.client.post('/s/ssc/api/bulk/teachers/import/', {'file': up(), 'commit': '1', 'skipErrors': '1', 'invite': '1'}).json()
        self.assertTrue(res['committed']); self.assertEqual(res['invited']['sent'], 2)
        carla = User.objects.get(email='carla@x.edu')
        self.assertTrue(carla.is_school_admin); self.assertEqual(carla.classes_taught.count(), 2)
        self.assertTrue(carla.must_change_password)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.is_school_admin); self.assertIn(self.c8, self.teacher.classes_taught.all())
        self.assertFalse(User.objects.filter(email='eve@x.edu').exists())

    def test_bulk_pupils_class_optional_and_template(self):
        self.client.force_login(self.admin)
        csv = 'First name,Email,Class\nAna,ana@x.edu,\nBo,bo@x.edu,Grade 7 - Newton\n'
        res = self.client.post('/s/ssc/api/bulk/pupils/import/', {'file': SimpleUploadedFile('p.csv', csv.encode()), 'commit': '1'}).json()
        self.assertEqual(res['summary']['create'], 2)
        self.assertEqual(User.objects.get(email='bo@x.edu').classes_enrolled.count(), 1)
        r = self.client.get('/s/ssc/api/bulk/teachers/template/')
        self.assertEqual(r.status_code, 200)

    def test_roles_and_classes_pages(self):
        self.client.force_login(self.admin)
        roles = self.client.get('/s/ssc/roles/')
        self.assertContains(roles, 'People &amp; roles'); self.assertContains(roles, 'id="bulkModal"')
        classes = self.client.get('/s/ssc/classes/')
        self.assertContains(classes, 'Bulk import year groups')
        self.assertNotContains(classes, 'createPupilBtn'); self.assertNotContains(classes, 'Class activity'); self.assertNotContains(classes, 'Recent activity'); self.assertNotContains(classes, 'View activity log')
