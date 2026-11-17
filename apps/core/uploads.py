"""Saving and removing private evidence files under UPLOAD_FOLDER."""

import os
from datetime import datetime
from io import BytesIO

from flask import current_app
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from apps.core.registry import resolve_document_upload_path
from apps.models import wib_now

# Phone photos (often 3-5 MB) are shrunk to this longest side, about 300-500 KB:
# still sharp enough to read a signed HO, and every backup copies them all.
MAX_PHOTO_SIDE = 1600
PHOTO_QUALITY = 80
PHOTO_FORMATS = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP'}
EXIF_IFD = 0x8769
EXIF_DATE_TAKEN = 0x9003  # DateTimeOriginal
EXIF_DATE_CHANGED = 0x0132  # DateTime, used when a phone writes no DateTimeOriginal


def photo_taken_at(image):
    """When the photo was taken, by the phone's own clock, or None when the photo does not say."""
    exif = image.getexif()
    raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATE_TAKEN) or exif.get(EXIF_DATE_CHANGED)
    try:
        return datetime.strptime(str(raw).strip('\x00 '), '%Y:%m:%d %H:%M:%S') if raw else None
    except ValueError:
        return None


def save_upload_file(file_storage, full_path):
    """Store an upload at full_path, shrinking photos; PDFs and unreadable images are kept as sent.

    A stored photo carries no metadata at all (GPS, phone model, ...): only its date
    taken is kept, returned here for the database. Returns that date or None.
    """
    data = file_storage.read()
    taken_at = None
    image_format = PHOTO_FORMATS.get(os.path.splitext(full_path)[1].lower())
    if image_format:
        try:
            with Image.open(BytesIO(data)) as image:
                taken_at = photo_taken_at(image)
                # Phones store rotation as an EXIF tag that re-saving drops, so apply it first.
                photo = ImageOps.exif_transpose(image)
                photo.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
                if image_format == 'JPEG' and photo.mode not in ('RGB', 'L'):
                    photo = photo.convert('RGB')
                options = {'optimize': True} if image_format == 'PNG' else {'quality': PHOTO_QUALITY, 'optimize': True}
                # Saved without exif=..., so no metadata is copied into the stored file;
                # kept even when a tiny image comes out a few bytes larger.
                photo.save(full_path, image_format, **options)
            return taken_at
        except (OSError, ValueError, Image.DecompressionBombError):
            pass  # not a readable image: keep the file exactly as uploaded
    with open(full_path, 'wb') as stream:
        stream.write(data)
    return taken_at


def save_document_upload(file_storage, doc_type, doc, user_id):
    """Store one uploaded evidence file and return its metadata dict."""
    rel_folder, filename = resolve_document_upload_path(
        doc_type=doc_type,
        doc=doc,
        user_id=user_id,
        original_filename=file_storage.filename,
        now=wib_now(),
    )
    upload_folder = os.path.join(current_app.config['UPLOAD_FOLDER'], rel_folder)
    os.makedirs(upload_folder, exist_ok=True)
    full_path = os.path.join(upload_folder, filename)
    taken_at = save_upload_file(file_storage, full_path)
    return {
        'stored_path': f"{rel_folder.rstrip('/')}/{filename}".lstrip('/'),
        'original_filename': (secure_filename(file_storage.filename) or 'photo')[:255],
        'file_size': os.path.getsize(full_path),
        'taken_at': taken_at,
    }


def remove_upload_files(records):
    """Delete stored files, never leaving UPLOAD_FOLDER. Accepts paths or metadata dicts."""
    upload_root = os.path.abspath(current_app.config['UPLOAD_FOLDER'])
    for record in records:
        stored_path = record['stored_path'] if isinstance(record, dict) else record
        if not stored_path:
            continue
        candidate = os.path.abspath(os.path.join(upload_root, stored_path))
        if os.path.commonpath([upload_root, candidate]) != upload_root:
            continue
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError:
            current_app.logger.warning('Could not remove upload file: %s', candidate)
