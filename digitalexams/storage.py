"""File storage that keeps every upload on local disk AND in the database.

Why: Andam runs from more than one place against one shared database (a local copy and
the Railway deployment), and Railway's disk is wiped on every deploy. With plain disk
storage, a PDF uploaded on one machine (or before a redeploy) is missing everywhere else —
pupils' and teachers' viewers then fail to open it. Reading here prefers the local disk
(fast) and falls back to the database copy, re-caching it to disk when possible.

Existing files that only live on one machine's disk can be copied into the database with:
    python manage.py sync_media_to_db
"""
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import DatabaseError

logger = logging.getLogger(__name__)


def _model():
    from .models import StoredFile
    return StoredFile


class DatabaseBackedStorage(FileSystemStorage):

    def _save(self, name, content):
        content.seek(0)
        data = content.read()
        try:
            name = super()._save(name, ContentFile(data))
        except OSError:
            logger.warning('Could not write %s to local disk; keeping the database copy only.', name)
        _model().objects.update_or_create(name=name, defaults={'content': data, 'size': len(data)})
        return name

    def _open(self, name, mode='rb'):
        if super().exists(name):
            return super()._open(name, mode)
        row = _model().objects.filter(name=name).only('content').first()
        if row is None:
            raise FileNotFoundError(name)
        data = bytes(row.content)
        try:  # cache it on this machine's disk for next time
            super()._save(name, ContentFile(data))
        except Exception:
            pass
        f = ContentFile(data, name=name)
        f.mode = mode
        return f

    def exists(self, name):
        if super().exists(name):
            return True
        try:
            return _model().objects.filter(name=name).exists()
        except DatabaseError:
            return False

    def size(self, name):
        if super().exists(name):
            return super().size(name)
        row = _model().objects.filter(name=name).only('size').first()
        if row is None:
            raise FileNotFoundError(name)
        return row.size

    def delete(self, name):
        if super().exists(name):
            super().delete(name)
        _model().objects.filter(name=name).delete()
