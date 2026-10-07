"""Exam resources (guide PDFs / images / text in the pupil's floating Resources panel), and the
marking-tool / zoom markup that ships with them."""
import json
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import Assignment, AssignmentItem, ExamResource, Recipient, School, Subject, User

PDF_BYTES = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF'
PNG_BYTES = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
             b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
MEDIA = tempfile.mkdtemp(prefix='examhub-test-media-')


@override_settings(MEDIA_ROOT=MEDIA)
class ExamResourceTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.school = School.objects.create(name='SSC', slug='ssc')
        self.t = User.objects.create_user('t', 't@x.edu', 'x', school=self.school, role='teacher', first_name='Tess')
        self.other_t = User.objects.create_user('o', 'o@x.edu', 'x', school=self.school, role='teacher')
        self.p = User.objects.create_user('p', 'p@x.edu', 'x', school=self.school, first_name='Pia')
        self.p2 = User.objects.create_user('p2', 'p2@x.edu', 'x', school=self.school, first_name='Ola')
        subj = Subject.objects.create(school=self.school, name='Geography')
        subj.teachers.add(self.t); subj.pupils.add(self.p, self.p2)

    def create(self, resources_meta, files):
        return self.client.post('/s/ssc/api/assignments/create/', {
            'title': 'Maps', 'items_meta': json.dumps([{'type': 'long_answer', 'label': 'Q1', 'prompt': 'Why?'}]),
            'recipients': json.dumps([{'name': 'Pia', 'email': 'p@x.edu'}]),
            'resources_meta': json.dumps(resources_meta), 'resource_files': files})

    def test_create_exam_with_resources_and_pupil_can_open_them(self):
        self.client.force_login(self.t)
        r = self.create([{'kind': 'pdf', 'title': 'Source A'}, {'kind': 'text', 'title': 'Formulae', 'text': 'a² + b² = c²'},
                         {'kind': 'image', 'title': ''}],
                        [SimpleUploadedFile('a.pdf', PDF_BYTES, 'application/pdf'),
                         SimpleUploadedFile('map.png', PNG_BYTES, 'image/png')])
        self.assertEqual(r.status_code, 201, r.content)
        exam = Assignment.objects.get(id=r.json()['id'])
        self.assertEqual([(x.kind, x.title) for x in exam.resources.all()],
                         [('pdf', 'Source A'), ('text', 'Formulae'), ('image', 'Image 3')])
        # Pupil sees them in the exam payload and can open the files.
        self.client.force_login(self.p)
        res = self.client.get('/s/ssc/api/assignments/').json()['assignments'][0]['resources']
        self.assertEqual([x['kind'] for x in res], ['pdf', 'text', 'image'])
        self.assertEqual(res[1]['text'], 'a² + b² = c²')
        self.assertNotIn('url', res[1])
        f = self.client.get('/s/ssc' + res[0]['url'])
        self.assertEqual(f.status_code, 200)
        self.assertEqual(f['Content-Type'], 'application/pdf')
        self.assertEqual(b''.join(f.streaming_content), PDF_BYTES)
        self.assertEqual(self.client.get('/s/ssc' + res[2]['url'])['Content-Type'], 'image/png')
        # A pupil not on the exam, and another teacher, can't.
        self.client.force_login(self.p2)
        self.assertEqual(self.client.get('/s/ssc' + res[0]['url']).status_code, 403)
        self.client.force_login(self.other_t)
        self.assertEqual(self.client.get('/s/ssc' + res[0]['url']).status_code, 403)

    def test_bad_resources_are_rejected_before_anything_is_saved(self):
        self.client.force_login(self.t)
        r = self.create([{'kind': 'text', 'title': 'Empty', 'text': '   '}], [])
        self.assertEqual(r.status_code, 400)
        self.assertIn('has no text', r.content.decode())
        r = self.create([{'kind': 'pdf', 'title': 'Not a PDF'}], [SimpleUploadedFile('a.png', PNG_BYTES, 'image/png')])
        self.assertEqual(r.status_code, 400)
        r = self.create([{'kind': 'image', 'title': 'Missing'}], [])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Assignment.objects.exists())

    def test_add_and_remove_resources_on_an_existing_exam(self):
        exam = Assignment.objects.create(school=self.school, title='Essay', teacher_name='Tess', teacher_email='t@x.edu')
        AssignmentItem.objects.create(assignment=exam, item_type='long_answer', label='Q1', prompt='Why?')
        Recipient.objects.create(assignment=exam, pupil_name='Pia', pupil_email='p@x.edu')
        url = f'/s/ssc/api/assignments/{exam.id}/resources/add/'
        # Only the exam's own teacher (or an admin) may add.
        self.client.force_login(self.p)
        self.assertEqual(self.client.post(url, {'kind': 'text', 'title': 'Hint', 'text': 'Think'}).status_code, 403)
        self.client.force_login(self.other_t)
        self.assertEqual(self.client.post(url, {'kind': 'text', 'title': 'Hint', 'text': 'Think'}).status_code, 403)
        self.client.force_login(self.t)
        r = self.client.post(url, {'kind': 'text', 'title': 'Hint', 'text': 'Think about scale.'})
        self.assertEqual(r.status_code, 201, r.content)
        r2 = self.client.post(url, {'kind': 'image', 'title': 'Map', 'file': SimpleUploadedFile('m.png', PNG_BYTES, 'image/png')})
        self.assertEqual(r2.status_code, 201, r2.content)
        self.assertEqual(list(exam.resources.values_list('title', flat=True)), ['Hint', 'Map'])
        self.assertEqual(self.client.post(url, {'kind': 'video', 'title': 'X'}).status_code, 400)
        # Remove one.
        self.client.force_login(self.other_t)
        self.assertEqual(self.client.post(f'/s/ssc/api/resources/{r.json()["id"]}/delete/').status_code, 403)
        self.client.force_login(self.t)
        self.assertEqual(self.client.post(f'/s/ssc/api/resources/{r.json()["id"]}/delete/').json(), {'deleted': True})
        self.assertEqual(list(ExamResource.objects.values_list('title', flat=True)), ['Map'])

    def test_markup_tools_and_zoom(self):
        self.client.force_login(self.t)
        html = self.client.get('/s/ssc/').content.decode()
        for marker in ('id="openResourcesBtn"', 'id="resourcesModal"', 'data-add-resource="pdf"', 'data-edit-add-resource="text"',
                       'id="pdfZoomIn"', 'id="pdfZoomFit"', 'function mountZoomableImage', 'data-tool="highlight"'):
            self.assertIn(marker, html)
        self.assertNotIn('data-tool="circle"', html)
        self.assertNotIn('data-s="circle"', html)

    def test_pdf_highlight_mark_is_accepted(self):
        exam = Assignment.objects.create(school=self.school, title='Essay', teacher_name='Tess', teacher_email='t@x.edu')
        item = AssignmentItem.objects.create(assignment=exam, item_type='long_answer', label='Q1', prompt='Why?')
        rec = Recipient.objects.create(assignment=exam, pupil_name='Pia', pupil_email='p@x.edu')
        from .models import Submission
        Submission.objects.create(recipient=rec, status='submitted')
        self.client.force_login(self.t)
        marks = {'answers': {}, 'annComments': {},
                 'pdf': {str(item.id): [{'id': 'h1', 'page': 1, 'shape': 'highlight', 'x': 5, 'y': 5, 'w': 80, 'h': 14, 'comment': ''}]}}
        r = self.client.post(f'/s/ssc/api/submissions/{rec.id}/marks/', json.dumps({'teacherMarks': marks}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Submission.objects.get(recipient=rec).teacher_marks['pdf'][str(item.id)][0]['shape'], 'highlight')
