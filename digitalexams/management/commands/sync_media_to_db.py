"""Copy uploaded files that exist only on this machine's disk into the database, so every
copy of Andam (local and Railway) can serve them. Safe to run more than once.

    python manage.py sync_media_to_db
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from digitalexams.models import AssignmentItem, ExamResource, School, StoredFile


class Command(BaseCommand):
    help = "Copy uploaded files from local MEDIA_ROOT into the database (StoredFile)."

    def handle(self, *args, **options):
        root = Path(settings.MEDIA_ROOT)
        names = set()
        for item in AssignmentItem.objects.all():
            for field in (item.pdf, item.image, item.video, item.audio):
                if field and field.name:
                    names.add(field.name)
        for resource in ExamResource.objects.exclude(file=''):
            if resource.file and resource.file.name:
                names.add(resource.file.name)
        for school in School.objects.exclude(logo=''):
            if school.logo and school.logo.name:
                names.add(school.logo.name)
        have = set(StoredFile.objects.filter(name__in=names).values_list('name', flat=True))
        copied = missing = 0
        for name in sorted(names - have):
            path = root / name
            if not path.is_file():
                missing += 1
                self.stdout.write(self.style.WARNING(f'Not on this machine: {name}'))
                continue
            data = path.read_bytes()
            StoredFile.objects.create(name=name, content=data, size=len(data))
            copied += 1
            self.stdout.write(f'Copied {name} ({len(data) // 1024} KB)')
        self.stdout.write(self.style.SUCCESS(
            f'Done: {copied} copied, {len(have)} already stored, {missing} not found on this machine.'))
        if missing:
            self.stdout.write('Run this command on the machine where those files were uploaded.')
