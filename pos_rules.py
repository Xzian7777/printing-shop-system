"""Counter sale validation and cent-based arithmetic shared by POS entry points."""
import math
import re


def cents(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError('Enter a valid amount.')
    if isinstance(value, bool) or not math.isfinite(number) or not 0 <= number <= 5000000:
        raise ValueError('Amounts must be between 0 and 5,000,000.')
    return math.floor(number * 100 + 0.50000001)


def validate_sale(request, catalog, discount_cents=0, allow_points=False):
    name = str(request.get('buyerName') or '').strip() or 'Walk-in Customer'
    if len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Buyer name must be at most 100 characters.')
    raw_lines = request.get('items')
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 50:
        raise ValueError('Add between 1 and 50 items.')
    lines, seen = [], set()
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError('Invalid POS item.')
        kind, key = raw.get('kind'), str(raw.get('id') or '')
        if kind not in ['product', 'service'] or not key or '/' in key or len(key) > 100:
            raise ValueError('Invalid POS item.')
        identity = (kind, key)
        if identity in seen:
            raise ValueError('Combine repeated items into one line.')
        seen.add(identity)
        product = catalog.get(identity)
        if not product or product.get('archived') or product.get('isArchived'):
            raise ValueError('An item was removed or archived. Refresh the catalog.')
        text = str(raw.get('qty', ''))
        if not re.fullmatch(r'[0-9]{1,6}', text) or not 1 <= int(text) <= 100000:
            raise ValueError('Quantity must be a whole number from 1 to 100,000.')
        qty = int(text)
        price = cents(product.get('price', 0)) if kind == 'product' else cents(raw.get('unitPrice'))
        if price <= 0:
            raise ValueError('Unit price must be greater than zero.')
        if kind == 'product':
            if cents(raw.get('unitPrice')) != price:
                raise ValueError('A product price changed. Refresh the catalog before continuing.')
            if qty > int(product.get('stock') or 0):
                raise ValueError(f"Insufficient stock for {product.get('name', key)}.")
        note = str(raw.get('note') or '').strip()
        if len(note) > 500:
            raise ValueError('Service details must be at most 500 characters.')
        lines.append({'kind': kind, 'id': key, 'name': str(product.get('name') or key),
                      'qty': qty, 'price': price / 100, 'unitPrice': price / 100,
                      'lineTotal': price * qty / 100, 'note': note,
                      'pricingSource': 'catalog' if kind == 'product' else 'counter_quote'})
    total = sum(cents(line['price']) * line['qty'] for line in lines)
    if not 0 < total <= 500000000 or cents(request.get('expectedTotal')) != total:
        raise ValueError('The sale total changed or exceeds 5,000,000. Review your items.')
    subtotal = total
    if not isinstance(discount_cents, int) or not 0 <= discount_cents <= total:
        raise ValueError('Invalid points discount.')
    total -= discount_cents
    payment = request.get('payment')
    if payment not in (['Points'] if allow_points and total == 0 else ['Cash', 'GCash']):
        raise ValueError('Choose Cash or GCash.')
    reference = str(request.get('gcashRef') or '').strip()
    if payment == 'GCash' and (not re.fullmatch(r'[0-9]{8,16}', reference) or request.get('paymentConfirmed') is not True):
        raise ValueError('Enter the GCash reference and confirm payment was received.')
    tendered = cents(request.get('cashReceived')) if payment == 'Cash' else total
    if tendered < total:
        raise ValueError('Cash received must cover the total.')
    return {'buyerName': name, 'items': lines, 'total': total / 100, 'subtotal': subtotal / 100, 'payment': payment,
            'cashReceived': tendered / 100, 'change': (tendered - total) / 100,
            'gcashRef': reference if payment == 'GCash' else '',
            'productTotal': sum(cents(x['price']) * x['qty'] for x in lines if x['kind'] == 'product') / 100,
            'serviceTotal': sum(cents(x['price']) * x['qty'] for x in lines if x['kind'] == 'service') / 100}


def order_record(sale, order_id, cashier, submission_id, timestamp):
    products = [x for x in sale['items'] if x['kind'] == 'product']
    services = [x for x in sale['items'] if x['kind'] == 'service']
    summary = ', '.join(f"{x['name']} ({x['qty']}x)" for x in sale['items'])
    return {**sale, 'id': order_id, 'submissionId': submission_id, 'source': 'POS',
            'customerName': sale['buyerName'], 'customer': sale['buyerName'],
            'service': ('In-Store: ' if not services else 'POS: ') + summary,
            'paperSize': 'Store Supply' if not services else 'Counter Service',
            'copies': sum(x['qty'] for x in sale['items']), 'pages': 0,
            'status': 'Completed', 'stockDeducted': bool(products),
            'paymentStatus': 'Paid at Counter', 'cashier': cashier,
            'createdAt': timestamp, 'timestamp': timestamp}
