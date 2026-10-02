import json

from django.test import TestCase

from .models import ActivityLogEntry, Assignment, SchoolClass, User


class ClassPermissionTests(TestCase):
    """Admin-teachers (Staff status ticked, or superuser) manage classes; regular teachers
    can only assign work to pupils in the classes an admin has assigned them to."""

    def setUp(self):
        self.admin_teacher = User.objects.create_user(
            'admin_t', 'admin@example.com', 'pw', role=User.Role.TEACHER, is_staff=True)
        self.teacher = User.objects.create_user(
            'teacher', 'teacher@example.com', 'pw', role=User.Role.TEACHER)
        self.other_teacher = User.objects.create_user(
            'other', 'other@example.com', 'pw', role=User.Role.TEACHER)
        self.p1 = User.objects.create_user('p1', 'p1@example.com', 'pw', first_name='Ann', role=User.Role.PUPIL)
        self.p2 = User.objects.create_user('p2', 'p2@example.com', 'pw', first_name='Ben', role=User.Role.PUPIL)
        self.p3 = User.objects.create_user('p3', 'p3@example.com', 'pw', first_name='Cy', role=User.Role.PUPIL)
        self.mine = SchoolClass.objects.create(name='Grade 10 - Newton')
        self.mine.teachers.add(self.teacher)
        self.mine.pupils.add(self.p1, self.p2)
        self.theirs = SchoolClass.objects.create(name='Grade 9 - Curie')
        self.theirs.teachers.add(self.other_teacher)
        self.theirs.pupils.add(self.p3)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def create_assignment(self, recipients):
        return self.client.post('/api/assignments/create/', {
            'title': 'Quiz', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
            'recipients': json.dumps(recipients),
        })

    # ---- flags ----
    def test_can_manage_classes_flag(self):
        self.assertTrue(self.admin_teacher.can_manage_classes)
        self.assertFalse(self.teacher.can_manage_classes)
        su = User.objects.create_superuser('su', 'su@example.com', 'pw')
        self.assertTrue(su.can_manage_classes)

    # ---- regular teacher ----
    def test_teacher_cannot_create_update_delete_classes_or_pupils(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.post_json('/api/classes/create/', {'name': 'New'}).status_code, 403)
        self.assertEqual(self.post_json(f'/api/classes/{self.mine.id}/update/', {'pupilIds': [self.p3.id]}).status_code, 403)
        self.assertEqual(self.post_json(f'/api/classes/{self.mine.id}/delete/', {}).status_code, 403)
        self.assertEqual(self.post_json('/api/pupils/create/', {'firstName': 'X', 'email': 'x@example.com'}).status_code, 403)
        self.assertEqual(list(self.mine.pupils.order_by('id')), [self.p1, self.p2])
        self.assertTrue(SchoolClass.objects.filter(id=self.mine.id).exists())

    def test_teacher_sees_only_own_classes_and_pupils(self):
        self.client.force_login(self.teacher)
        classes = self.client.get('/api/classes/').json()
        self.assertFalse(classes['canManageClasses'])
        self.assertEqual([c['name'] for c in classes['classes']], ['Grade 10 - Newton'])
        self.assertNotIn('teachers', classes)
        pupils = self.client.get('/api/pupils/').json()['pupils']
        self.assertEqual(sorted(p['email'] for p in pupils), ['p1@example.com', 'p2@example.com'])

    def test_teacher_can_assign_to_own_class_pupils(self):
        self.client.force_login(self.teacher)
        resp = self.create_assignment([{'name': 'Ann', 'email': 'p1@example.com'}, {'name': 'Ben', 'email': 'p2@example.com'}])
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(Assignment.objects.get().recipients.count(), 2)

    def test_teacher_cannot_assign_outside_their_classes(self):
        self.client.force_login(self.teacher)
        resp = self.create_assignment([{'name': 'Ann', 'email': 'p1@example.com'}, {'name': 'Cy', 'email': 'p3@example.com'}])
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Assignment.objects.exists())

    def test_unassigned_teacher_has_no_pupils(self):
        lonely = User.objects.create_user('lonely', 'lonely@example.com', 'pw', role=User.Role.TEACHER)
        self.client.force_login(lonely)
        self.assertEqual(self.client.get('/api/classes/').json()['classes'], [])
        self.assertEqual(self.client.get('/api/pupils/').json()['pupils'], [])

    def test_teacher_gains_access_once_admin_assigns_them(self):
        self.client.force_login(self.admin_teacher)
        resp = self.post_json(f'/api/classes/{self.theirs.id}/update/',
                              {'teacherIds': [self.other_teacher.id, self.teacher.id]})
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(self.teacher)
        resp = self.create_assignment([{'name': 'Cy', 'email': 'p3@example.com'}])
        self.assertEqual(resp.status_code, 201)

    # ---- admin teacher ----
    def test_admin_manages_classes_and_teachers(self):
        self.client.force_login(self.admin_teacher)
        data = self.client.get('/api/classes/').json()
        self.assertTrue(data['canManageClasses'])
        self.assertEqual(len(data['classes']), 2)
        self.assertIn('teacher@example.com', [t['email'] for t in data['teachers']])
        self.assertNotIn('p1@example.com', [t['email'] for t in data['teachers']])

        resp = self.post_json('/api/classes/create/', {'name': 'Grade 11 - Darwin', 'teacherIds': [self.teacher.id],
                                                       'pupilIds': [self.p3.id]})
        self.assertEqual(resp.status_code, 201)
        cls = SchoolClass.objects.get(name='Grade 11 - Darwin')
        self.assertEqual(list(cls.teachers.all()), [self.teacher])
        # Pupils can't be smuggled in as teachers.
        self.post_json(f'/api/classes/{cls.id}/update/', {'teacherIds': [self.teacher.id, self.p1.id]})
        self.assertEqual(list(cls.teachers.all()), [self.teacher])
        # Editing pupils doesn't silently add the admin as a teacher.
        self.post_json(f'/api/classes/{cls.id}/update/', {'pupilIds': [self.p1.id]})
        self.assertNotIn(self.admin_teacher, cls.teachers.all())

        self.assertEqual(self.post_json('/api/pupils/create/', {'firstName': 'Dee', 'email': 'd@example.com',
                                                               'classId': cls.id}).status_code, 201)
        self.assertEqual(self.post_json(f'/api/classes/{cls.id}/delete/', {}).status_code, 200)

    def test_admin_can_assign_to_any_pupil(self):
        self.client.force_login(self.admin_teacher)
        self.assertEqual(len(self.client.get('/api/pupils/').json()['pupils']), 3)
        resp = self.create_assignment([{'name': 'Cy', 'email': 'p3@example.com'}])
        self.assertEqual(resp.status_code, 201)

    def test_dashboard_renders_for_both(self):
        for user, manage in ((self.teacher, False), (self.admin_teacher, True)):
            self.client.force_login(user)
            html = self.client.get('/').content.decode()
            self.assertIn(f'data-can-manage-classes="{1 if manage else 0}"', html)
            self.assertEqual('id="quickNewClassBtn"' in html, manage)
            self.assertEqual('id="createClassBtn"' in html, manage)


class ClassAndActivityPageTests(TestCase):
    def setUp(self):
        self.admin_teacher = User.objects.create_user('admin_t', 'admin@example.com', 'pw', role=User.Role.TEACHER, is_staff=True)
        self.teacher = User.objects.create_user('teacher', 'teacher@example.com', 'pw', role=User.Role.TEACHER)
        self.pupil = User.objects.create_user('p1', 'p1@example.com', 'pw', first_name='Ann', role=User.Role.PUPIL)
        self.p3 = User.objects.create_user('p3', 'p3@example.com', 'pw', first_name='Cy', role=User.Role.PUPIL)
        self.mine = SchoolClass.objects.create(name='Newton')
        self.other = SchoolClass.objects.create(name='Curie')
        self.other.pupils.add(self.p3)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_pages_render_and_pupils_are_redirected(self):
        for user, word in ((self.admin_teacher, 'Classes'), (self.teacher, 'My classes')):
            self.client.force_login(user)
            for url in ('/classes/', '/activity/'):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(word, resp.content.decode())
            self.assertEqual('id="createClassBtn"' in self.client.get('/classes/').content.decode(),
                             user == self.admin_teacher)
        self.client.force_login(self.pupil)
        self.assertRedirects(self.client.get('/classes/'), '/', fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/activity/'), '/', fetch_redirect_response=False)

    def test_class_activity_is_logged_and_filterable(self):
        self.client.force_login(self.admin_teacher)
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'teacherIds': [self.teacher.id], 'pupilIds': [self.pupil.id]})
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'name': 'Newton A'})
        self.post_json(f'/api/classes/{self.other.id}/update/', {'name': 'Curie B'})
        data = self.client.get(f'/api/activity-log/?class={self.mine.id}').json()
        descs = [e['description'] for e in data['entries']]
        self.assertTrue(any('Added Ann' in d for d in descs))
        self.assertTrue(any('Renamed class "Newton" to "Newton A"' in d for d in descs))
        self.assertFalse(any('Curie' in d for d in descs))
        self.assertEqual(data['selectedClass']['name'], 'Newton A')
        cats = self.client.get('/api/activity-log/?category=classes').json()['entries']
        self.assertTrue(cats and all(e['category'] == 'classes' for e in cats))

    def test_assignment_activity_shows_under_pupils_class(self):
        self.mine.teachers.add(self.teacher)
        self.mine.pupils.add(self.pupil)
        self.client.force_login(self.teacher)
        self.client.post('/api/assignments/create/', {
            'title': 'Essay', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
            'recipients': json.dumps([{'name': 'Ann', 'email': 'p1@example.com'}])})
        data = self.client.get(f'/api/activity-log/?class={self.mine.id}').json()
        self.assertEqual([e['assignmentTitle'] for e in data['entries']], ['Essay'])
        self.assertEqual(self.client.get(f'/api/activity-log/?class={self.other.id}').status_code, 403)

    def test_regular_teacher_scope(self):
        self.mine.teachers.add(self.teacher)
        self.client.force_login(self.admin_teacher)
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'pupilIds': [self.pupil.id]})
        self.post_json(f'/api/classes/{self.other.id}/update/', {'name': 'Curie B'})
        self.client.force_login(self.teacher)
        data = self.client.get('/api/activity-log/').json()
        descs = ' | '.join(e['description'] for e in data['entries'])
        self.assertIn('Newton', descs)
        self.assertNotIn('Curie', descs)
        self.assertEqual([c['name'] for c in data['classes']], ['Newton'])
        self.assertFalse(data['canManageClasses'])

    def test_pagination(self):
        self.client.force_login(self.admin_teacher)
        for i in range(5):
            self.post_json(f'/api/classes/{self.mine.id}/update/', {'name': f'Newton {i}'})
        first = self.client.get('/api/activity-log/?limit=3').json()
        self.assertTrue(first['hasMore'])
        second = self.client.get(f'/api/activity-log/?limit=3&offset={first["nextOffset"]}').json()
        self.assertFalse(second['hasMore'])
        self.assertEqual(len({e['id'] for e in first['entries'] + second['entries']}), 5)


class TeacherRoleTests(TestCase):
    def setUp(self):
        self.main = User.objects.create_superuser('main', 'main@example.com', 'pw')
        self.staff_admin = User.objects.create_user('sa', 'sa@example.com', 'pw', role=User.Role.TEACHER, is_staff=True)
        self.teacher = User.objects.create_user('t', 't@example.com', 'pw', first_name='Tina', role=User.Role.TEACHER)
        self.pupil = User.objects.create_user('p', 'p@example.com', 'pw', role=User.Role.PUPIL)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_main_admin_can_promote_and_demote(self):
        self.client.force_login(self.main)
        self.assertEqual(self.client.get('/roles/').status_code, 200)
        self.assertRedirects(self.client.get('/teachers/'), '/roles/', fetch_redirect_response=False)
        listing = self.client.get('/api/teachers/').json()['teachers']
        self.assertEqual({t['username'] for t in listing}, {'main', 'sa', 't'})
        resp = self.post_json(f'/api/teachers/{self.teacher.id}/admin/', {'isAdmin': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['isAdmin'])
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.can_manage_classes)
        # Promotion takes effect: the teacher can now create classes.
        self.client.force_login(self.teacher)
        self.assertEqual(self.post_json('/api/classes/create/', {'name': 'X'}).status_code, 201)
        self.client.force_login(self.main)
        self.post_json(f'/api/teachers/{self.teacher.id}/admin/', {'isAdmin': False})
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.can_manage_classes)
        actions = list(ActivityLogEntry.objects.filter(action__in=['admin_granted', 'admin_revoked']).values_list('action', flat=True))
        self.assertCountEqual(actions, ['admin_granted', 'admin_revoked'])
        roles = self.client.get('/api/activity-log/?category=roles').json()['entries']
        self.assertEqual(len(roles), 2)

    def test_cannot_change_superuser_or_pupil(self):
        self.client.force_login(self.main)
        self.assertEqual(self.post_json(f'/api/teachers/{self.main.id}/admin/', {'isAdmin': False}).status_code, 400)
        self.assertEqual(self.post_json(f'/api/teachers/{self.pupil.id}/admin/', {'isAdmin': True}).status_code, 400)
        self.pupil.refresh_from_db()
        self.assertFalse(self.pupil.is_staff)

    def test_only_main_admin_can_manage_roles(self):
        for user in (self.staff_admin, self.teacher):
            self.client.force_login(user)
            self.assertEqual(self.client.get('/api/teachers/').status_code, 403)
            self.assertEqual(self.post_json(f'/api/teachers/{self.teacher.id}/admin/', {'isAdmin': True}).status_code, 403)
            self.assertRedirects(self.client.get('/roles/'), '/classes/', fetch_redirect_response=False)
            self.assertNotIn('href="/roles/"', self.client.get('/classes/').content.decode())
        self.client.force_login(self.pupil)
        self.assertRedirects(self.client.get('/roles/'), '/', fetch_redirect_response=False)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_staff)


class RolesTabAndTourTests(TestCase):
    def setUp(self):
        self.main = User.objects.create_superuser('main', 'main@example.com', 'pw')
        self.teacher = User.objects.create_user('t', 't@example.com', 'pw', role=User.Role.TEACHER)
        self.pupil = User.objects.create_user('p', 'p@example.com', 'pw', role=User.Role.PUPIL)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_roles_tab_on_every_teacher_page_for_main_admin_only(self):
        for url in ('/', '/classes/', '/activity/', '/roles/'):
            self.client.force_login(self.main)
            self.assertIn('href="/roles/"', self.client.get(url).content.decode(), url)
        self.client.force_login(self.teacher)
        for url in ('/', '/classes/', '/activity/'):
            self.assertNotIn('href="/roles/"', self.client.get(url).content.decode(), url)

    def test_main_admin_adds_admin_and_teacher(self):
        self.client.force_login(self.main)
        r = self.post_json('/api/teachers/create/', {'firstName': 'Ana', 'email': 'ana@example.com', 'role': 'admin'})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.json()['isAdmin'])
        self.assertTrue(r.json()['tempPassword'])
        ana = User.objects.get(email='ana@example.com')
        self.assertEqual(ana.role, User.Role.TEACHER)
        self.assertTrue(ana.can_manage_classes)
        self.assertTrue(ana.check_password(r.json()['tempPassword']))
        r = self.post_json('/api/teachers/create/', {'firstName': 'Ben', 'email': 'ben@example.com', 'role': 'teacher'})
        self.assertEqual(r.status_code, 201)
        self.assertFalse(User.objects.get(email='ben@example.com').can_manage_classes)
        # Only "admin" / "teacher" are accepted, and emails stay unique.
        self.assertEqual(self.post_json('/api/teachers/create/', {'firstName': 'X', 'email': 'x@example.com', 'role': 'main_admin'}).status_code, 400)
        self.assertEqual(self.post_json('/api/teachers/create/', {'firstName': 'X', 'email': 'x@example.com', 'role': 'pupil'}).status_code, 400)
        self.assertEqual(self.post_json('/api/teachers/create/', {'firstName': 'A', 'email': 'ANA@example.com', 'role': 'teacher'}).status_code, 400)
        self.assertFalse(User.objects.filter(email='x@example.com').exists())
        self.assertEqual(ActivityLogEntry.objects.filter(action='teacher_created').count(), 2)

    def test_only_main_admin_can_add_people(self):
        self.client.force_login(self.teacher)
        self.assertEqual(self.post_json('/api/teachers/create/', {'firstName': 'X', 'email': 'x@example.com', 'role': 'admin'}).status_code, 403)

    def test_tour_shows_until_done(self):
        for user, role in ((self.main, 'main_admin'), (self.teacher, 'teacher'), (self.pupil, 'pupil')):
            self.client.force_login(user)
            html = self.client.get('/').content.decode()
            self.assertIn(f'const ROLE = "{role}"', html)
            self.assertIn('const AUTO_START = true', html)
            self.assertEqual(self.client.post('/api/account/tour-done/').status_code, 200)
            self.assertIn('const AUTO_START = false', self.client.get('/').content.decode())
            self.assertIn('data-start-tour', self.client.get('/').content.decode())
        self.client.force_login(self.teacher)
        self.assertIn('const AUTO_START = false', self.client.get('/classes/').content.decode())
        self.teacher.is_staff = True
        self.teacher.save()
        self.assertEqual(self.teacher.tour_role, 'admin')
