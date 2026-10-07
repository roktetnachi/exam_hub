import json

from django.test import TestCase

from .models import ActivityLogEntry, Assignment, School, SchoolClass, Subject, User


class ClassPermissionTests(TestCase):
    """Admin-teachers (is_school_admin=True, or superuser) manage classes; regular teachers
    can only assign work to pupils in the classes an admin has assigned them to."""

    def setUp(self):
        self.school = School.objects.create(name='Class Test School', slug='class-test')
        self.admin_teacher = User.objects.create_user(
            'admin_t', 'admin@example.com', 'pw', role=User.Role.TEACHER, school=self.school, is_school_admin=True)
        self.teacher = User.objects.create_user(
            'teacher', 'teacher@example.com', 'pw', role=User.Role.TEACHER, school=self.school)
        self.other_teacher = User.objects.create_user(
            'other', 'other@example.com', 'pw', role=User.Role.TEACHER, school=self.school)
        self.p1 = User.objects.create_user('p1', 'p1@example.com', 'pw', first_name='Ann', role=User.Role.PUPIL, school=self.school)
        self.p2 = User.objects.create_user('p2', 'p2@example.com', 'pw', first_name='Ben', role=User.Role.PUPIL, school=self.school)
        self.p3 = User.objects.create_user('p3', 'p3@example.com', 'pw', first_name='Cy', role=User.Role.PUPIL, school=self.school)
        self.mine = SchoolClass.objects.create(name='Grade 10 - Newton', school=self.school)
        self.mine.teachers.add(self.teacher)
        self.mine.pupils.add(self.p1, self.p2)
        self.theirs = SchoolClass.objects.create(name='Grade 9 - Curie', school=self.school)
        self.theirs.teachers.add(self.other_teacher)
        self.theirs.pupils.add(self.p3)
        # Plain teachers assign by SUBJECT (year-group membership alone doesn't grant it).
        self.physics = Subject.objects.create(name='Physics 10', school=self.school)
        self.physics.teachers.add(self.teacher); self.physics.pupils.add(self.p1, self.p2)
        self.chem = Subject.objects.create(name='Chemistry 9', school=self.school)
        self.chem.teachers.add(self.other_teacher); self.chem.pupils.add(self.p3)

    def post_json(self, url, data):
        return self.client.post(f'/s/{self.school.slug}{url}', json.dumps(data), content_type='application/json')

    def get_url(self, url):
        return self.client.get(f'/s/{self.school.slug}{url}')

    def create_assignment(self, recipients):
        return self.client.post(f'/s/{self.school.slug}/api/assignments/create/', {
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
        classes = self.get_url('/api/classes/').json()
        self.assertFalse(classes['canManageClasses'])
        self.assertEqual([c['name'] for c in classes['classes']], ['Grade 10 - Newton'])
        self.assertNotIn('teachers', classes)
        pupils = self.get_url('/api/pupils/').json()['pupils']
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
        lonely = User.objects.create_user('lonely', 'lonely@example.com', 'pw', role=User.Role.TEACHER, school=self.school)
        self.client.force_login(lonely)
        self.assertEqual(self.get_url('/api/classes/').json()['classes'], [])
        self.assertEqual(self.get_url('/api/pupils/').json()['pupils'], [])

    def test_teacher_gains_access_once_admin_assigns_them(self):
        self.client.force_login(self.admin_teacher)
        # Attaching the teacher to the year group is NOT enough to assign work to its pupils…
        self.post_json(f'/api/classes/{self.theirs.id}/update/', {'teacherIds': [self.other_teacher.id, self.teacher.id]})
        self.client.force_login(self.teacher)
        self.assertEqual(self.create_assignment([{'name': 'Cy', 'email': 'p3@example.com'}]).status_code, 403)
        # …teaching the subject is.
        self.client.force_login(self.admin_teacher)
        resp = self.post_json(f'/api/subjects/{self.chem.id}/update/', {'teacherIds': [self.other_teacher.id, self.teacher.id]})
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(self.teacher)
        self.assertEqual(self.create_assignment([{'name': 'Cy', 'email': 'p3@example.com'}]).status_code, 201)

    # ---- admin teacher ----
    def test_admin_manages_classes_and_teachers(self):
        self.client.force_login(self.admin_teacher)
        data = self.get_url('/api/classes/').json()
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
        self.assertEqual(len(self.get_url('/api/pupils/').json()['pupils']), 3)
        resp = self.create_assignment([{'name': 'Cy', 'email': 'p3@example.com'}])
        self.assertEqual(resp.status_code, 201)

    def test_dashboard_renders_for_both(self):
        for user, manage in ((self.teacher, False), (self.admin_teacher, True)):
            self.client.force_login(user)
            html = self.get_url('/').content.decode()
            self.assertIn(f'data-can-manage-classes="{1 if manage else 0}"', html)
            self.assertEqual('id="quickNewClassBtn"' in html, manage)
            self.assertEqual('id="createClassBtn"' in html, manage)


class ClassAndActivityPageTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Activity Test School', slug='activity-test')
        self.admin_teacher = User.objects.create_user('admin_t', 'admin@example.com', 'pw', role=User.Role.TEACHER, school=self.school, is_school_admin=True)
        self.teacher = User.objects.create_user('teacher', 'teacher@example.com', 'pw', role=User.Role.TEACHER, school=self.school)
        self.pupil = User.objects.create_user('p1', 'p1@example.com', 'pw', first_name='Ann', role=User.Role.PUPIL, school=self.school)
        self.p3 = User.objects.create_user('p3', 'p3@example.com', 'pw', first_name='Cy', role=User.Role.PUPIL, school=self.school)
        self.mine = SchoolClass.objects.create(name='Newton', school=self.school)
        self.other = SchoolClass.objects.create(name='Curie', school=self.school)
        self.other.pupils.add(self.p3)

    def post_json(self, url, data):
        return self.client.post(f'/s/{self.school.slug}{url}', json.dumps(data), content_type='application/json')

    def get_url(self, url):
        return self.client.get(f'/s/{self.school.slug}{url}')

    def test_pages_render_and_pupils_are_redirected(self):
        self.client.force_login(self.admin_teacher)
        resp = self.get_url('/classes/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Year groups', resp.content.decode())
        self.assertIn('id="createClassBtn"', resp.content.decode())
        self.assertEqual(self.get_url('/activity/').status_code, 200)
        # Plain teachers work by subject: no Year groups page (sent to Subjects), no Activity.
        self.client.force_login(self.teacher)
        self.assertRedirects(self.get_url('/classes/'), f'/s/{self.school.slug}/subjects/', fetch_redirect_response=False)
        self.assertEqual(self.get_url('/activity/').status_code, 302)
        self.assertNotIn('>My year groups<', self.get_url('/subjects/').content.decode())
        self.client.force_login(self.pupil)
        self.assertRedirects(self.get_url('/classes/'), f'/s/{self.school.slug}/', fetch_redirect_response=False)
        self.assertRedirects(self.get_url('/activity/'), f'/s/{self.school.slug}/', fetch_redirect_response=False)

    def test_class_activity_is_logged_and_filterable(self):
        self.client.force_login(self.admin_teacher)
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'teacherIds': [self.teacher.id], 'pupilIds': [self.pupil.id]})
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'name': 'Newton A'})
        self.post_json(f'/api/classes/{self.other.id}/update/', {'name': 'Curie B'})
        data = self.get_url(f'/api/activity-log/?class={self.mine.id}').json()
        descs = [e['description'] for e in data['entries']]
        self.assertTrue(any('Added Ann' in d for d in descs))
        self.assertTrue(any('Renamed year group "Newton" to "Newton A"' in d for d in descs))
        self.assertFalse(any('Curie' in d for d in descs))
        self.assertEqual(data['selectedClass']['name'], 'Newton A')
        cats = self.get_url('/api/activity-log/?category=classes').json()['entries']
        self.assertTrue(cats and all(e['category'] == 'classes' for e in cats))

    def test_assignment_activity_shows_under_pupils_class(self):
        self.mine.teachers.add(self.teacher)
        self.mine.pupils.add(self.pupil)
        subject = Subject.objects.create(name='English', school=self.school)
        subject.teachers.add(self.teacher); subject.pupils.add(self.pupil)
        self.client.force_login(self.teacher)
        self.client.post(f'/s/{self.school.slug}/api/assignments/create/', {
            'title': 'Essay', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
            'recipients': json.dumps([{'name': 'Ann', 'email': 'p1@example.com'}])})
        self.assertEqual(self.get_url(f'/api/activity-log/?class={self.mine.id}').status_code, 403)  # admin-only
        self.client.force_login(self.admin_teacher)
        data = self.get_url(f'/api/activity-log/?class={self.mine.id}').json()
        self.assertEqual([e['assignmentTitle'] for e in data['entries']], ['Essay'])

    def test_regular_teacher_scope(self):
        self.mine.teachers.add(self.teacher)
        self.client.force_login(self.admin_teacher)
        self.post_json(f'/api/classes/{self.mine.id}/update/', {'pupilIds': [self.pupil.id]})
        self.post_json(f'/api/classes/{self.other.id}/update/', {'name': 'Curie B'})
        self.client.force_login(self.teacher)
        # The activity log is admin-only now; a plain teacher still sees just their own classes.
        self.assertEqual(self.get_url('/api/activity-log/').status_code, 403)
        self.assertEqual([c['name'] for c in self.get_url('/api/classes/').json()['classes']], ['Newton'])

    def test_pagination(self):
        self.client.force_login(self.admin_teacher)
        for i in range(5):
            self.post_json(f'/api/classes/{self.mine.id}/update/', {'name': f'Newton {i}'})
        first = self.get_url('/api/activity-log/?limit=3').json()
        self.assertTrue(first['hasMore'])
        second = self.get_url(f'/api/activity-log/?limit=3&offset={first["nextOffset"]}').json()
        self.assertFalse(second['hasMore'])
        self.assertEqual(len({e['id'] for e in first['entries'] + second['entries']}), 5)


class TeacherRoleTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Role Test School', slug='role-test')
        self.main = User.objects.create_superuser('main', 'main@example.com', 'pw')
        self.staff_admin = User.objects.create_user('sa', 'sa@example.com', 'pw', role=User.Role.TEACHER, school=self.school, is_school_admin=True)
        self.teacher = User.objects.create_user('t', 't@example.com', 'pw', first_name='Tina', role=User.Role.TEACHER, school=self.school)
        self.pupil = User.objects.create_user('p', 'p@example.com', 'pw', role=User.Role.PUPIL, school=self.school)

    def post_json(self, url, data):
        return self.client.post(f'/s/{self.school.slug}{url}', json.dumps(data), content_type='application/json')

    def get_url(self, url):
        return self.client.get(f'/s/{self.school.slug}{url}')

    def test_main_admin_can_promote_and_demote(self):
        self.client.force_login(self.main)
        self.assertEqual(self.get_url('/roles/').status_code, 200)
        self.assertRedirects(self.get_url('/teachers/'), f'/s/{self.school.slug}/roles/', fetch_redirect_response=False)
        listing = self.get_url('/api/teachers/').json()['teachers']
        self.assertEqual({t['username'] for t in listing}, {'sa', 't'})
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
        roles = self.get_url('/api/activity-log/?category=roles').json()['entries']
        self.assertEqual(len(roles), 2)

    def test_cannot_change_superuser_or_pupil(self):
        self.client.force_login(self.main)
        self.assertEqual(self.post_json(f'/api/teachers/{self.main.id}/admin/', {'isAdmin': False}).status_code, 400)
        self.assertEqual(self.post_json(f'/api/teachers/{self.pupil.id}/admin/', {'isAdmin': True}).status_code, 400)
        self.pupil.refresh_from_db()
        self.assertFalse(self.pupil.is_school_admin)

    def test_only_main_admin_can_manage_roles(self):
        for user in (self.staff_admin, self.teacher):
            self.client.force_login(user)
            self.assertEqual(self.get_url('/api/teachers/').status_code, 403)
            self.assertEqual(self.post_json(f'/api/teachers/{self.teacher.id}/admin/', {'isAdmin': True}).status_code, 403)
            self.assertRedirects(self.get_url('/roles/'), f'/s/{self.school.slug}/classes/', fetch_redirect_response=False)
            self.assertNotIn('href="/roles/"', self.get_url('/classes/').content.decode())
        self.client.force_login(self.pupil)
        self.assertRedirects(self.get_url('/roles/'), f'/s/{self.school.slug}/', fetch_redirect_response=False)
        self.teacher.refresh_from_db()
        self.assertFalse(self.teacher.is_school_admin)


class RolesTabAndTourTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name='Tour Test School', slug='tour-test')
        self.main = User.objects.create_superuser('main', 'main@example.com', 'pw')
        self.teacher = User.objects.create_user('t', 't@example.com', 'pw', role=User.Role.TEACHER, school=self.school)
        self.pupil = User.objects.create_user('p', 'p@example.com', 'pw', role=User.Role.PUPIL, school=self.school)

    def post_json(self, url, data):
        return self.client.post(f'/s/{self.school.slug}{url}', json.dumps(data), content_type='application/json')

    def get_url(self, url):
        return self.client.get(f'/s/{self.school.slug}{url}')

    def test_roles_tab_on_every_teacher_page_for_main_admin_only(self):
        for url in ('/', '/classes/', '/activity/', '/roles/'):
            self.client.force_login(self.main)
            self.assertIn('Roles', self.get_url(url).content.decode(), url)
        self.client.force_login(self.teacher)
        for url in ('/', '/classes/', '/activity/'):
            self.assertNotIn('Roles', self.get_url(url).content.decode(), url)

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
            html = self.get_url('/').content.decode()
            self.assertIn(f'const ROLE = "{role}"', html)
            self.assertIn('const AUTO_START = true', html)
            self.assertEqual(self.post_json('/api/account/tour-done/', {}).status_code, 200)
            self.assertIn('const AUTO_START = false', self.get_url('/').content.decode())
            self.assertIn('data-start-tour', self.get_url('/').content.decode())
        self.client.force_login(self.teacher)
        self.assertIn('const AUTO_START = false', self.get_url('/subjects/').content.decode())
        self.teacher.is_school_admin = True
        self.teacher.save()
        self.assertEqual(self.teacher.tour_role, 'admin')


class MultiTenancyAndSuperAdminTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser('superadmin', 'super@platform.com', 'pw123')
        self.school1 = School.objects.create(name='St Mary Academy', slug='st-mary', primary_color='#e11d48', is_active=True)
        self.school2 = School.objects.create(name='Lincoln High', slug='lincoln', primary_color='#2563eb', is_active=True)
        self.inactive_school = School.objects.create(name='Old School', slug='old-school', is_active=False)

        self.school1_admin = User.objects.create_user(
            'admin1', 'admin1@stmary.edu', 'pw', role=User.Role.TEACHER, school=self.school1, is_school_admin=True
        )
        self.school1_teacher = User.objects.create_user(
            'teach1', 'teach1@stmary.edu', 'pw', role=User.Role.TEACHER, school=self.school1
        )
        self.school1_pupil = User.objects.create_user(
            'pupil1', 'pupil1@stmary.edu', 'pw', role=User.Role.PUPIL, school=self.school1
        )

        self.school2_pupil = User.objects.create_user(
            'pupil2', 'pupil2@lincoln.edu', 'pw', role=User.Role.PUPIL, school=self.school2
        )

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type='application/json')

    def test_superadmin_dashboard_access(self):
        # Unauthenticated redirects to login
        resp = self.client.get('/superadmin/')
        self.assertEqual(resp.status_code, 302)

        # Regular teacher gets 403
        self.client.force_login(self.school1_teacher)
        resp = self.client.get('/superadmin/')
        self.assertEqual(resp.status_code, 403)

        # Superuser gets 200 with dashboard
        self.client.force_login(self.superuser)
        resp = self.client.get('/superadmin/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('St Mary Academy', resp.content.decode())
        self.assertIn('Lincoln High', resp.content.decode())

    def test_superadmin_create_school_api(self):
        self.client.force_login(self.superuser)
        payload = {
            'name': 'Greenwood High',
            'slug': 'greenwood',
            'primary_color': '#059669',
            'timezone': 'Asia/Manila',
            'admin_username': 'gw_principal',
            'admin_email': 'principal@greenwood.edu',
            'admin_password': 'securepassword123',
            'admin_full_name': 'Principal Green',
        }
        resp = self.post_json('/superadmin/api/schools/create/', payload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(School.objects.filter(slug='greenwood').exists())
        gw_admin = User.objects.get(username='gw_principal')
        self.assertTrue(gw_admin.is_school_admin)
        self.assertEqual(gw_admin.school.slug, 'greenwood')

    def test_superadmin_toggle_school_active_api(self):
        self.client.force_login(self.superuser)
        resp = self.post_json(f'/superadmin/api/schools/{self.school1.id}/toggle-active/', {})
        self.assertEqual(resp.status_code, 200)
        self.school1.refresh_from_db()
        self.assertFalse(self.school1.is_active)

    def test_superadmin_create_school_admin_api(self):
        self.client.force_login(self.superuser)
        payload = {
            'username': 'new_admin_mary',
            'email': 'new_admin@stmary.edu',
            'password': 'password123',
            'full_name': 'Mary Smith',
        }
        resp = self.post_json(f'/superadmin/api/schools/{self.school1.id}/create-admin/', payload)
        self.assertEqual(resp.status_code, 200)
        new_adm = User.objects.get(username='new_admin_mary')
        self.assertTrue(new_adm.is_school_admin)
        self.assertEqual(new_adm.school, self.school1)

    def test_tenant_url_routing_and_isolation(self):
        # 404 for unknown school slug
        resp = self.client.get('/s/nonexistent-school/')
        self.assertEqual(resp.status_code, 404)

        # 403 for inactive school
        resp = self.client.get(f'/s/{self.inactive_school.slug}/')
        self.assertEqual(resp.status_code, 403)

        # Pupil 1 accessing their school dashboard
        self.client.force_login(self.school1_pupil)
        resp = self.client.get(f'/s/{self.school1.slug}/')
        self.assertEqual(resp.status_code, 200)

        # Pupil 1 trying to access School 2 gets 403
        resp = self.client.get(f'/s/{self.school2.slug}/')
        self.assertEqual(resp.status_code, 403)

    def test_branding_rendered_in_templates(self):
        self.client.force_login(self.school1_pupil)
        resp = self.client.get(f'/s/{self.school1.slug}/')
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('St Mary Academy', content)
        self.assertIn('#e11d48', content)



class BulkImportTests(TestCase):
    """Bulk import of classes (+ teachers) and pupils from .xlsx / .csv uploads."""

    def setUp(self):
        self.school = School.objects.create(name='Bulk School', slug='bulk')
        self.other_school = School.objects.create(name='Other School', slug='other')
        self.admin = User.objects.create_user('adm', 'adm@bulk.edu', 'pw', role=User.Role.TEACHER,
                                              school=self.school, is_school_admin=True)
        self.ana = User.objects.create_user('ana', 'ana@bulk.edu', 'pw', first_name='Ana', role=User.Role.TEACHER, school=self.school)
        self.ben = User.objects.create_user('ben', 'ben@bulk.edu', 'pw', first_name='Ben', role=User.Role.TEACHER, school=self.school)
        self.newton = SchoolClass.objects.create(name='Grade 7 - Newton', school=self.school)
        self.newton.teachers.add(self.ana)
        self.client.force_login(self.admin)

    def upload(self, kind, content, name='sheet.csv', commit=False, skip=False):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {'file': SimpleUploadedFile(name, content.encode() if isinstance(content, str) else content),
                'commit': '1' if commit else '0'}
        if skip:
            data['skipErrors'] = '1'
        return self.client.post(f'/s/{self.school.slug}/api/bulk/{kind}/import/', data)

    def xlsx(self, title, rows):
        import io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = title
        for r in rows:
            ws.append(r)
        wb.create_sheet('Instructions').append(['Class name *', 'nonsense'])
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    def test_only_admins(self):
        self.client.force_login(self.ana)
        self.assertEqual(self.upload('classes', 'Class name\nX\n').status_code, 403)
        self.assertEqual(self.client.get(f'/s/{self.school.slug}/api/bulk/pupils/template/').status_code, 403)

    def test_templates_have_instructions_as_second_tab(self):
        import io
        from openpyxl import load_workbook
        for kind, first in (('classes', 'Year groups'), ('pupils', 'Pupils')):
            resp = self.client.get(f'/s/{self.school.slug}/api/bulk/{kind}/template/')
            self.assertEqual(resp.status_code, 200)
            wb = load_workbook(io.BytesIO(resp.content))
            self.assertEqual(wb.sheetnames[:2], [first, 'Instructions'])
        # Classes template is pre-filled with existing classes and their teachers.
        wb = load_workbook(io.BytesIO(self.client.get(f'/s/{self.school.slug}/api/bulk/classes/template/').content))
        self.assertEqual(wb['Year groups']['A2'].value, 'Grade 7 - Newton')
        self.assertEqual(wb['Year groups']['B2'].value, 'ana@bulk.edu')

    def test_classes_preview_then_commit(self):
        csv_text = ('Class name *,Teacher emails,Rename to\n'
                    'Grade 7 - Newton,ana@bulk.edu,\n'               # unchanged
                    'grade 8 - curie,ben@bulk.edu; ana@bulk.edu,\n'  # create with 2 teachers
                    'Grade 9,nobody@bulk.edu,\n'                     # unknown teacher
                    'Grade 8 - Curie,,\n')                           # duplicate in file
        resp = self.upload('classes', csv_text)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual([r['status'] for r in data['rows']], ['unchanged', 'create', 'error', 'error'])
        self.assertFalse(SchoolClass.objects.filter(name='grade 8 - curie').exists())   # preview saves nothing
        self.assertEqual(self.upload('classes', csv_text, commit=True).status_code, 400)  # errors block
        resp = self.upload('classes', csv_text, commit=True, skip=True)
        self.assertTrue(resp.json()['committed'])
        curie = SchoolClass.objects.get(name='grade 8 - curie', school=self.school)
        self.assertEqual(set(curie.teachers.all()), {self.ana, self.ben})

    def test_classes_update_rename_and_clear_teachers(self):
        resp = self.upload('classes', self.xlsx('Classes', [
            ['My school export'],                              # title row above the header is fine
            ['Class', 'Teachers', 'Rename to'],
            ['GRADE 7  -  NEWTON', 'ben', 'Grade 7 - Einstein'],
        ]), name='classes.xlsx', commit=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.newton.refresh_from_db()
        self.assertEqual(self.newton.name, 'Grade 7 - Einstein')
        self.assertEqual(list(self.newton.teachers.all()), [self.ben])
        self.upload('classes', 'Class name,Teacher emails\nGrade 7 - Einstein,NONE\n', commit=True)
        self.assertFalse(self.newton.teachers.exists())

    def test_pupils_require_existing_class(self):
        csv_text = ('First name,Last name,Email,Class\n'
                    'Juan,Dela Cruz,juan@bulk.edu,Grade 7 - Newton\n'
                    'Maria,Santos,maria@bulk.edu,Grade 99 - Nowhere\n')
        data = self.upload('pupils', csv_text).json()
        self.assertEqual([r['status'] for r in data['rows']], ['create', 'error'])
        self.assertIn("doesn't exist", data['rows'][1]['message'])
        resp = self.upload('pupils', csv_text, commit=True, skip=True).json()
        self.assertEqual([c['email'] for c in resp['credentials']], ['juan@bulk.edu'])
        self.assertTrue(resp['credentials'][0]['tempPassword'])
        juan = User.objects.get(email='juan@bulk.edu')
        self.assertEqual((juan.role, juan.school, juan.get_full_name()), (User.Role.PUPIL, self.school, 'Juan Dela Cruz'))
        self.assertIn(juan, self.newton.pupils.all())
        self.assertFalse(User.objects.filter(email='maria@bulk.edu').exists())

    def test_pupils_existing_multi_class_and_conflicts(self):
        curie = SchoolClass.objects.create(name='Grade 8 - Curie', school=self.school)
        existing = User.objects.create_user('kim', 'Kim@bulk.edu', 'pw', first_name='Kim', role=User.Role.PUPIL, school=self.school)
        self.newton.pupils.add(existing)
        User.objects.create_user('far', 'far@other.edu', 'pw', role=User.Role.PUPIL, school=self.other_school)
        resp = self.upload('pupils', self.xlsx('Pupils', [
            ['Name', 'Email address', 'Section'],
            ['Tan, Ken', 'ken@bulk.edu', 'Grade 7 - Newton; Grade 8 - Curie'],
            ['', 'kim@bulk.edu', 'Grade 7 - Newton'],          # already enrolled → unchanged
            ['', 'KIM@bulk.edu', 'Grade 8 - Curie'],           # existing pupil → enrol
            ['Ana', 'ana@bulk.edu', 'Grade 8 - Curie'],        # teacher email
            ['Far', 'far@other.edu', 'Grade 8 - Curie'],       # same email at another school → a NEW account here
        ]), name='pupils.xlsx')
        rows = resp.json()['rows']
        self.assertEqual([r['status'] for r in rows], ['create', 'unchanged', 'update', 'error', 'create'])
        self.upload('pupils', self.xlsx('Pupils', [
            ['Name', 'Email address', 'Section'],
            ['Tan, Ken', 'ken@bulk.edu', 'Grade 7 - Newton; Grade 8 - Curie'],
            ['', 'KIM@bulk.edu', 'Grade 8 - Curie'],
        ]), name='pupils.xlsx', commit=True)
        ken = User.objects.get(email='ken@bulk.edu')
        self.assertEqual((ken.first_name, ken.last_name), ('Ken', 'Tan'))
        self.assertEqual(set(ken.classes_enrolled.all()), {self.newton, curie})
        self.assertIn(existing, curie.pupils.all())
        self.assertEqual(User.objects.filter(email__iexact='kim@bulk.edu').count(), 1)

    def test_bad_files(self):
        self.assertEqual(self.upload('pupils', 'foo,bar\n1,2\n').status_code, 400)
        self.assertEqual(self.upload('pupils', b'not excel', name='x.xlsx').status_code, 400)
        self.assertEqual(self.upload('pupils', 'x', name='x.pdf').status_code, 400)
