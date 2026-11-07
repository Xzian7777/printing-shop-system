"""Money and loyalty validation shared by HTTP and transaction entry points."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def whole(value, label, minimum=0, maximum=1000000):
    text = str(value)
    if not text.isascii() or not text.isdigit() or not minimum <= int(text) <= maximum:
        raise ValueError(f'{label} must be a whole number from {minimum} to {maximum}.')
    return int(text)


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 5000000:
            raise ValueError('Invalid amount.')
        return int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError):
        raise ValueError('Invalid amount.') from None


def account_values(data):
    balance = whole(data.get('balance', 0), 'Balance', maximum=9007199254740991)
    reserved = whole(data.get('reserved', 0), 'Reserved points', maximum=balance)
    return balance, reserved


def discount_quote(subtotal, points, available, rate):
    points = whole(points, 'Points', maximum=9007199254740991)
    rate = whole(rate, 'Point value in centavos', minimum=1, maximum=100000)
    limit = min(available, subtotal // (2 * rate))
    if points > limit:
        raise ValueError(f'Use at most {limit} points. Discounts are limited to 50% of the subtotal.')
    return {'pointsUsed': points, 'pointValueCents': rate,
            'subtotal': subtotal / 100, 'pointsDiscount': points * rate / 100,
            'total': (subtotal - points * rate) / 100}


def service_total(service_id, base_price, payload):
    base = money(base_price)
    qty = whole(payload.get('quantity', payload.get('copies', 1)), 'Quantity', 1, 100000)
    pages = whole(payload.get('pages', 1), 'Pages', 1, 5000)
    details = payload.get('details') or {}
    if service_id == 'tarpaulins':
        try:
            dimensions = str(details.get('dimensions', '')).lower().replace('ft', '').split('x')
            width = Decimal(str(payload.get('width') or dimensions[0]).strip())
            height = Decimal(str(payload.get('height') or dimensions[1]).strip())
            if not width.is_finite() or not height.is_finite() or not 0 < width <= 100 or not 0 < height <= 100:
                raise ValueError('Dimensions must be greater than zero and at most 100 feet.')
            result = Decimal(base) * width * height * qty
        except (IndexError, InvalidOperation):
            raise ValueError('Provide valid width and height.') from None
    elif service_id == 'booklets':
        result = Decimal(base) * qty * pages / 4
    elif service_id == 'photoCanvas':
        size = str(details.get('size') or '')
        unit = 55000 if '550' in size else 25000 if '250' in size or '12 x 18' in size else base
        result = Decimal(unit) * qty
    elif service_id in ['businessCards', 'flyers', 'stickers', 'invitations', 'shirt', 'mug']:
        result = Decimal(base) * qty
    else:
        stock = payload.get('paperStock', 'short')
        if stock not in ['short', 'long', 'a4']:
            raise ValueError('Invalid paper stock.')
        result = Decimal(base) * pages * qty * Decimal({'short':'1', 'long':'1.2', 'a4':'1.1'}[stock])
    value = int(result.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if not 0 < value <= 500000000:
        raise ValueError('The order total must be greater than zero and at most 5,000,000.')
    return value
