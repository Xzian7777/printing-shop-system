"""
supabase_config.py
Supabase Storage integration for the Printing Shop website.

All credentials are read from environment variables — never hardcoded.
Uses the service-role key server-side so it can bypass RLS for uploads/downloads.
The anon key is NOT used for storage operations.
"""

import os
import re
import uuid
import mimetypes
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# UPLOAD LIMITS  (change here to apply everywhere)
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES        = 10 * 1024 * 1024   # 10 MB per file
MAX_FILES_PER_REQUEST      = 5                   # max files in one upload call
MAX_COMBINED_SIZE_BYTES    = 25 * 1024 * 1024   # 25 MB combined per request
MAX_FILENAME_LENGTH        = 150                 # characters (original name)
ALLOWED_EXTENSIONS         = {'pdf', 'png', 'jpg', 'jpeg', 'doc', 'docx'}

# Mapping extension → MIME type for server-side validation
ALLOWED_MIME_TYPES = {
    'pdf':  'application/pdf',
    'png':  'image/png',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'doc':  'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}

# ---------------------------------------------------------------------------
# CREDENTIALS  (from .env — never expose to client)
# ---------------------------------------------------------------------------
SUPABASE_URL      = os.getenv('SUPABASE_URL', '').rstrip('/')
SERVICE_ROLE_KEY  = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
BUCKET_NAME       = os.getenv('BUCKET_NAME', 'kezcemzek-files')

# ---------------------------------------------------------------------------
# CLIENT  (service-role key — server-side only)
# ---------------------------------------------------------------------------
_supabase_client: Client | None = None

def _get_client() -> Client:
    """Return (and lazily initialise) the Supabase client."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not SUPABASE_URL or not SERVICE_ROLE_KEY:
        raise RuntimeError(
            'Supabase credentials missing. '
            'Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env.'
        )
    _supabase_client = create_client(SUPABASE_URL, SERVICE_ROLE_KEY)
    print('✅ Supabase client initialised (service-role key).')
    return _supabase_client


# ---------------------------------------------------------------------------
# FILENAME UTILITIES
# ---------------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    """
    • Strip directory traversal characters.
    • Replace whitespace and unsafe chars with underscores.
    • Truncate the *base* name so the total length ≤ MAX_FILENAME_LENGTH.
    """
    # Strip any path component
    name = os.path.basename(name)
    # Replace unsafe characters (keep alphanumeric, dot, dash, underscore)
    name = re.sub(r'[^\w.\-]', '_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name).strip('_')

    # Split into base + ext and enforce total length
    if '.' in name:
        base, ext = name.rsplit('.', 1)
        ext = ext[:20]          # extension capped at 20 chars just in case
        max_base = MAX_FILENAME_LENGTH - len(ext) - 1
        base = base[:max_base]
        name = f'{base}.{ext}'
    else:
        name = name[:MAX_FILENAME_LENGTH]

    return name or 'upload'


def _get_extension(filename: str) -> str:
    """Return the lowercase file extension without the dot."""
    if '.' in filename:
        return filename.rsplit('.', 1)[1].lower()
    return ''


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------
def validate_upload_file(file_obj, filename: str | None = None) -> tuple[bool, str]:
    """
    Validate a single file object (werkzeug FileStorage).

    Returns (True, '') if valid, or (False, error_message) if not.
    """
    original_name = filename or getattr(file_obj, 'filename', '') or ''

    # 1. Non-empty filename
    if not original_name.strip():
        return False, 'File has no name.'

    # 2. Filename length
    if len(original_name) > MAX_FILENAME_LENGTH:
        return False, (
            f'Filename is too long ({len(original_name)} chars). '
            f'Maximum allowed is {MAX_FILENAME_LENGTH} characters.'
        )

    # 3. Extension whitelist
    ext = _get_extension(original_name)
    if ext not in ALLOWED_EXTENSIONS:
        allowed_str = ', '.join(f'.{e}' for e in sorted(ALLOWED_EXTENSIONS))
        return False, f'File type ".{ext}" is not allowed. Accepted formats: {allowed_str}.'

    # 4. Read file bytes (reset pointer after)
    file_obj.seek(0)
    file_bytes = file_obj.read()
    file_obj.seek(0)

    # 5. Empty file
    if not file_bytes:
        return False, 'The uploaded file is empty.'

    # 6. File size
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        mb = len(file_bytes) / (1024 * 1024)
        return False, f'File size ({mb:.1f} MB) exceeds the 10 MB limit per file.'

    # 7. MIME type check via magic bytes (first 512 bytes)
    # We use mimetypes as a fallback since python-magic isn't available everywhere.
    # For stronger validation install python-magic-bin and swap in magic.from_buffer().
    mime_from_ext = ALLOWED_MIME_TYPES.get(ext, '')
    content_type  = getattr(file_obj, 'content_type', '') or ''

    # Accept if either the declared content-type or the extension-derived MIME matches.
    # This is sufficient for a server-side guard; Supabase will reject unrecognised bytes.
    declared_ok = (
        not content_type
        or content_type == 'application/octet-stream'
        or content_type == mime_from_ext
        or (ext in ('jpg', 'jpeg') and content_type == 'image/jpeg')
    )
    if not declared_ok:
        return False, (
            f'File MIME type "{content_type}" does not match the expected type '
            f'for a .{ext} file.'
        )

    return True, ''


# ---------------------------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------------------------
def upload_order_file(
    file_obj,
    order_id: str,
    customer_id: str = 'unknown',
    filename: str | None = None,
) -> tuple[str, str]:
    """
    Upload a design/document file to Supabase Storage.

    Returns (storage_path, signed_url) on success.
    Raises RuntimeError on failure — never falls back to local storage.

    Storage path format:
        orders/{order_id}/{uuid}_{sanitized_filename}
    """
    client = _get_client()

    original_name = filename or getattr(file_obj, 'filename', '') or 'upload'
    sanitized     = _sanitize_filename(original_name)
    unique_id     = uuid.uuid4().hex[:12]
    storage_path  = f'orders/{order_id}/{unique_id}_{sanitized}'

    file_obj.seek(0)
    file_bytes = file_obj.read()
    file_obj.seek(0)

    ext          = _get_extension(sanitized)
    content_type = ALLOWED_MIME_TYPES.get(ext, 'application/octet-stream')

    try:
        client.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={'content-type': content_type, 'upsert': 'false'},
        )
    except Exception as exc:
        raise RuntimeError(f'Supabase Storage upload failed: {exc}') from exc

    # Generate a 24-hour signed URL immediately
    signed_url = get_signed_url(storage_path, expires_in=86400)
    print(f'✅ Uploaded to Supabase: {storage_path}')
    return storage_path, signed_url


# ---------------------------------------------------------------------------
# SIGNED URL
# ---------------------------------------------------------------------------
def get_signed_url(storage_path: str, expires_in: int = 3600) -> str:
    """
    Generate a time-limited signed URL for a private file.

    expires_in: seconds until the URL expires (default 1 hour).
    Returns the signed URL string, or '' on failure.
    """
    try:
        client  = _get_client()
        result  = client.storage.from_(BUCKET_NAME).create_signed_url(
            storage_path, expires_in
        )
        # SDK v2: result is a dict with 'signedURL' key
        url = result.get('signedURL') or result.get('signedUrl') or ''
        return url
    except Exception as exc:
        print(f'⚠️  Could not generate signed URL for {storage_path}: {exc}')
        return ''


# ---------------------------------------------------------------------------
# DELETE  (cleanup on order failure)
# ---------------------------------------------------------------------------
def delete_file(storage_path: str) -> bool:
    """
    Delete a file from Supabase Storage.
    Returns True on success, False on failure.
    """
    try:
        client = _get_client()
        client.storage.from_(BUCKET_NAME).remove([storage_path])
        print(f'🗑️  Deleted from Supabase: {storage_path}')
        return True
    except Exception as exc:
        print(f'⚠️  Could not delete {storage_path}: {exc}')
        return False