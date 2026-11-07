"""Checkout validation and portable merchant QR images."""
import base64
import mimetypes
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote


def validate_payment(method, reference=''):
    method = str(method or '').strip()
    name = method.lower()
    if name not in ('gcash', 'gcash online', 'gcash online payment', 'cash on pickup', 'cash on pick-up', 'cash pickup'):
        raise ValueError('Choose GCash or Cash on Pickup.')
    if 'gcash' in name:
        reference = str(reference or '').strip()
        if not re.fullmatch(r'[0-9]{8,16}', reference):
            raise ValueError('Ilagay ang wastong GCash Reference Number (8–16 digits, karaniwang 13).')
        return method, reference
    return method, ''


def validate_merchant(settings, expected_account=''):
    account = str(settings.get('accountNumber') or '').strip()
    if settings.get('isActive') is False or not account:
        raise ValueError('GCash is unavailable. Please choose Cash on Pickup.')
    if expected_account and expected_account != account:
        raise ValueError('GCash merchant details changed. Refresh and check the current payment details.')
    return account


def validate_receipt(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if len(value) > 700000:
        raise ValueError('Receipt image is too large. Choose a smaller PNG or JPG image.')
    if value.startswith('data:'):
        if not re.match(r'^data:image/(png|jpeg|webp);base64,', value):
            raise ValueError('Receipt must be a PNG or JPG image.')
        try:
            image = base64.b64decode(value.split(',', 1)[1], validate=True)
        except (ValueError, TypeError):
            raise ValueError('Receipt image is invalid.') from None
        if not image or len(image) > 512 * 1024:
            raise ValueError('Receipt image is invalid or too large.')
        return value
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.netloc:
        raise ValueError('Receipt link is invalid.')
    return value


def shared_qr_settings(settings, image_directory):
    result = dict(settings)
    source = str(result.get('qrUrl') or '')
    result.update(qrImageData='', qrImageSource=source)
    url = urlsplit(source)
    if url.scheme or url.netloc or not url.path.startswith('/static/images/'):
        return result
    directory = Path(image_directory).resolve()
    image = (directory / unquote(url.path.removeprefix('/static/images/'))).resolve()
    if not image.is_relative_to(directory) or not image.is_file():
        return result
    mime = mimetypes.guess_type(image.name)[0]
    if mime not in ('image/png', 'image/jpeg', 'image/webp') or image.stat().st_size > 512 * 1024:
        return result
    result['qrImageData'] = f'data:{mime};base64,' + base64.b64encode(image.read_bytes()).decode('ascii')
    return result
