import argparse
import base64
import datetime
import hashlib
import logging
import math
import mimetypes
import os
import random
import re
import secrets
import smtplib
import string
import threading
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

import requests
from dotenv import load_dotenv
from firebase_admin import auth, firestore, messaging
from firebase_admin import auth as firebase_auth
from flask import Blueprint, Flask, jsonify, redirect, render_template, request, session, url_for

from firebase_config import db
import supabase_config as sc


# ===========================================================================
# CATALOG RULES
# ===========================================================================

def validate_service(data):
    name = str(data.get('name') or '').strip()
    key = str(data.get('id') or '').strip()
    if not name or len(name) > 500 or not key or '/' in key or len(key) > 100:
        raise ValueError('Provide a service name and a valid ID (max 100 characters, without /).')
    price = float(data.get('basePrice', 0))
    sort = int(data.get('sortOrder', 1))
    if not math.isfinite(price) or not 0 <= price <= 5000000 or not 0 <= sort <= 100000:
        raise ValueError('Invalid base price or display order.')
    requirements, labels = [], set()
    for raw in data.get('requirements') or []:
        label = str(raw.get('label') or '').strip()
        kind = raw.get('type', 'text')
        if not label or len(label) > 150 or label in labels or kind not in ('text', 'number', 'select', 'radio', 'checkboxes'):
            raise ValueError('Requirements must have unique labels and supported types.')
        labels.add(label)
        options = raw.get('options') or []
        if isinstance(options, str): options = options.replace(',', '\n').splitlines()
        options = list(dict.fromkeys(str(option).strip() for option in options if str(option).strip()))
        if kind in ('select', 'radio', 'checkboxes') and not options:
            raise ValueError(f'Provide choices for {label}.')
        requirements.append(dict(raw, label=label, type=kind, options=options, required=raw.get('required') is not False))
    return dict(data, id=key, name=name, basePrice=price, sortOrder=sort, requirements=requirements)


def validate_requirements(service, supplied):
    if not isinstance(supplied, dict): raise ValueError('Invalid service requirements.')
    for req in service.get('requirements') or []:
        label = str(req.get('label') or req.get('name') or '')
        value = supplied.get(label)
        if req.get('required') is not False and (value is None or value == [] or not str(value).strip()):
            raise ValueError(f'Please provide: {label}')
        if value is None or value == '' or value == []: continue
        if req.get('type') == 'number':
            text = str(value)
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 100000:
                raise ValueError(f'{label} must be a whole number from 1 to 100,000.')
        elif req.get('type') in ('select', 'radio', 'checkboxes'):
            options = req.get('options') or []
            if isinstance(options, str): options = [o.strip() for o in options.replace('/', ',').split(',')]
            values = value if isinstance(value, list) else [value]
            if any(v not in options for v in values): raise ValueError(f'Invalid choice for {label}.')
        elif len(str(value)) > 150:
            raise ValueError(f'{label} cannot exceed 150 characters.')


# ===========================================================================
# INVENTORY RULES
# ===========================================================================

def order_items(order):
    items = order.get('items') or []
    if items:
        return [dict(item, id=item.get('productId') or item.get('id'),
                     qty=int(item.get('qty') or item.get('quantity') or 1)) for item in items]
    service = str(order.get('service') or '')
    if not service.lower().startswith('in-store:'):
        return []
    return [{'name': name.strip(), 'qty': int(qty)}
            for name, qty in re.findall(r'([^,]+?)\s*\((\d+)x\)', service.split(':', 1)[1])]


def stock_plan(order, products, restore=False):
    """Resolve IDs first, exact legacy names second; aggregate repeated lines."""
    quantities = {}
    for item in order_items(order):
        item_id = str(item.get('id') or '')
        name = str(item.get('name') or '').strip().lower()
        matches = [(key, data) for key, data in products.items()
                   if (item_id and key == item_id) or
                   (not item_id and str(data.get('name') or '').strip().lower() == name)]
        if len(matches) != 1:
            raise ValueError(f"Product not found or ambiguous: {item.get('name') or item_id}")
        key, product = matches[0]
        if product.get('archived') is True and not restore:
            raise ValueError(f"Product is archived: {name}")
        qty = int(item['qty'])
        if qty <= 0:
            raise ValueError('Item quantity must be positive.')
        quantities[key] = quantities.get(key, 0) + qty
    plan = {}
    for key, qty in quantities.items():
        current = int(products[key].get('stock') or 0)
        if not restore and current < qty:
            raise ValueError(f"Insufficient stock: {products[key].get('name', key)}")
        plan[key] = current + qty if restore else current - qty
    return plan


def valid_transition(current, target):
    current = str(current or 'Order Submitted')
    if current == 'Cancelled':
        return False
    if target.startswith('Rejected'):
        return current not in ('Completed',) and not current.startswith('Rejected')
    return target in {
        'Order Submitted': ['Approved'], 'For Approval': ['Approved'],
        'Approved': ['Printing', 'Preparing'], 'Printing': ['Ready for Pickup'],
        'Preparing': ['Ready for Pickup'], 'Ready for Pickup': ['Completed'],
    }.get(current, [])


# ===========================================================================
# PAYMENT RULES
# ===========================================================================

def validate_payment(method, reference=''):
    method = str(method or '').strip()
    name = method.lower()
    if name not in ('gcash', 'gcash online', 'gcash online payment', 'cash on pickup', 'cash on pick-up', 'cash pickup'):
        raise ValueError('Choose GCash or Cash on Pickup.')
    if 'gcash' in name:
        reference = str(reference or '').strip()
        if not re.fullmatch(r'[0-9]{8,16}', reference):
            raise ValueError('Enter a valid GCash reference number (8–16 digits, usually 13).')
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


# ===========================================================================
# LOYALTY RULES
# ===========================================================================

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


# ===========================================================================
# POS RULES
# ===========================================================================

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


# ===========================================================================
# ORDER CANCELLATION
# ===========================================================================

def can_cancel_order(order):
    return (order.get('source') != 'POS' and
            order.get('status', 'Order Submitted') in ['Order Submitted', 'For Approval'] and
            order.get('approvedAt') is None and order.get('stockDeducted', False) is False)


def cancel_customer_order(db, order_id, uid, email, reason=''):
    if not isinstance(reason, str) or len(reason.strip()) > 500:
        raise TypeError('Cancellation reason must be at most 500 characters.')
    if not order_id or '/' in order_id:
        raise FileNotFoundError('Order not found.')
    ref = db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('orders').document(order_id)
    clean_email = str(email or '').strip().lower()

    @firestore.transactional
    def apply(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise FileNotFoundError('Order not found.')
        order = snapshot.to_dict()
        owner_email = str(order.get('email') or order.get('customerEmail') or '').strip().lower()
        owner_uid = order.get('customerId') or order.get('uid')
        if not ((uid and owner_uid == uid) or (clean_email and owner_email == clean_email)):
            raise PermissionError('You can only cancel your own order.')
        if order.get('status') == 'Cancelled':
            return {'id': order_id, 'status': 'Cancelled', 'cancellationReason': order.get('cancellationReason', '')}
        if not can_cancel_order(order):
            raise ValueError('This order cannot be cancelled because it has been approved or processing has started.')
        transaction.update(ref, {
            'status': 'Cancelled', 'cancelledAt': firestore.SERVER_TIMESTAMP,
            'cancelledBy': uid or clean_email, 'cancelledByEmail': clean_email,
            'cancellationReason': reason.strip(),
        })
        return {'id': order_id, 'status': 'Cancelled', 'cancellationReason': reason.strip()}

    return apply(db.transaction())


# ===========================================================================
# FIREBASE CONFIG
# ===========================================================================

# ---------------------------------------------------------------------------
# FIRESTORE ORDERS SUB-COLLECTION REFERENCE
# ---------------------------------------------------------------------------
def get_orders_ref():
    if db is None:
        return None
    return db.collection('artifacts') \
             .document('printcraft-pro') \
             .collection('public') \
             .document('data') \
             .collection('orders')

def get_orders():
    try:
        ref = get_orders_ref()
        if ref is None:
            return []
        docs = ref.get()
        orders = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            orders.append(data)
        orders.sort(key=lambda x: str(x.get('createdAt') or x.get('timestamp') or x.get('orderDate') or ''))
        return orders
    except Exception as e:
        print(f"Error fetching orders from Firestore: {e}")
        return []

def create_order(order_data):
    try:
        ref = get_orders_ref()
        if ref is None:
            return False
        doc_id = order_data.get('id')
        ref.document(doc_id).set(order_data)

        # Notify Admin about new incoming order
        try:
            cust_name = order_data.get('customerName') or order_data.get('customer') or 'Customer'
            svc_name = order_data.get('service') or 'Print Job'
            notif_id = f"notif_{int(datetime.datetime.now().timestamp()*1000)}"
            create_notification({
                'id': notif_id,
                'userId': 'ADMIN',
                'type': 'new_order', 'orderId': doc_id,
                'createdAt': firestore.SERVER_TIMESTAMP,
                'title': f'New Order: {doc_id}',
                'message': f'{cust_name} placed an order for {svc_name}.',
                'status': 'Unread',
                'link': '/admin',
                'timestamp': datetime.datetime.now().isoformat()
            })
            send_fcm_push_notification(
                recipient_role='admin',
                recipient_email=None,
                title=f'New Order: {doc_id}',
                body=f'{cust_name} submitted a new {svc_name} order.',
                click_url='/admin'
            )
        except Exception as e_ord_notif:
            print(f"Non-fatal order notification error: {e_ord_notif}")

        return True
    except Exception as e:
        print(f"Error creating order: {e}")
        return False

def update_order_status(order_id, new_status):
    try:
        ref = get_orders_ref()
        if ref is None:
            return False

        order_ref = ref.document(order_id)
        order_doc = order_ref.get()

        if not order_doc.exists:
            print(f"Order {order_id} not found.")
            return False

        order_data = order_doc.to_dict()
        @firestore.transactional
        def apply_status(transaction):
            snapshot = order_ref.get(transaction=transaction)
            current = snapshot.to_dict() or {}
            if not valid_transition(current.get('status'), new_status):
                raise ValueError('Invalid status transition')
            deduct = new_status == 'Approved' and not current.get('stockDeducted')
            restore = new_status.startswith('Rejected') and current.get('stockDeducted') is True
            plan = {}
            if deduct or restore:
                products = {d.id: d.to_dict() for d in _legacy_products().stream(transaction=transaction)}
                products.update({d.id: d.to_dict() for d in db.collection('products').stream(transaction=transaction)})
                plan = stock_plan(current, products, restore=restore)
            for product_id, stock in plan.items():
                transaction.set(db.collection('products').document(product_id), {**products[product_id], 'stock': stock}, merge=True)
                transaction.set(_legacy_products().document(product_id), {**products[product_id], 'stock': stock}, merge=True)
            updates = {'status': new_status}
            if new_status == 'Approved':
                updates['approvedAt'] = firestore.SERVER_TIMESTAMP
            if deduct or restore:
                updates['stockDeducted'] = bool(deduct)
            transaction.update(order_ref, updates)
            transaction.set(db.collection('push_jobs').document(), {
                'type': 'order_status', 'orderId': order_id,
                'status': new_status, 'state': 'pending',
                'createdAt': firestore.SERVER_TIMESTAMP,
            })
        apply_status(db.transaction())

        # Notify Customer about status change
        try:
            c_email = order_data.get('customerEmail') or order_data.get('email')
            c_uid = order_data.get('customerId') or order_data.get('uid') or c_email
            svc = order_data.get('service') or 'Print Order'
            notif_id = f"notif_{int(datetime.datetime.now().timestamp()*1000)}"
            create_notification({
                'id': notif_id,
                'userId': c_uid or c_email,
                'recipientEmail': str(c_email or '').strip().lower(),
                'type': 'order_status', 'orderId': order_id,
                'createdAt': firestore.SERVER_TIMESTAMP,
                'title': f'Order Status: {new_status}',
                'message': f'Your {svc} order ({order_id}) is now {new_status}.',
                'status': 'Unread',
                'link': '/customer?tab=myOrders',
                'timestamp': datetime.datetime.now().isoformat()
            })
        except Exception as e_status_notif:
            print(f"Non-fatal status update notification error: {e_status_notif}")

        return True
    except Exception as e:
        print(f"Error updating order status: {e}")
        return False

# ---------------------------------------------------------------------------
# OTHER STORE & USER HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def get_customers():
    try:
        if db is None: return []
        profiles = {}
        for collection in ['users', 'customers']:
            for doc in db.collection(collection).stream():
                data = doc.to_dict() or {}
                key = str(data.get('email') or f'{collection}/{doc.id}').strip().lower()
                old = profiles.get(key, {})
                archived = old.get('archived') or old.get('isArchived') or data.get('archived') or data.get('isArchived') or False
                profiles[key] = {**old, **data, 'id': doc.id, 'archived': archived, 'isArchived': archived}
        balances = {doc.id: doc.to_dict() for doc in db.collection('loyalty_accounts').stream()}
        for profile in profiles.values():
            balance = balances.get(profile.get('loyaltyUid') or profile.get('uid') or profile.get('id'))
            if balance:
                profile['points'] = int(balance.get('balance', 0)) - int(balance.get('reserved', 0))
                profile['loyaltyPoints'] = profile['points']
                profile['reservedPoints'] = int(balance.get('reserved', 0))
        return [p for p in profiles.values() if not p.get('archived') and not p.get('isArchived')]
    except Exception as e:
        print(f'Error fetching customers: {e}')
        return []


def create_customer(customer_data):
    try:
        sync_customer_profile(customer_data['id'], customer_data)
        return True
    except Exception as e:
        print(f"Error creating customer: {e}")
        return False


def create_firebase_customer(email, password):
    try:
        user = auth.create_user(
            email=email,
            password=password
        )
        return user.uid
    except auth.EmailAlreadyExistsError:
        return None
    except Exception as e:
        print(f"Error creating Firebase Auth user: {e}")
        return None


def delete_firebase_customer(uid):
    try:
        auth.delete_user(uid)
        return True
    except Exception as e:
        print(f"Error deleting Firebase Auth user: {e}")
        return False

def update_firebase_password(uid, new_password):
    try:
        auth.update_user(uid, password=new_password)
        return True
    except Exception as e:
        print(f"Error updating Firebase Auth password: {e}")
        return False

def get_firebase_user_by_email(email):
    try:
        return auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        return None
    except Exception as e:
        print(f"Error getting Firebase Auth user by email: {e}")
        return None

def get_customer_profile(username):
    try:
        doc = db.collection('customers').document(username).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None

def save_customer_profile(username, profile_data):
    try:
        db.collection('customers').document(username).set(profile_data, merge=True)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# STORE PRODUCTS MANAGEMENT (FIRESTORE DOCUMENTS SYNC)
# ---------------------------------------------------------------------------


def get_store_products():
    try:
        if db is None: return []
        records = {d.id: d.to_dict() for d in _legacy_products().stream()}
        records.update({d.id: d.to_dict() for d in db.collection('products').stream()})
        return [dict(data, id=key) for key, data in records.items() if data.get('archived') is not True]
    except Exception as e:
        print(f'Error fetching store products: {e}')
        raise


def save_store_products(products_list):
    try:
        if db is None: return False
        @firestore.transactional
        def apply(transaction):
            pending = []
            for product in products_list:
                product_id = str(product.get('id') or uuid.uuid4().hex)
                ref = db.collection('products').document(product_id)
                current = ref.get(transaction=transaction)
                if not current.exists: current = _legacy_products().document(product_id).get(transaction=transaction)
                archived = db.collection('archives').document('store_product_' + product_id).get(transaction=transaction)
                if archived.exists or (current.exists and current.to_dict().get('archived') is True):
                    raise ValueError('Product has been archived. Refresh inventory.')
                payload = dict(product, id=product_id)
                expected_stock = payload.pop('expectedStock', None)
                if expected_stock is not None and (not current.exists or current.to_dict().get('stock') != expected_stock):
                    raise ValueError('Stock changed. Refresh inventory.')
                delta = payload.pop('stockDelta', None)
                if delta is not None:
                    if not current.exists: raise ValueError('Product not found.')
                    payload['stock'] = int(current.to_dict().get('stock') or 0) + int(delta)
                stock = int(payload.get('stock') or 0)
                price = float(payload.get('price') or 0)
                if stock < 0 or stock > 100000 or not (0 <= price <= 5000000):
                    raise ValueError('Invalid stock or price.')
                if 'stock' in payload: payload['stock'] = stock
                if 'price' in payload: payload['price'] = price
                pending.append((ref, product_id, payload))
            for ref, product_id, payload in pending:
                transaction.set(ref, payload, merge=True)
                transaction.set(_legacy_products().document(product_id), payload, merge=True)
        apply(db.transaction())
        return True
    except Exception as e:
        print(f'Error saving products: {e}')
        return False


def delete_store_product(prod_id):
    try:
        if db is not None:
            str_id = str(prod_id)
            db.collection('products').document(str_id).delete()

            db.collection('artifacts') \
              .document('printcraft-pro') \
              .collection('public') \
              .document('data') \
              .collection('store_products') \
              .document(str_id).delete()
            print(f"✅ Product {str_id} deleted permanently from Firestore!")
            return True
        return False
    except Exception as e:
        print(f"Error deleting product: {e}")
        return False

def get_inventory():
    return get_store_products()

# ---------------------------------------------------------------------------
# SAMPLES & SERVICES SHOWCASE (SAMPLE WORKS CATALOG)
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_PRODUCTS = [
    {
        "id": "sample-1",
        "title": "Document & Thesis Bookbinding",
        "category": "Standard Printing",
        "serviceVal": "standardA4",
        "price": "Starts at ₱3.00 / page",
        "badge": "Best Seller",
        "imageUrl": "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?auto=format&fit=crop&w=600&q=80",
        "description": "High-definition monochrome & full-color document printing with 80 GSM smooth bond paper. Available for softbound, hardbound, and ring bind options."
    },
    {
        "id": "sample-2",
        "title": "Premium Waterproof PVC IDs & Lanyards",
        "category": "ID & Badges",
        "serviceVal": "photoCanvas",
        "price": "₱50.00 / card",
        "badge": "Heavy Duty",
        "imageUrl": "https://images.unsplash.com/photo-1589829085413-56de8ae18c73?auto=format&fit=crop&w=600&q=80",
        "description": "Thick ATM-type PVC cards, fade-proof digital pigment ink, vibrant color reproduction for school, company IDs, and visitor badges."
    },
    {
        "id": "sample-3",
        "title": "Glossy Die-Cut Vinyl Stickers",
        "category": "Stickers & Labels",
        "serviceVal": "stickers",
        "price": "Starts at ₱5.00 / pc",
        "badge": "Waterproof",
        "imageUrl": "https://images.unsplash.com/photo-1572375992501-4b0892d50c69?auto=format&fit=crop&w=600&q=80",
        "description": "Machine-cut kiss-cut or die-cut vinyl stickers with matte or glossy cold laminate coating. Waterproof, tear-resistant, and scratch-proof."
    },
    {
        "id": "sample-4",
        "title": "Event Tarpaulin & Billboard Banners",
        "category": "Large Format",
        "serviceVal": "tarpaulins",
        "price": "₱15.00 / sq.ft",
        "badge": "High Demand",
        "imageUrl": "https://images.unsplash.com/photo-1563986768609-322da13575f3?auto=format&fit=crop&w=600&q=80",
        "description": "Heavy-duty 13oz high-gloss tarpaulin banner printing with rust-resistant metal eyelets. Perfect for birthdays, grand openings, and announcements."
    },
    {
        "id": "sample-5",
        "title": "Corporate Matte Business Cards",
        "category": "Corporate Branding",
        "serviceVal": "businessCards",
        "price": "₱250.00 / box (100 pcs)",
        "badge": "Professional",
        "imageUrl": "https://images.unsplash.com/photo-1593062096033-9a26b09da705?auto=format&fit=crop&w=600&q=80",
        "description": "Premium 300 GSM cardstock coated with smooth matte or soft-touch velvet laminate. Single or double-sided full color with rounded corners available."
    },
    {
        "id": "sample-6",
        "title": "Custom DTF Printed Shirts & Apparel",
        "category": "Apparel & Merch",
        "serviceVal": "shirt",
        "price": "Starts at ₱180.00 / pc",
        "badge": "Full Color DTF",
        "imageUrl": "https://images.unsplash.com/photo-1521572267360-ee0c2909d518?auto=format&fit=crop&w=600&q=80",
        "description": "Vibrant direct-to-film (DTF) apparel printing on 100% combed cotton or dri-fit shirts. Stretchable, washable, and highly detailed graphic prints."
    }
]

def get_sample_products():
    try:
        if db is not None:
            docs = list(db.collection('sample_products').stream())
            samples = []
            for doc in docs:
                data = doc.to_dict()
                if data.get('archived') is True: continue
                if 'id' not in data or not data['id']:
                    data['id'] = doc.id
                samples.append(data)
            if docs:
                return samples

            if list(db.collection('archives').where('itemType', '==', 'sample_product').stream()): return []
            # Seed default samples only for a catalog with no archive history
            for s in DEFAULT_SAMPLE_PRODUCTS:
                db.collection('sample_products').document(s['id']).set(s)
            return DEFAULT_SAMPLE_PRODUCTS
        return DEFAULT_SAMPLE_PRODUCTS
    except Exception as e:
        print(f"Error fetching sample products: {e}")
        return DEFAULT_SAMPLE_PRODUCTS

def save_sample_product(sample_data):
    try:
        if db is not None:
            s_id = str(sample_data.get('id') or f"sample-{int(datetime.datetime.now().timestamp())}")
            sample_data['id'] = s_id
            sample_data['updatedAt'] = datetime.datetime.now().isoformat()
            if not sample_data.get('title'): raise ValueError('Sample title is required.')
            ref = db.collection('sample_products').document(s_id)
            @firestore.transactional
            def save(transaction):
                old = ref.get(transaction=transaction)
                if old.exists and old.to_dict().get('archived') is True: raise ValueError('Sample has been archived.')
                transaction.set(ref, sample_data, merge=True)
            save(db.transaction())
            return sample_data
        return sample_data
    except Exception as e:
        print(f"Error saving sample product: {e}")
        return None

def delete_sample_product(sample_id):
    try:
        if db is not None:
            str_id = str(sample_id)
            db.collection('sample_products').document(str_id).delete()
            return True
        return False
    except Exception as e:
        print(f"Error deleting sample product: {e}")
        return False

# ---------------------------------------------------------------------------
# SERVICES CRUD (Printing Services available for customer order form)
# ---------------------------------------------------------------------------

DEFAULT_SERVICES = [
    {"id": "businessCards", "name": "Business Cards", "emoji": "💳", "basePrice": 250, "unit": "from ₱250", "sortOrder": 1},
    {"id": "tarpaulins", "name": "Tarpaulins", "emoji": "🚩", "basePrice": 15, "unit": "₱15/sqft", "sortOrder": 2},
    {"id": "flyers", "name": "Flyers & Leaflets", "emoji": "📄", "basePrice": 2.5, "unit": "₱2.50/pc", "sortOrder": 3},
    {"id": "stickers", "name": "Stickers & Labels", "emoji": "🏷️", "basePrice": 5, "unit": "₱5/pc", "sortOrder": 4},
    {"id": "invitations", "name": "Invitations", "emoji": "✉", "basePrice": 35, "unit": "₱35/pc", "sortOrder": 5},
    {"id": "shirt", "name": "Shirt Printing - DTF/Sublimation", "emoji": "👕", "basePrice": 180, "unit": "₱180/pc", "sortOrder": 6},
    {"id": "photoCanvas", "name": "Photo & Canvas Prints", "emoji": "🖼️", "basePrice": 120, "unit": "from ₱120", "sortOrder": 7},
    {"id": "booklets", "name": "Booklets & Menus", "emoji": "📖", "basePrice": 45, "unit": "₱45/pc", "sortOrder": 8},
    {"id": "mug", "name": "Mug Printing", "emoji": "☕", "basePrice": 150, "unit": "₱150/pc", "sortOrder": 9},
    {"id": "standardA4", "name": "Standard A4 Print", "emoji": "🖨️", "basePrice": 3, "unit": "₱3.00/pg", "sortOrder": 10},
]

def get_services():
    try:
        if db is not None:
            docs = list(db.collection('services').stream())
            services = []
            for doc in docs:
                data = doc.to_dict()
                if data.get('archived') is True: continue
                if 'id' not in data or not data['id']:
                    data['id'] = doc.id
                services.append(data)
            if docs:
                return sorted(services, key=lambda x: x.get('sortOrder', 0))
            if list(db.collection('archives').where('itemType', '==', 'service').stream()): return []
            # Seed defaults only for a catalog with no archive history
            for s in DEFAULT_SERVICES:
                db.collection('services').document(s['id']).set(s)
            return DEFAULT_SERVICES
        return DEFAULT_SERVICES
    except Exception as e:
        print(f"Error fetching services: {e}")
        return DEFAULT_SERVICES

def save_service(service_data):
    try:
        if db is not None:
            s_id = str(service_data.get('id') or f"service-{int(datetime.datetime.now().timestamp())}")
            service_data['id'] = s_id
            service_data = validate_service(service_data)
            service_data['updatedAt'] = datetime.datetime.now().isoformat()
            ref = db.collection('services').document(s_id)
            @firestore.transactional
            def save(transaction):
                old = ref.get(transaction=transaction)
                if old.exists and old.to_dict().get('archived') is True: raise ValueError('Service has been archived.')
                transaction.set(ref, service_data, merge=True)
            save(db.transaction())
            return service_data
        return service_data
    except Exception as e:
        print(f"Error saving service: {e}")
        return None

def delete_service(service_id):
    try:
        if db is not None:
            db.collection('services').document(str(service_id)).delete()
            return True
        return False
    except Exception as e:
        print(f"Error deleting service: {e}")
        return False

def sync_customer_profile(customer_id, data):
    targets = _item_refs('user', str(customer_id), data)
    name = data.get('fullName') or data.get('full_name') or data.get('name')
    payload = dict(data)
    if name: payload.update(fullName=name, full_name=name, name=name)
    batch = db.batch()
    for target in targets:
        safe = {k: v for k, v in payload.items() if target.parent.id != 'users' or k not in ('password', 'passwordHash')}
        batch.set(target, safe, merge=True)
    batch.commit()


def log_rfid_tap(card_uid, earned_points=0):
    earned_points = 0
    try:
        matches = list(db.collection('customers').where('rfidUid', '==', card_uid).stream())
        if not matches: matches = list(db.collection('users').where('rfidUid', '==', card_uid).stream())
        if not matches: return False
        customer = matches[0]
        targets = _item_refs('user', customer.id, customer.to_dict())
        @firestore.transactional
        def apply(transaction):
            data = customer.reference.get(transaction=transaction).to_dict()
            profiles = [r.get(transaction=transaction).to_dict() or {} for r in targets]
            if any(p.get('archived') is True for p in profiles): raise ValueError('Customer is archived.')
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for target in targets:
                transaction.set(target, {'lastTap': now}, merge=True)
            ref = db.collection('rfid_logs').document()
            transaction.set(ref, {'id': ref.id, 'cardUid': card_uid,
                'customerName': data.get('fullName') or data.get('name'),
                'pointsAdded': earned_points, 'timestamp': now})
        apply(db.transaction())
        return True
    except Exception as e:
        print(f'RFID tap failed: {e}')
        return False

# ===========================================================================
# SYSTEM SETTINGS / GCASH MERCHANT CONFIG
# ===========================================================================
DEFAULT_GCASH_SETTINGS = {
    "accountName": "XIAN JOHN DAVID DAWINAN",
    "accountNumber": "09311508184",
    "qrUrl": "/static/images/gcash_qr.png",
    "isActive": True,
    "updatedAt": datetime.datetime.now().isoformat()
}

def get_gcash_settings():
    try:
        if db is not None:
            doc = db.collection('settings').document('gcash_config').get()
            if doc.exists:
                data = doc.to_dict() or {}
                # merge with default fallback
                merged = {**DEFAULT_GCASH_SETTINGS, **data}
                shared = shared_qr_settings(merged, Path(__file__).parent / 'static/images')
                changes = {key: shared[key] for key in ('qrImageData', 'qrImageSource') if data.get(key) != shared[key]}
                if changes:
                    try:
                        doc.reference.set(changes, merge=True)
                    except Exception:
                        pass  # Merchant details are still available during a temporary sync failure.
                return shared
            # seed default if not exists
            db.collection('settings').document('gcash_config').set(DEFAULT_GCASH_SETTINGS)
            return DEFAULT_GCASH_SETTINGS
        return {**DEFAULT_GCASH_SETTINGS, 'isActive': False, 'accountName': '', 'accountNumber': '', 'qrUrl': ''}
    except Exception as e:
        print(f"Error fetching gcash_settings: {e}")
        return {**DEFAULT_GCASH_SETTINGS, 'isActive': False, 'accountName': '', 'accountNumber': '', 'qrUrl': ''}

def save_gcash_settings(settings_data):
    try:
        settings_data = shared_qr_settings(settings_data, Path(__file__).parent / 'static/images')
        if db is not None:
            settings_data['updatedAt'] = datetime.datetime.now().isoformat()
            db.collection('settings').document('gcash_config').set(settings_data, merge=True)
            return settings_data
        return settings_data
    except Exception as e:
        print(f"Error saving gcash_settings: {e}")
        return None

def get_notifications(user_id, email=None, state_user_id=None):
    state_uid = state_user_id or user_id
    email = str(email or '').strip().lower()
    audience = {user_id, state_uid, 'ALL'}
    if email:
        audience.add(email)
    states = {doc.id: doc.to_dict() for doc in
              db.collection('notification_states').document(state_uid).collection('items').get()}
    result = []
    for doc in db.collection('notifications').get():
        data = doc.to_dict() or {}
        addressed = data.get('userId') in audience or (
            email and str(data.get('recipientEmail') or '').strip().lower() == email)
        state = states.get(doc.id) or {}
        if addressed and not state.get('dismissed'):
            read = state.get('read') or str(data.get('status', '')).lower() == 'read'
            if data.get('type') == 'order_status' or str(data.get('title') or '').startswith('Order Status:'):
                message = str(data.get('message') or '')
                match = re.fullmatch(r'Ang iyong (.+) ay nasa status na: (.+)\.', message)
                if match:
                    data['message'] = f'Your {match[1]} is now {match[2]}.'
            result.append({**data, 'id': doc.id, 'status': 'Read' if read else 'Unread'})
    return result


def _save_notification_states(user_id, email=None, state_user_id=None, dismiss=False):
    try:
        items = get_notifications(user_id, email, state_user_id)
        states = db.collection('notification_states').document(state_user_id or user_id).collection('items')
        for start in range(0, len(items), 400):
            batch = db.batch()
            for item in items[start:start + 400]:
                data = {'read': True, 'updatedAt': firestore.SERVER_TIMESTAMP}
                if dismiss:
                    data['dismissed'] = True
                batch.set(states.document(item['id']), data, merge=True)
            batch.commit()
        return True
    except Exception as error:
        print(f'Error saving notification state: {error}')
        return False


def create_notification(notif_data):
    try:
        db.collection('notifications').document(notif_data['id']).set(notif_data)
        return True
    except Exception:
        return False

def mark_all_notifications_read(user_id, email=None, state_user_id=None):
    return _save_notification_states(user_id, email, state_user_id)


def delete_notification(notif_id):
    try:
        db.collection('notifications').document(notif_id).delete()
        return True
    except Exception:
        return False

def delete_all_notifications(user_id, email=None, state_user_id=None):
    return _save_notification_states(user_id, email, state_user_id, dismiss=True)


def get_analytics_summary():
    orders = get_orders()
    total_sales = sum(float(o.get('total', 0)) for o in orders if o.get('status') in ['Approved', 'Completed'])
    return {
        "totalOrders": len(orders),
        "totalSales": total_sales,
        "activeOrders": len([o for o in orders if o.get('status') not in ['Completed', 'Rejected']])
    }

# ---------------------------------------------------------------------------
# LIVE CUSTOMER CHAT & CONVERSATIONS (FIRESTORE)
# ---------------------------------------------------------------------------

def get_safe_conversation_id(email):
    if not email:
        return None
    safe_email = re.sub(r'[^a-zA-Z0-9]', '_', email.strip().lower())
    return f"conv_{safe_email}"

def get_or_create_conversation(customer_email, customer_name=None, customer_phone=None, customer_id=None):
    if db is None or not customer_email:
        return None
    email_clean = customer_email.strip().lower()
    conv_id = get_safe_conversation_id(email_clean)
    conv_ref = db.collection('conversations').document(conv_id)
    doc = conv_ref.get()

    if doc.exists:
        data = doc.to_dict()
        data['id'] = conv_id
        updates = {}
        if customer_name and (not data.get('customerName') or data.get('customerName') == email_clean.split('@')[0]):
            updates['customerName'] = customer_name
            data['customerName'] = customer_name
        if customer_phone and not data.get('customerPhone'):
            updates['customerPhone'] = customer_phone
            data['customerPhone'] = customer_phone
        if customer_id and not data.get('customerId'):
            updates['customerId'] = customer_id
            data['customerId'] = customer_id
        if updates:
            conv_ref.update(updates)
        return data

    # Lookup customer profile in Firestore 'customers' if name not provided
    name = customer_name
    phone = customer_phone
    cid = customer_id
    if not name or not phone or not cid:
        for c in get_customers():
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email_clean:
                if not name:
                    name = c.get('fullName') or c.get('username')
                if not phone:
                    phone = c.get('contact') or c.get('phone')
                if not cid:
                    cid = c.get('id')
                break

    now_iso = datetime.datetime.now().isoformat()
    new_conv = {
        'id': conv_id,
        'customerId': cid or f"CUST-{int(datetime.datetime.now().timestamp())}",
        'customerEmail': email_clean,
        'customerName': name or email_clean.split('@')[0],
        'customerPhone': phone or '',
        'lastMessage': '',
        'lastMessageSender': '',
        'lastMessageSenderName': '',
        'lastMessageAt': now_iso,
        'updatedAt': now_iso,
        'createdAt': now_iso,
        'unreadCountAdmin': 0,
        'unreadCountCustomer': 0
    }
    conv_ref.set(new_conv)
    return new_conv

def get_all_conversations():
    """
    Fetches all conversations sorted by latest message activity descending.
    Ensures that ALL registered customers appear in the inbox (even those who have never
    chatted yet). Customers with active conversations appear first sorted by newest message,
    while registered customers with no conversation yet are neatly appended below them.
    """
    if db is None:
        return []
    try:
        convs_by_email = {}
        # 1. Fetch existing conversations from Firestore
        docs = db.collection('conversations').stream()
        for doc in docs:
            cdata = doc.to_dict()
            cdata['id'] = doc.id
            c_email = (cdata.get('customerEmail') or '').strip().lower()
            if c_email:
                cdata['hasChat'] = bool(cdata.get('lastMessage') or cdata.get('lastMessageAt'))
                convs_by_email[c_email] = cdata

        # 2. Fetch all registered customers to ensure complete customer inbox list
        try:
            customers = get_customers() or []
            for c in customers:
                if not isinstance(c, dict):
                    continue
                c_email = (c.get('email') or '').strip().lower()
                if not c_email or '@' not in c_email:
                    continue
                # Skip if already in conversations
                if c_email in convs_by_email:
                    continue

                # Build an entry for this customer who has never chatted yet
                c_name = c.get('fullName') or c.get('name') or c.get('username') or c_email.split('@')[0]
                c_id = c.get('id') or f"CUST-{abs(hash(c_email)) % 899 + 100}"
                conv_id = get_safe_conversation_id(c_email)
                convs_by_email[c_email] = {
                    'id': conv_id,
                    'customerId': c_id,
                    'customerEmail': c_email,
                    'customerName': c_name,
                    'customerPhone': c.get('contact') or c.get('phone') or '',
                    'lastMessage': '',
                    'lastMessageSender': '',
                    'lastMessageSenderName': '',
                    'lastMessageAt': '',
                    'updatedAt': '',
                    'createdAt': c.get('regDate') or '',
                    'unreadCountAdmin': 0,
                    'unreadCountCustomer': 0,
                    'hasChat': False
                }
        except Exception as err:
            print(f"Warning: could not merge registered customers into conversations: {err}")

        convs = list(convs_by_email.values())

        # Sort so active chats are at top with newest message first,
        # then non-chatted customers sorted alphabetically by name
        convs.sort(key=lambda x: (
            1 if (x.get('lastMessage') or x.get('lastMessageAt')) else 0,
            str(x.get('updatedAt') or x.get('lastMessageAt') or ''),
            str(x.get('customerName') or '').lower()
        ), reverse=True)

        return convs
    except Exception as e:
        print(f"Error fetching conversations: {e}")
        return []


def get_conversation_messages(conv_id, mark_read_for_role=None):
    """
    Fetches messages for a conversation ordered by createdAt asc.
    If mark_read_for_role is 'admin', marks customer messages as read and clears unreadCountAdmin.
    If mark_read_for_role is 'customer', marks admin messages as read and clears unreadCountCustomer.
    """
    if db is None or not conv_id:
        return []
    try:
        conv_ref = db.collection('conversations').document(conv_id)
        messages_ref = conv_ref.collection('messages')
        docs = messages_ref.stream()
        messages = []
        unread_doc_refs = []
        for doc in docs:
            m = doc.to_dict()
            m['id'] = doc.id
            messages.append(m)
            if mark_read_for_role == 'admin' and m.get('senderRole') == 'customer' and not m.get('read', False):
                unread_doc_refs.append(doc.reference)
            elif mark_read_for_role == 'customer' and m.get('senderRole') in ['admin', 'staff'] and not m.get('read', False):
                unread_doc_refs.append(doc.reference)

        messages.sort(key=lambda x: str(x.get('createdAt') or x.get('timestamp') or ''))

        # Update read status if any unread
        if unread_doc_refs:
            for ref in unread_doc_refs:
                try:
                    ref.update({'read': True, 'readAt': datetime.datetime.now().isoformat()})
                except Exception:
                    pass
            if mark_read_for_role == 'admin':
                try:
                    conv_ref.update({'unreadCountAdmin': 0})
                except Exception:
                    pass
            elif mark_read_for_role == 'customer':
                try:
                    conv_ref.update({'unreadCountCustomer': 0})
                except Exception:
                    pass

        return messages
    except Exception as e:
        print(f"Error fetching messages for {conv_id}: {e}")
        return []

def send_chat_message(conv_id, sender_email, sender_role, sender_name, message_text):
    """
    Saves a message in the conversation and updates lastMessage & updatedAt
    so the conversation jumps to the top immediately!
    """
    if db is None or not conv_id or not message_text:
        return None
    try:
        conv_ref = db.collection('conversations').document(conv_id)
        conv_doc = conv_ref.get()
        if not conv_doc.exists:
            get_or_create_conversation(sender_email if sender_role == 'customer' else '')
            conv_doc = conv_ref.get()

        now_dt = datetime.datetime.now()
        now_iso = now_dt.isoformat()
        time_formatted = now_dt.strftime('%I:%M %p').lstrip('0')

        msg_id = f"msg_{int(now_dt.timestamp()*1000)}_{uuid.uuid4().hex[:6]}"
        msg_data = {
            'id': msg_id,
            'conversationId': conv_id,
            'senderId': sender_email,
            'senderRole': sender_role, # 'customer' or 'admin'
            'senderName': sender_name or ('Staff' if sender_role == 'admin' else 'Customer'),
            'message': message_text,
            'text': message_text,
            'time': time_formatted,
            'createdAt': now_iso,
            'timestamp': now_iso,
            'read': False
        }

        # Save message in subcollection
        batch = db.batch()
        batch.set(conv_ref.collection('messages').document(msg_id), msg_data)

        # Update conversation preview and timestamp to move it to the top
        conv_update = {
            'lastMessage': message_text[:120],
            'lastMessageSender': sender_role,
            'lastMessageSenderName': sender_name or ('Staff' if sender_role == 'admin' else 'Customer'),
            'lastMessageAt': now_iso,
            'updatedAt': now_iso
        }
        if sender_role == 'customer':
            current_unread = (conv_doc.to_dict() or {}).get('unreadCountAdmin', 0) if conv_doc.exists else 0
            conv_update['unreadCountAdmin'] = current_unread + 1
        elif sender_role in ['admin', 'staff']:
            current_unread = (conv_doc.to_dict() or {}).get('unreadCountCustomer', 0) if conv_doc.exists else 0
            conv_update['unreadCountCustomer'] = current_unread + 1

        batch.set(conv_ref, conv_update, merge=True)
        batch.set(db.collection('push_jobs').document(f'chat_{msg_id}'), {
            'type': 'chat', 'conversationId': conv_id, 'messageId': msg_id,
            'state': 'pending', 'createdAt': firestore.SERVER_TIMESTAMP,
        })
        customer_email = str((conv_doc.to_dict() or {}).get('customerEmail') or '').strip().lower()
        recipient = 'ADMIN' if sender_role == 'customer' else customer_email
        if recipient:
            notification = {
                'id': f'chat_{msg_id}', 'userId': recipient,
                'title': f'New message from {sender_name or "Customer"}' if sender_role == 'customer' else 'New reply from KCZ Printing',
                'message': message_text[:300], 'type': 'chat',
                'conversationId': conv_id, 'messageId': msg_id, 'status': 'Unread',
                'link': f'/admin?tab=liveChat&convId={conv_id}' if sender_role == 'customer' else '/customer?tab=chat',
                'createdAt': firestore.SERVER_TIMESTAMP, 'timestamp': now_iso,
            }
            if sender_role != 'customer':
                notification['recipientEmail'] = customer_email
            batch.set(db.collection('notifications').document(f'chat_{msg_id}'), notification)
        batch.commit()

        return msg_data
    except Exception as e:
        print(f"Error sending chat message in {conv_id}: {e}")
        return None

def seed_initial_conversations_if_needed():
    if db is None:
        return
    try:
        existing = list(db.collection('conversations').limit(1).stream())
        if existing:
            return

        customers = get_customers() or []
        now = datetime.datetime.now()

        # Match user's illustration (Customer A with unread 1, Customer B, Customer C)
        for idx, c in enumerate(customers[:4]):
            if not isinstance(c, dict):
                continue
            email = c.get('email', '').strip().lower()
            if not email or '@' not in email:
                continue

            name = c.get('fullName') or c.get('username') or f"Customer {chr(65+idx)}"
            cid = c.get('id') or f"CUST-{idx+101}"
            conv = get_or_create_conversation(email, customer_name=name, customer_phone=c.get('contact'), customer_id=cid)

            offset_minutes = (idx + 1) * 12
            past_time = now - datetime.timedelta(minutes=offset_minutes)
            past_iso = past_time.isoformat()

            msg_texts = [
                "Hello, is this available?",
                "Thank you!",
                "Where can I view my order?",
                "How much does tarpaulin printing cost?"
            ]
            sample_text = msg_texts[idx % len(msg_texts)]

            conv_id = conv['id']
            msg_id = f"msg_{int(past_time.timestamp()*1000)}_{idx}"
            db.collection('conversations').document(conv_id).collection('messages').document(msg_id).set({
                'id': msg_id,
                'conversationId': conv_id,
                'senderId': email,
                'senderRole': 'customer',
                'senderName': name,
                'message': sample_text,
                'text': sample_text,
                'time': past_time.strftime('%I:%M %p').lstrip('0'),
                'createdAt': past_iso,
                'timestamp': past_iso,
                'read': idx != 0
            })
            db.collection('conversations').document(conv_id).update({
                'lastMessage': sample_text,
                'lastMessageSender': 'customer',
                'lastMessageSenderName': name,
                'lastMessageAt': past_iso,
                'updatedAt': past_iso,
                'unreadCountAdmin': 1 if idx == 0 else 0
            })
        print("[OK] Initial messenger conversations seeded for existing customers!")
    except Exception as e:
        print(f"Error seeding initial conversations: {e}")


# ---------------------------------------------------------------------------
# PUSH NOTIFICATION & FCM TOKEN SERVICES
# ---------------------------------------------------------------------------

def register_fcm_token(email, role, token, device_info=None):
    """
    Saves or updates an FCM Web Push registration token in Firestore.
    Associates the token with the authenticated user email and role.
    """
    if db is None or not token or not email:
        return False
    try:
        clean_email = email.strip().lower()
        token_doc_id = re.sub(r'[^a-zA-Z0-9]', '_', token[-40:])
        token_ref = db.collection('fcm_tokens').document(token_doc_id)

        now_iso = datetime.datetime.now().isoformat()
        token_data = {
            'token': token,
            'email': clean_email,
            'role': role or 'customer',
            'device': device_info or 'Web Browser',
            'updatedAt': now_iso,
            'createdAt': now_iso,
            'active': True
        }
        token_ref.set(token_data, merge=True)
        return True
    except Exception as e:
        print(f"Error registering FCM token: {e}")
        return False

def remove_fcm_token(token):
    """
    Deactivates or deletes an FCM token on logout or opt-out.
    """
    if db is None or not token:
        return False
    try:
        token_doc_id = re.sub(r'[^a-zA-Z0-9]', '_', token[-40:])
        db.collection('fcm_tokens').document(token_doc_id).delete()
        return True
    except Exception as e:
        print(f"Error removing FCM token: {e}")
        return False

def send_fcm_push_notification(recipient_role, recipient_email, title, body, click_url=None, data_payload=None):
    """
    Sends browser background push notification via Firebase Cloud Messaging.
    - If recipient_role == 'admin', sends to all active admin tokens.
    - If recipient_role == 'customer', sends to tokens associated with recipient_email.
    - Never throws an uncaught error if delivery fails or token is expired.
    """
    if db is None or not title:
        return {"success": False, "sent_count": 0, "message": "Firebase db unavailable"}

    try:
        # 1. Query target tokens
        tokens_ref = db.collection('fcm_tokens')
        if recipient_role in ['admin', 'staff']:
            query = tokens_ref.where('role', 'in', ['admin', 'staff']).where('active', '==', True)
        else:
            if not recipient_email:
                return {"success": False, "sent_count": 0, "message": "Recipient email required for customer push"}
            clean_email = recipient_email.strip().lower()
            query = tokens_ref.where('email', '==', clean_email).where('active', '==', True)

        docs = query.stream()
        active_tokens = []
        doc_refs_by_token = {}
        for d in docs:
            tdata = d.to_dict()
            tok = tdata.get('token')
            if tok and tok not in active_tokens and tok not in (data_payload or {}).get('_exclude_tokens', []):
                active_tokens.append(tok)
                doc_refs_by_token[tok] = d.reference

        if not active_tokens:
            return {"success": True, "sent_count": 0, "message": "No active tokens for recipient"}

        # 2. Prepare payload
        payload_data = {
            'click_url': click_url or '/',
            'title': title,
            'body': body or '',
            'timestamp': str(int(datetime.datetime.now().timestamp()))
        }
        if recipient_email:
            payload_data['recipient_email'] = recipient_email.strip().lower()
        if data_payload and isinstance(data_payload, dict):
            for k, v in data_payload.items():
                if not str(k).startswith('_'):
                    payload_data[str(k)] = str(v)

        webpush_config = messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title=title,
                body=body,
                icon='/static/icons/icon-192.png',
                badge='/static/icons/icon-192.png',
                tag=f"kcz_chat_{payload_data.get('conversation_id', 'general')}",
                renotify=True,
                data=payload_data
            ),
            fcm_options=(messaging.WebpushFCMOptions(link=click_url)
                         if click_url and click_url.startswith('https://') else None)
        )

        sent_count = 0
        failed_tokens = []
        invalid_tokens = []

        for token in active_tokens:
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(
                        title=title,
                        body=body
                    ),
                    data=payload_data,
                    webpush=webpush_config,
                    android=messaging.AndroidConfig(
                        priority='high',
                        ttl=datetime.timedelta(hours=24),
                        notification=messaging.AndroidNotification(
                            channel_id='kcz_chat_channel',
                            icon='ic_notification',
                            color='#15803D',
                            tag=payload_data.get('event_id'),
                        ),
                    ),
                    token=token
                )
                messaging.send(msg)
                sent_count += 1
            except messaging.UnregisteredError:
                invalid_tokens.append(token)
            except Exception as token_err:
                failed_tokens.append(token)
                print(f"Push send warning: {type(token_err).__name__}")

        # Clean up expired or unregistered tokens
        for inv_tok in invalid_tokens:
            try:
                ref = doc_refs_by_token.get(inv_tok)
                if ref:
                    ref.delete()
            except Exception:
                pass

        return {
            "success": not failed_tokens,
            "sent_count": sent_count,
            "delivered_tokens": [t for t in active_tokens if t not in invalid_tokens and t not in failed_tokens],
            "failed_count": len(failed_tokens),
            "invalid_cleaned": len(invalid_tokens)
        }
    except Exception as e:
        print(f"Error in send_fcm_push_notification: {e}")
        return {"success": False, "sent_count": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# ARCHIVE MANAGEMENT SYSTEM (Soft-delete & Restore for Users, Services, Products, Samples)
# ---------------------------------------------------------------------------

def archive_item(item_type, item_id, item_data=None, original_source=None):
    try:
        if db is None: return False
        item_id = str(item_id)
        targets = _item_refs(item_type, item_id, item_data or {})
        @firestore.transactional
        def apply(transaction):
            records = [r.get(transaction=transaction) for r in targets]
            existing = [d for d in records if d.exists]
            if not existing or existing[0].to_dict().get('archived') is True:
                raise ValueError('Item is missing or already archived.')
            data = {**(item_data or {}), **existing[0].to_dict(), 'id': item_id}
            archive_id = f'{item_type}_{item_id}'
            transaction.set(db.collection('archives').document(archive_id), {
                'id': archive_id, 'originalId': item_id, 'itemType': item_type,
                'data': data, 'source': original_source or 'admin',
                'originalPaths': [r.path for r in targets],
                'archivedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()
            })
            for target in targets:
                safe = {k: v for k, v in data.items() if target.parent.id != 'users' or k not in ('password', 'passwordHash')}
                transaction.set(target, {**safe, 'archived': True, 'isArchived': True,
                                        'archivedAt': firestore.SERVER_TIMESTAMP}, merge=True)
        apply(db.transaction())
        return True
    except Exception as e:
        print(f'Error archiving: {e}')
        return False


def get_archived_items(item_type=None):
    """
    Retrieves archived items from Firestore archives collection.
    """
    try:
        if db is not None:
            col = db.collection('archives')
            if item_type and item_type != 'all':
                docs = col.where('itemType', '==', item_type).stream()
            else:
                docs = col.stream()
            items = []
            for d in docs:
                data = d.to_dict()
                data['archiveDocId'] = d.id
                items.append(data)
            items.sort(key=lambda x: x.get('archivedAt', ''), reverse=True)
            return items
        return []
    except Exception as e:
        print(f"Error fetching archives: {e}")
        return []

def _legacy_products():
    return db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('store_products')


def _item_refs(item_type, item_id, data):
    if item_type == 'store_product':
        return [db.collection('products').document(item_id), _legacy_products().document(item_id)]
    if item_type == 'sample_product':
        return [db.collection('sample_products').document(item_id), db.collection('sample_works').document(item_id)]
    if item_type == 'service':
        return [db.collection('services').document(item_id)]
    if item_type != 'user':
        raise ValueError('Unknown archive type')
    refs = {}
    email = str(data.get('email') or '').strip().lower()
    if not email:
        for source in ['customers', 'users']:
            candidate = db.collection(source).document(item_id).get()
            if candidate.exists:
                email = str(candidate.to_dict().get('email') or '').strip().lower()
                if email: break
    for collection in ['customers', 'users']:
        direct = db.collection(collection).document(item_id)
        if direct.get().exists: refs[direct.path] = direct
        if email:
            for doc in db.collection(collection).where('email', '==', email).stream():
                refs[doc.reference.path] = doc.reference
    customer_id = str(data.get('customerId') or data.get('id') or item_id)
    refs.setdefault('customers/' + customer_id, db.collection('customers').document(customer_id))
    if not any(r.parent.id == 'users' for r in refs.values()):
        uid = str(data.get('uid') or item_id)
        refs['users/' + uid] = db.collection('users').document(uid)
    return list(refs.values())


def _finish_archive(archive_doc_id, restore):
    if db is None: return False, 'Database not connected.'
    ref = db.collection('archives').document(archive_doc_id)
    snapshot = ref.get()
    if not snapshot.exists: return False, 'Archived item not found.'
    item = snapshot.to_dict()
    orig_id = str(item['originalId'])
    data = dict(item.get('data') or {})
    targets = {r.path: r for r in _item_refs(item['itemType'], orig_id, data)}
    for path in item.get('originalPaths', []): targets[path] = db.document(path)
    @firestore.transactional
    def apply(transaction):
        current = ref.get(transaction=transaction)
        records = [r.get(transaction=transaction) for r in targets.values()]
        if not current.exists: raise ValueError('Archive was already restored or deleted.')
        def compatible_user(d):
            return restore and item['itemType'] == 'user' and data.get('email') and d.to_dict().get('email') == data.get('email')
        if any(d.exists and d.to_dict().get('archived') is not True and not compatible_user(d) for d in records):
            raise ValueError('An active record already exists. Refresh before continuing.')
        payload = dict(current.to_dict().get('data') or {}, id=orig_id, archived=False, isArchived=False)
        if item['itemType'] == 'store_product':
            source = next((d for d in records if d.exists and d.reference.parent.id == 'products'), next((d for d in records if d.exists), None))
            if source is not None:
                payload = {**payload, **source.to_dict(), 'id': orig_id, 'archived': False, 'isArchived': False}
        payload.pop('archivedAt', None)
        for target, before in zip(targets.values(), records):
            if restore and before.exists and before.to_dict().get('archived') is not True and compatible_user(before):
                transaction.set(target, {'archived': False, 'isArchived': False, 'archivedAt': firestore.DELETE_FIELD}, merge=True)
            elif restore:
                safe = {k: v for k, v in payload.items() if target.parent.id != 'users' or k not in ('password', 'passwordHash')}
                if item['itemType'] == 'user':
                    old = before.to_dict() or {}
                    for field in ['points', 'loyaltyPoints', 'loyaltyUid']:
                        if before.exists:
                            if field in old:
                                safe[field] = old[field]
                            else:
                                safe.pop(field, None)
                        elif field != 'loyaltyUid':
                            safe[field] = 0
                transaction.set(target, safe)
            else: transaction.delete(target)
        transaction.delete(ref)
    apply(db.transaction())
    return True, 'Item restored successfully!' if restore else 'Item permanently deleted.'


def restore_archived_item(archive_doc_id):
    try: return _finish_archive(archive_doc_id, True)
    except Exception as e: return False, str(e)


def permanently_delete_archived_item(archive_doc_id):
    try: return _finish_archive(archive_doc_id, False)[0]
    except Exception as e:
        print(f'Error deleting archive: {e}')
        return False


# ===========================================================================
# LOYALTY BACKEND
# ===========================================================================

def orders_ref(db):
    return db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('orders')


def legacy_products(db):
    return db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('store_products')


def config(db, transaction=None):
    data = db.collection('settings').document('loyalty_config').get(transaction=transaction).to_dict() or {}
    if not data.get('enabled'):
        raise ValueError('Rewards are awaiting the loyalty rollout. Please contact the admin.')
    return data


def read_account(db, transaction, uid):
    ref = db.collection('loyalty_accounts').document(uid)
    data = ref.get(transaction=transaction).to_dict() or {'balance': 0, 'reserved': 0}
    account_values(data)
    return ref, dict(data)


def save_account(transaction, ref, data, event_id, kind, points, order_id):
    balance, reserved = account_values(data)
    transaction.set(ref, {'balance': balance, 'reserved': reserved,
                         'updatedAt': firestore.SERVER_TIMESTAMP}, merge=True)
    transaction.create(ref.collection('events').document(event_id), {
        'type': kind, 'points': points, 'orderId': order_id,
        'balance': balance, 'reserved': reserved, 'createdAt': firestore.SERVER_TIMESTAMP,
    })


def operation_key(uid, key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}', key):
        raise ValueError('A valid submissionId is required for safe retries.')
    return hashlib.sha256(f'{uid}|{key}'.encode()).hexdigest()


def catalog_order(db, transaction, payload, reward=None):
    """Rebuild prices, names and reward package limits from the current catalog."""
    order = dict(payload)
    items = payload.get('items') or []
    if reward:
        order['quantity'] = order['copies'] = reward['quantity']
        order['pages'] = reward.get('pages', 1)
        if reward['kind'] == 'product':
            items = [{'id': reward['catalogId'], 'qty': reward['quantity']}]
        else:
            items = []
            order['serviceId'] = reward['catalogId']
            order['details'] = reward.get('specifications') or {}
            order['customRequirements'] = reward.get('requirements') or {}
    if items:
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise ValueError('Add between 1 and 50 store items.')
        clean, seen, subtotal = [], set(), 0
        for item in items:
            key = str(item.get('id') or item.get('productId') or '')
            if not key or '/' in key or key in seen:
                raise ValueError('Invalid or repeated store product.')
            seen.add(key)
            product = db.collection('products').document(key).get(transaction=transaction)
            if not product.exists:
                product = legacy_products(db).document(key).get(transaction=transaction)
            data = product.to_dict() or {}
            if not data or data.get('archived') or data.get('isArchived'):
                raise ValueError('A store product is unavailable.')
            qty = whole(item.get('qty', item.get('quantity', 1)), 'Quantity', 1, 100000)
            if qty > int(data.get('stock') or 0):
                raise ValueError('Insufficient stock. Refresh the store.')
            unit = money(data.get('price'))
            if unit <= 0:
                raise ValueError('A product has no valid price.')
            subtotal += unit * qty
            clean.append({'id': key, 'productId': key, 'name': data.get('name', key),
                          'qty': qty, 'price': unit / 100, 'lineTotal': unit * qty / 100})
        order.update(items=clean, service='In-Store: ' + ', '.join(f"{x['name']} ({x['qty']}x)" for x in clean),
                     paperSize='Store Supply', colorMode='N/A', file='Store_Receipt.pdf',
                     quantity=sum(x['qty'] for x in clean), copies=sum(x['qty'] for x in clean), pages=1)
    else:
        service_id = str(order.get('serviceId') or '')
        if not service_id or '/' in service_id:
            raise ValueError('Select a catalog service.')
        service = db.collection('services').document(service_id).get(transaction=transaction).to_dict() or {}
        if not service or service.get('archived') or service.get('isArchived'):
            raise ValueError('The service is unavailable.')
        validate_requirements(service, order.get('customRequirements') or {})
        if not str(order.get('file') or order.get('fileName') or '').strip():
            raise ValueError('Attach the document or design file.')
        url = urlparse(str(order.get('fileUrl') or ''))
        if url.scheme != 'https' or not url.netloc:
            raise ValueError('Finish uploading the document before redeeming points.')
        subtotal = service_total(service_id, service.get('basePrice'), order)
        order.update(items=[], service=service['name'])
    if not 0 < subtotal <= 500000000:
        raise ValueError('Invalid order subtotal.')
    return order, subtotal


def submit_order(db, actor, payload):
    key = operation_key(actor['uid'], payload.get('submissionId'))
    request_ref = db.collection('loyalty_submissions').document(key)
    for _ in range(10):
        order_id = f'PRNT-{secrets.randbelow(1000000):06d}'
        order_ref = orders_ref(db).document(order_id)

        @firestore.transactional
        def apply(transaction):
            prior = request_ref.get(transaction=transaction)
            if prior.exists:
                if prior.to_dict().get('uid') != actor['uid']:
                    raise PermissionError('Submission belongs to another customer.')
                return orders_ref(db).document(prior.to_dict()['orderId']).get(transaction=transaction).to_dict()
            if order_ref.get(transaction=transaction).exists:
                return None
            settings = config(db, transaction)
            account_ref, account = read_account(db, transaction, actor['uid'])
            balance, reserved = account_values(account)
            reward_id = str(payload.get('rewardId') or '')
            reward = None
            if reward_id:
                if '/' in reward_id or payload.get('pointsUsed', 0):
                    raise ValueError('Select one reward type per transaction.')
                reward = db.collection('loyalty_rewards').document(reward_id).get(transaction=transaction).to_dict() or {}
                if payload.get('rewardVersion') != reward.get('updatedAt'):
                    raise ValueError('Reward details changed. Reload the rewards catalog.')
                if not reward.get('active'):
                    raise ValueError('This reward is unavailable.')
            order, subtotal = catalog_order(db, transaction, payload, reward)
            if reward:
                points = whole(reward['pointsCost'], 'Reward points', 1)
                if points > balance - reserved:
                    raise ValueError('Insufficient available points.')
                quote = {'pointsUsed': points, 'subtotal': subtotal / 100,
                         'pointsDiscount': subtotal / 100, 'total': 0,
                         'rewardId': reward_id, 'rewardName': reward['name'],
                         'rewardSnapshot': reward, 'redemptionType': 'reward'}
                payment, reference, receipt, merchant = 'Points', '', '', ''
            else:
                quote = discount_quote(subtotal, payload.get('pointsUsed', 0), balance - reserved,
                                       settings.get('pointValueCents', 100))
                if quote['pointsUsed'] and 'expectedDiscount' in payload and money(payload['expectedDiscount']) != money(quote['pointsDiscount']):
                    raise ValueError('Point conversion changed. Reload the points balance.')
                points = quote['pointsUsed']
                quote['redemptionType'] = 'discount' if points else 'none'
                if money(payload.get('totalAmount', payload.get('total'))) != subtotal:
                    raise ValueError('Catalog prices changed. Review the updated subtotal.')
                payment, reference = validate_payment(payload.get('payment') or 'Cash on Pickup', str(payload.get('gcashRef') or ''))
                receipt, merchant = '', ''
                if 'gcash' in payment.lower():
                    details = db.collection('settings').document('gcash_config').get(transaction=transaction).to_dict() or {}
                    merchant = validate_merchant(details, str(payload.get('merchantAccountNumber') or ''))
                    receipt = validate_receipt(str(payload.get('gcashReceiptUrl') or ''))
            note = str(payload.get('customerNote') or '').strip()
            if len(note) > 500:
                raise ValueError('Note must be at most 500 characters.')
            allowed = ['serviceId', 'service', 'customRequirements', 'details', 'items', 'quantity', 'copies',
                       'pages', 'paperSize', 'colorMode', 'sides', 'file', 'fileName', 'fileUrl', 'storagePath']
            record = {k: order[k] for k in allowed if k in order}
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record.update(quote, id=order_id, submissionId=payload['submissionId'],
                          customerId=actor['uid'], loyaltyUid=actor['uid'], email=actor['email'],
                          customer=actor['name'], status='Order Submitted', stockDeducted=False,
                          payment=payment, gcashRef=reference, gcashReceiptUrl=receipt,
                          merchantAccountNumber=merchant, customerNote=note, createdAt=now,
                          timestamp=now, loyaltyEpoch=settings['epoch'], pointsEarned=0,
                          pointsState='reserved' if points else 'none')
            if points:
                account['reserved'] = reserved + points
                save_account(transaction, account_ref, account, f'reserve_{order_id}', 'reserve', points, order_id)
            transaction.create(order_ref, record)
            transaction.create(request_ref, {'uid': actor['uid'], 'orderId': order_id})
            transaction.set(db.collection('notifications').document('order_created_' + order_id), {
                'id': 'order_created_' + order_id, 'userId': 'ADMIN', 'type': 'new_order',
                'orderId': order_id, 'title': f'New Order: {order_id}',
                'message': f"{actor['name']} placed an order.", 'status': 'Unread',
                'link': '/admin', 'createdAt': firestore.SERVER_TIMESTAMP, 'timestamp': now,
            })
            return record
        result = apply(db.transaction())
        if result is not None:
            return result
    raise ValueError('Unable to reserve an order reference. Retry the same submission.')


def apply_order_status(db, order_id, target, actor, reason='', payment_confirmed=False):
    if not order_id or '/' in order_id:
        raise FileNotFoundError('Order not found.')
    ref = orders_ref(db).document(order_id)

    @firestore.transactional
    def apply(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise FileNotFoundError('Order not found.')
        order = snapshot.to_dict()
        current = order.get('status', 'Order Submitted')
        cancelling = target == 'Cancelled'
        if cancelling:
            if actor['uid'] not in [order.get('customerId'), order.get('uid')] and actor['email'] != str(order.get('email') or order.get('customerEmail') or '').lower():
                raise PermissionError('You can only cancel your own order.')
        elif actor['role'] != 'admin':
            raise PermissionError('Admin access required.')
        if current == target:
            return order
        if cancelling:
            if not can_cancel_order(order):
                raise ValueError('This order cannot be cancelled because it has been approved or processing has started.')
        elif not valid_transition(current, target):
            raise ValueError('Invalid status transition.')
        if not isinstance(reason, str) or len(reason.strip()) > 500:
            raise ValueError('Reason must be at most 500 characters.')
        if target.startswith('Rejected') and len(reason.strip()) < 4:
            raise ValueError('Provide a rejection reason of at least 4 characters.')
        if target == 'Completed' and money(order.get('total', 0)) > 0 and payment_confirmed is not True:
            raise ValueError('Confirm that payment was received before completing the order.')
        settings = config(db, transaction)
        uid = order.get('loyaltyUid')
        # Resolve legacy ownership outside this function, never from an editable buyer name.
        if not uid and actor.get('orderCustomerUid'):
            uid = actor['orderCustomerUid']
        account_ref, account = read_account(db, transaction, uid) if uid else (None, None)
        products = {}
        deduct = target == 'Approved' and not order.get('stockDeducted')
        restore = target.startswith('Rejected') and order.get('stockDeducted') is True
        if deduct or restore:
            products = {d.id: d.to_dict() for d in legacy_products(db).stream(transaction=transaction)}
            products.update({d.id: d.to_dict() for d in db.collection('products').stream(transaction=transaction)})
        plan = stock_plan(order, products, restore) if deduct or restore else {}
        updates = {'status': target}
        points = whole(order.get('pointsUsed', 0), 'Points', maximum=9007199254740991)
        state = order.get('pointsState', 'none')
        if points and not uid:
            raise ValueError('Resolve the loyalty account before changing this order.')
        if target == 'Approved':
            updates['approvedAt'] = firestore.SERVER_TIMESTAMP
            if state == 'reserved':
                account['balance'] -= points
                account['reserved'] -= points
                updates['pointsState'] = 'spent'
                save_account(transaction, account_ref, account, f'redeem_{order_id}', 'redeem', points, order_id)
        if cancelling or target.startswith('Rejected'):
            if state == 'reserved':
                account['reserved'] -= points
                updates['pointsState'] = 'returned'
                save_account(transaction, account_ref, account, f'return_{order_id}', 'release', points, order_id)
            elif state == 'spent':
                account['balance'] += points
                updates['pointsState'] = 'returned'
                save_account(transaction, account_ref, account, f'return_{order_id}', 'return', points, order_id)
        if target == 'Completed':
            updates['completedAt'] = firestore.SERVER_TIMESTAMP
            updates['paymentStatus'] = 'Paid' if money(order.get('total', 0)) else 'Paid with Points'
            if uid and money(order.get('total', 0)) > 0 and not order.get('pointsEarned'):
                account['balance'] += 1
                updates['pointsEarned'] = 1
                save_account(transaction, account_ref, account, f'earn_{order_id}', 'earn', 1, order_id)
        if uid:
            updates['loyaltyUid'] = uid
        if deduct or restore:
            updates['stockDeducted'] = bool(deduct and plan)
        if cancelling:
            updates.update(cancelledAt=firestore.SERVER_TIMESTAMP, cancelledBy=actor['uid'],
                           cancelledByEmail=actor['email'], cancellationReason=reason.strip())
        if target.startswith('Rejected'):
            updates['rejectionReason'] = reason.strip()
        for key, stock in plan.items():
            for product_ref in [db.collection('products').document(key), legacy_products(db).document(key)]:
                transaction.set(product_ref, {**products[key], 'stock': stock}, merge=True)
        transaction.update(ref, updates)
        transaction.set(db.collection('push_jobs').document(), {
            'type': 'order_status', 'orderId': order_id, 'status': target,
            'state': 'pending', 'createdAt': firestore.SERVER_TIMESTAMP,
        })
        notif = db.collection('notifications').document()
        transaction.set(notif, {'id': notif.id, 'userId': uid or order.get('customerId') or order.get('email'),
            'recipientEmail': str(order.get('email') or order.get('customerEmail') or '').lower(),
            'title': f'Order Status: {target}', 'message': f'{order_id} is now {target}.',
            'type': 'order_status', 'orderId': order_id, 'status': 'Unread',
            'link': '/customer?tab=myOrders', 'createdAt': firestore.SERVER_TIMESTAMP,
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()})
        # Server timestamp sentinels are never included in an HTTP response.
        return {**order, **{k: v for k, v in updates.items() if v is not firestore.SERVER_TIMESTAMP}}
    return apply(db.transaction())


# ===========================================================================
# POS BACKEND
# ===========================================================================

def complete_sale(db, request, cashier, loyalty_customer=None):
    key = str(request.get('submissionId') or '')
    if not re.fullmatch(r'[a-f0-9]{32}', key):
        raise ValueError('Invalid sale session. Start a new sale.')
    receipt_ref = db.collection('pos_sales').document(key)
    orders = db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('orders')
    legacy = db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('store_products')
    for _ in range(10):
        order_id = f'POS-{secrets.randbelow(1000000):06d}'

        @firestore.transactional
        def apply(transaction):
            previous = receipt_ref.get(transaction=transaction)
            if previous.exists:
                sale = previous.to_dict()
                if sale.get('cashier') != cashier:
                    raise PermissionError('This sale belongs to another cashier.')
                return sale
            occupied = orders.document(order_id).get(transaction=transaction)
            if occupied.exists:
                return None
            settings, account_ref, account = None, None, None
            loyalty_request = dict(request)
            # Existing unit callers retain their legacy sale path until rollout is configured.
            if loyalty_customer or request.get('pointsUsed') or request.get('rewardId'):
                settings = config(db, transaction)
                if not loyalty_customer:
                    raise ValueError('Link a registered customer account before using points.')
                account_ref, account = read_account(db, transaction, loyalty_customer['uid'])
            reward_id = str(request.get('rewardId') or '')
            reward = None
            if reward_id:
                if '/' in reward_id or request.get('pointsUsed', 0):
                    raise ValueError('Choose one reward type.')
                reward = db.collection('loyalty_rewards').document(reward_id).get(transaction=transaction).to_dict() or {}
                if request.get('rewardVersion') != reward.get('updatedAt'):
                    raise ValueError('Reward changed. Reload the catalog.')
                if not reward.get('active'):
                    raise ValueError('This reward is unavailable.')
                if request.get('items'):
                    raise ValueError('A free reward is a separate counter claim.')
                product = db.collection('products' if reward['kind'] == 'product' else 'services').document(reward['catalogId']).get(transaction=transaction).to_dict() or {}
                unit = money(product.get('price')) if reward['kind'] == 'product' else service_total(reward['catalogId'], product.get('basePrice'), {
                    'quantity': 1, 'pages': reward.get('pages', 1), 'details': reward.get('specifications') or {}})
                loyalty_request.update(items=[{'kind': reward['kind'], 'id': reward['catalogId'], 'qty': reward['quantity'],
                    'unitPrice': unit/100, 'note': str(reward.get('specifications') or '')}],
                    expectedTotal=unit*reward['quantity']/100, payment='Points')
            raw_lines = loyalty_request.get('items')
            if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 50:
                raise ValueError('Add between 1 and 50 items.')
            catalog = {}
            for raw in raw_lines:
                if not isinstance(raw, dict):
                    raise ValueError('Invalid POS item.')
                kind, item_id = raw.get('kind'), str(raw.get('id') or '')
                if kind not in ['product', 'service'] or not item_id or '/' in item_id or len(item_id) > 100:
                    raise ValueError('Invalid POS item.')
                ref = db.collection('products' if kind == 'product' else 'services').document(item_id)
                current = ref.get(transaction=transaction)
                if kind == 'product' and not current.exists:
                    current = legacy.document(item_id).get(transaction=transaction)
                catalog[(kind, item_id)] = current.to_dict() if current.exists else None
            if reward and reward['kind'] == 'service':
                validate_requirements(catalog[('service', reward['catalogId'])], reward.get('requirements') or {})
            quote = {'pointsUsed': 0, 'pointsDiscount': 0}
            if account is not None:
                balance, reserved = account_values(account)
                if reward:
                    points = whole(reward['pointsCost'], 'Reward cost', 1)
                    if points > balance-reserved:
                        raise ValueError('Insufficient points.')
                    quote = {'pointsUsed': points, 'pointsDiscount': loyalty_request['expectedTotal'],
                             'rewardId': reward_id, 'rewardName': reward['name'], 'redemptionType': 'reward'}
                else:
                    quote = discount_quote(money(loyalty_request.get('expectedTotal')), request.get('pointsUsed', 0),
                                           balance-reserved, settings.get('pointValueCents', 100))
            if quote['pointsUsed'] and not reward and 'expectedDiscount' in request and money(request['expectedDiscount']) != money(quote['pointsDiscount']):
                raise ValueError('Point conversion changed. Reload the customer balance.')
            sale = validate_sale(loyalty_request, catalog, money(quote['pointsDiscount']), allow_points=bool(reward))
            sale.update({k: v for k, v in quote.items() if k not in ['total', 'subtotal']})
            if loyalty_customer:
                sale.update(customerUid=loyalty_customer['uid'], loyaltyUid=loyalty_customer['uid'],
                            customerId=loyalty_customer['uid'], email=loyalty_customer['email'])
            sale['pointsEarned'] = 1 if loyalty_customer and sale['total'] > 0 else 0
            sale['pointsState'] = 'spent' if quote['pointsUsed'] else 'none'
            if settings:
                sale['loyaltyEpoch'] = settings['epoch']
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record = order_record(sale, order_id, cashier, key, now)
            for item in sale['items']:
                if item['kind'] == 'product':
                    product = catalog[('product', item['id'])]
                    updated = {**product, 'id': item['id'], 'stock': int(product.get('stock') or 0) - item['qty']}
                    transaction.set(db.collection('products').document(item['id']), updated, merge=True)
                    transaction.set(legacy.document(item['id']), updated, merge=True)
            if account is not None:
                if quote['pointsUsed']:
                    account['balance'] -= quote['pointsUsed']
                    save_account(transaction, account_ref, account, f'redeem_{order_id}', 'redeem', quote['pointsUsed'], order_id)
                if sale['pointsEarned']:
                    account['balance'] += 1
                    save_account(transaction, account_ref, account, f'earn_{order_id}', 'earn', 1, order_id)
            transaction.set(orders.document(order_id), record)
            transaction.set(receipt_ref, {**record, 'recordedAt': firestore.SERVER_TIMESTAMP})
            return record

        result = apply(db.transaction())
        if result is not None:
            return result
    raise ValueError('Unable to reserve a receipt number. Retry this sale.')


# ===========================================================================
# PUSH WORKER
# ===========================================================================

log = logging.getLogger(__name__)
_lock = threading.Lock()
_thread = None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def customer_email(data):
    email = data.get('customerEmail') or data.get('email')
    if email and '@' in str(email):
        return str(email).strip().lower()
    owner = data.get('customerId') or data.get('uid')
    if owner and '@' in str(owner):
        return str(owner).strip().lower()
    if owner:
        try:
            return auth.get_user(str(owner)).email
        except auth.UserNotFoundError:
            return None
    return None


def job_payload(db, job, event_id):
    """Read the saved source, never a client-supplied recipient or message body."""
    if job.get('type') == 'order_status':
        order_id = job.get('orderId')
        status = str(job.get('status') or '')
        allowed = ['Approved', 'Printing', 'Preparing', 'Ready for Pickup', 'Completed']
        if not order_id or (status not in allowed and not status.startswith('Rejected')):
            return None
        order = db.document(f'artifacts/printcraft-pro/public/data/orders/{order_id}').get()
        if not order.exists:
            return None
        data = order.to_dict()
        email = customer_email(data)
        if not email:
            return None
        return dict(
            recipient_role='customer', recipient_email=email,
            title=f'Order {status}: {order_id}'[:200],
            body=f"Your {data.get('service') or 'order'} is now {status}."[:300],
            click_url='/customer?tab=myOrders',
            data_payload={'type': 'order_status', 'order_id': order_id,
                          'status': status, 'event_id': event_id},
        )
    if job.get('type') == 'chat':
        conv_id, msg_id = job.get('conversationId'), job.get('messageId')
        if not conv_id or not msg_id:
            return None
        ref = db.collection('conversations').document(conv_id)
        conv, message = ref.get(), ref.collection('messages').document(msg_id).get()
        if not conv.exists or not message.exists:
            return None
        data, msg = conv.to_dict(), message.to_dict()
        sender = str(msg.get('senderRole') or msg.get('sender') or '').lower()
        if sender not in ['admin', 'staff', 'customer']:
            return None
        to_customer = sender in ['admin', 'staff']
        email = customer_email(data) if to_customer else None
        if to_customer and not email:
            return None
        return dict(
            recipient_role='customer' if to_customer else 'admin', recipient_email=email,
            title='New message from KCZ Printing' if to_customer else 'New customer message',
            body=str(msg.get('message') or msg.get('text') or 'New message')[:300],
            click_url='/customer?tab=chat' if to_customer else f'/admin?tab=liveChat&convId={conv_id}',
            data_payload={'type': 'chat', 'conversation_id': conv_id, 'event_id': event_id},
        )
    return None


@firestore.transactional
def claim_job(transaction, ref, now, lease):
    snap = ref.get(transaction=transaction)
    data = snap.to_dict() or {}
    if data.get('state') not in ['pending', 'retry', 'sending']:
        return None
    if data.get('availableAt', now) > now or data.get('leaseUntil', now) > now:
        return None
    created = data.get('createdAt')
    if isinstance(created, datetime.datetime) and now - created > datetime.timedelta(days=1):
        transaction.update(ref, {'state': 'expired', 'finishedAt': now})
        return None
    transaction.update(ref, {
        'state': 'sending', 'lease': lease,
        'leaseUntil': now + datetime.timedelta(minutes=5),
        'attempts': int(data.get('attempts', 0)) + 1,
    })
    return data


@firestore.transactional
def finish_job(transaction, ref, lease, updates):
    snap = ref.get(transaction=transaction)
    if snap.exists and snap.to_dict().get('lease') == lease:
        transaction.update(ref, {**updates, 'leaseUntil': utc_now()})


def deliver_job(db, ref, send):
    lease = uuid.uuid4().hex
    job = claim_job(db.transaction(), ref, utc_now(), lease)
    if job is None:
        return
    delivered = list(job.get('deliveredTokens', []))
    try:
        payload = job_payload(db, job, ref.id)
        if payload is None:
            finish_job(db.transaction(), ref, lease, {'state': 'unresolved', 'finishedAt': utc_now()})
            return
        payload['data_payload']['_exclude_tokens'] = delivered
        result = send(**payload)
        delivered = list(set(delivered + result.get('delivered_tokens', [])))
        if result.get('success'):
            finish_job(db.transaction(), ref, lease, {
                'state': 'sent', 'finishedAt': utc_now(), 'deliveredTokens': delivered,
            })
            return
    except Exception:
        log.exception('Push job %s failed', ref.id)
    delay = min(1800, 15 * 2 ** min(int(job.get('attempts', 0)), 7))
    finish_job(db.transaction(), ref, lease, {
        'state': 'retry', 'availableAt': utc_now() + datetime.timedelta(seconds=delay),
        'deliveredTokens': delivered,
    })


def run(db, send):
    while True:
        try:
            # One field index, with persistent leases for concurrent web workers.
            jobs = db.collection('push_jobs').where('state', 'in', ['pending', 'retry', 'sending'])
            for snapshot in jobs.stream():
                deliver_job(db, snapshot.reference, send)
        except Exception:
            log.exception('Push delivery poll failed')
        threading.Event().wait(5)


def start_push_worker(db, send):
    global _thread
    if db is None:
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=run, args=(db, send), name='fcm-delivery', daemon=True)
            _thread.start()


# ===========================================================================
# LOYALTY API
# ===========================================================================

class SignInRequired(Exception):
    pass


def customer_identity(uid=None, email=None):
    user = auth.get_user(uid) if uid else auth.get_user_by_email(email)
    if user.disabled:
        raise PermissionError('Customer account is disabled.')
    profile = db.collection('users').document(user.uid).get().to_dict() or {}
    if profile.get('email') and str(profile['email']).strip().lower() != user.email.lower():
        raise ValueError('Resolve the customer profile email mapping before using points.')
    mirrors = list(db.collection('customers').where('email', '==', user.email.lower()).stream())
    profiles = [profile, *[d.to_dict() for d in mirrors]]
    if any(p.get('archived') or p.get('isArchived') for p in profiles):
        raise PermissionError('Customer account is archived.')
    explicit_uids = {str(p.get('uid')) for p in profiles if p.get('uid')}
    if explicit_uids - {user.uid}:
        raise ValueError('Customer identity is ambiguous. Resolve the account mapping first.')
    role = str(profile.get('role') or 'customer').lower()
    return {'uid': user.uid, 'email': user.email.lower(), 'role': role,
            'name': profile.get('fullName') or profile.get('name') or user.display_name or user.email.split('@')[0]}


def identity(admin=False):
    header = request.headers.get('Authorization', '')
    if header:
        if not header.startswith('Bearer '):
            raise SignInRequired('Sign in again.')
        try:
            decoded = auth.verify_id_token(header[7:], check_revoked=True)
        except Exception:
            raise SignInRequired('Your session expired. Sign in again.') from None
        actor = customer_identity(uid=decoded['uid'])
    elif session.get('email'):
        actor = customer_identity(email=session['email'])
    else:
        raise SignInRequired('Sign in first.')
    if admin and actor['role'] != 'admin':
        raise PermissionError('Admin access required.')
    return actor


def api_errors(function):
    @wraps(function)
    def handle(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SignInRequired as error:
            return jsonify(success=False, message=str(error)), 401
        except auth.UserNotFoundError:
            return jsonify(success=False, message='Resolve the customer Firebase account before using points.'), 409
        except FileNotFoundError as error:
            return jsonify(success=False, message=str(error)), 404
        except PermissionError as error:
            return jsonify(success=False, message=str(error)), 403
        except (ValueError, TypeError, KeyError) as error:
            return jsonify(success=False, message=str(error)), 409
        except Exception:
            from flask import current_app
            current_app.logger.exception('Loyalty operation failed')
            return jsonify(success=False, message='Unable to confirm the operation. Reconnect and retry the same submission.'), 503
    return handle


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Send a JSON object.')
    return data


def safe_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or '/' in value or value in ['.', '..']:
        raise ValueError('Invalid record identifier.')
    return value


def register_loyalty_routes(app):
    api = Blueprint('loyalty', __name__)

    @api.get('/api/loyalty/me')
    @api_errors
    def me():
        actor = identity()
        settings = config(db)
        ref = db.collection('loyalty_accounts').document(actor['uid'])
        account = ref.get().to_dict() or {}
        balance, reserved = account_values(account)
        events = [{**doc.to_dict(), 'id': doc.id} for doc in
                  ref.collection('events').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100).stream()]
        return jsonify(success=True, uid=actor['uid'], balance=balance, reserved=reserved,
                       available=balance-reserved, pointValueCents=settings.get('pointValueCents', 100), events=events)

    @api.get('/api/loyalty/rewards')
    @api_errors
    def rewards():
        identity()
        config(db)
        records = [{**d.to_dict(), 'id': d.id} for d in db.collection('loyalty_rewards').stream()
                   if d.to_dict().get('active')]
        return jsonify(success=True, rewards=records)

    @api.post('/api/loyalty/orders')
    @api_errors
    def order():
        actor = identity()
        if actor['role'] == 'admin':
            raise PermissionError('Use POS to place a customer counter order.')
        record = submit_order(db, actor, body())
        return jsonify(success=True, orderId=record['id'], order=record)

    @api.route('/api/admin/loyalty/rewards', methods=['GET', 'POST'])
    @api.route('/api/admin/loyalty/rewards/<reward_id>', methods=['PUT'])
    @api_errors
    def manage_rewards(reward_id=None):
        identity(admin=True)
        if request.method == 'GET':
            return jsonify(success=True, rewards=[{**d.to_dict(), 'id': d.id} for d in db.collection('loyalty_rewards').stream()])
        data = body()
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '').strip()
        kind = data.get('kind')
        key = safe_key(data.get('catalogId'))
        if not name or len(name) > 100 or len(description) > 500 or kind not in ['product', 'service']:
            raise ValueError('Provide a name, description up to 500 characters, and a product or service.')
        catalog = db.collection('products' if kind == 'product' else 'services').document(key).get().to_dict() or {}
        if not catalog or catalog.get('archived') or catalog.get('isArchived'):
            raise ValueError('Select an active catalog product or service.')
        points = whole(data.get('pointsCost'), 'Points cost', 1)
        quantity = whole(data.get('quantity', 1), 'Quantity', 1, 100000)
        pages = whole(data.get('pages', 1), 'Pages', 1, 5000)
        specifications = data.get('specifications') or {}
        requirements = data.get('requirements') or {}
        if not isinstance(specifications, dict) or not isinstance(requirements, dict):
            raise ValueError('Service specifications must be objects.')
        if kind == 'service':
            validate_requirements(catalog, requirements)
            service_total(key, catalog.get('basePrice'), {'quantity': quantity, 'pages': pages, 'details': specifications})
        ref = db.collection('loyalty_rewards').document(safe_key(reward_id) if reward_id else uuid.uuid4().hex)
        ref.set({'name': name, 'description': description, 'kind': kind, 'catalogId': key,
                 'pointsCost': points, 'quantity': quantity, 'pages': pages, 'specifications': specifications,
                 'requirements': requirements, 'active': data.get('active') is True,
                 'updatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()})
        return jsonify(success=True, id=ref.id)

    @api.route('/api/admin/loyalty/settings', methods=['GET', 'PUT'])
    @api_errors
    def settings():
        identity(admin=True)
        current = config(db)
        if request.method == 'PUT':
            value = money(body().get('pointValue'))
            whole(value, 'Point value in centavos', 1, 100000)
            db.collection('settings').document('loyalty_config').update({'pointValueCents': value})
            current['pointValueCents'] = value
        return jsonify(success=True, settings=current)

    @api.get('/api/admin/loyalty/catalog')
    @api_errors
    def catalog():
        identity(admin=True)
        return jsonify(success=True, products=get_store_products(), services=get_services())

    @api.get('/api/admin/loyalty/customers')
    @api_errors
    def customers():
        identity(admin=True)
        query = str(request.args.get('q') or '').strip().lower()
        if len(query) < 2:
            return jsonify(success=True, customers=[])
        found = {}
        rate = config(db).get('pointValueCents', 100)
        for data in get_customers():
            if not any(query in str(data.get(k) or '').lower() for k in ['email', 'fullName', 'name', 'rfidUid']):
                continue
            try:
                actor = customer_identity(email=data.get('email'))
                if actor['role'] == 'admin':
                    continue
                balance = db.collection('loyalty_accounts').document(actor['uid']).get().to_dict() or {}
                total, reserved = account_values(balance)
                found[actor['uid']] = {**actor, 'available': total-reserved, 'pointValueCents': rate, 'rfidUid': data.get('rfidUid')}
            except (auth.UserNotFoundError, ValueError, PermissionError):
                continue
            if len(found) >= 20:
                break
        return jsonify(success=True, customers=list(found.values()))

    @app.before_request
    @api_errors
    def shared_mutations():
        path = request.path
        if request.method not in ['POST', 'PUT', 'GET']:
            return None
        if path == '/api/rfid/tap' and request.method == 'POST':
            actor = identity()
            data = body()
            card = str(data.get('rfidCardUid') or '')
            if not card:
                profile = db.collection('users').document(actor['uid']).get().to_dict() or {}
                card = profile.get('rfidUid', '')
            matches = []
            for collection in ['users', 'customers']:
                matches.extend(db.collection(collection).where('rfidUid', '==', card).stream())
            owners = {customer_identity(email=doc.to_dict().get('email'))['uid'] for doc in matches}
            if not card or len(owners) != 1:
                raise ValueError('Card is unassigned or ambiguous.')
            if actor['role'] != 'admin' and actor['uid'] not in owners:
                raise PermissionError('This card belongs to another customer.')
            if not log_rfid_tap(card, 0):
                raise ValueError('Card identification failed.')
            return jsonify(success=True, earnedPoints=0, message='Card identified. Points are earned on paid completed purchases.')
        if path == '/api/orders' and request.method == 'POST':
            data = body()
            if data.get('pointsUsed') or data.get('rewardId') or request.headers.get('Authorization'):
                actor = identity()
                if actor['role'] == 'admin':
                    raise PermissionError('Use POS for customer counter orders.')
                record = submit_order(db, actor, data)
                return jsonify(success=True, orderId=record['id'], order=record)
        match = re.fullmatch(r'/api/orders/([^/]+)/(status|cancel)', path)
        if match and request.method in ['POST', 'PUT']:
            actor = identity(admin=match[2] == 'status')
            data = body()
            target = 'Cancelled' if match[2] == 'cancel' else str(data.get('status') or '')
            # UID is resolved through Firebase Auth for legacy order owners.
            snapshot = orders_ref(db).document(match[1]).get()
            order_data = snapshot.to_dict() or {}
            if not order_data.get('loyaltyUid') and order_data.get('source') != 'POS':
                try:
                    owner = customer_identity(email=order_data.get('email') or order_data.get('customerEmail'))
                    if owner['role'] != 'admin':
                        actor['orderCustomerUid'] = owner['uid']
                except (auth.UserNotFoundError, ValueError, PermissionError):
                    pass
            result = apply_order_status(db, match[1], target, actor,
                                        data.get('reason', ''), data.get('paymentConfirmed', False))
            return jsonify(success=True, order=result)
        if path == '/api/pos/sales':
            actor = identity(admin=True)
            if request.method == 'GET':
                records = [{k: v for k, v in doc.to_dict().items() if k != 'recordedAt'} for doc in
                           db.collection('pos_sales').order_by('recordedAt', direction=firestore.Query.DESCENDING).limit(20).stream()]
                return jsonify(success=True, sales=records)
            data = body()
            uid = data.get('customerUid')
            customer = customer_identity(uid=safe_key(uid)) if uid else None
            if customer and customer['role'] == 'admin':
                raise ValueError('Select a customer account.')
            if customer and not str(data.get('buyerName') or '').strip():
                data['buyerName'] = customer['name']
            sale = complete_sale(db, data, actor['uid'], customer)
            return jsonify(success=True, sale={k: v for k, v in sale.items() if k != 'recordedAt'})
        return None

    app.register_blueprint(api)


# ===========================================================================
# VALIDATORS
# ===========================================================================

class ServerValidators:

    @staticmethod
    def validate_name(name):
        if not name or not str(name).strip():
            return False, "Full name is required."
        trimmed = str(name).strip()
        if len(trimmed) < 2:
            return False, "Name must be at least 2 characters long."
        if len(trimmed) > 50:
            return False, "Name cannot exceed 50 characters."
        if not re.match(r"^[a-zA-Z\s\-\.\ñ\Ñ]+$", trimmed):
            return False, "Name contains invalid characters. Letters and spaces only."
        return True, None

    @staticmethod
    def validate_email(email):
        if not email or not str(email).strip():
            return False, "Email address is required."
        trimmed = str(email).strip()
        email_regex = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_regex, trimmed):
            return False, "Invalid email address format."
        return True, None

    @staticmethod
    def validate_contact(contact):
        if not contact or not str(contact).strip():
            return False, "Contact number is required."
        trimmed = str(contact).strip()
        if not re.match(r"^09\d{9}$", trimmed):
            return False, "Contact number must be an 11-digit mobile number starting with 09."
        return True, None

    @staticmethod
    def validate_number(val, min_val=1, max_val=1000, name="Quantity"):
        if val is None or str(val).strip() == "":
            return False, f"{name} is required."
        try:
            num = float(val)
            if min_val is not None and num < min_val:
                return False, f"{name} cannot be less than {min_val}."
            if max_val is not None and num > max_val:
                return False, f"{name} cannot exceed {max_val}."
            return True, None
        except (ValueError, TypeError):
            return False, f"Please enter a valid numeric value for {name}."

    @staticmethod
    def validate_password(password):
        if not password:
            return False, "Password is required."
        if len(password) < 6:
            return False, "Password must be at least 6 characters long."
        if len(password) > 32:
            return False, "Password cannot exceed 32 characters."
        return True, None

    @staticmethod
    def validate_date(date_str):
        if not date_str or not str(date_str).strip():
            return False, "Date selection is required."
        try:
            selected_date = datetime.datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
            today = datetime.datetime.now().date()
            if selected_date < today:
                return False, "Selected date cannot be in the past."
            return True, None
        except ValueError:
            return False, "Invalid date format. Use YYYY-MM-DD."
            return False, "Invalid date format. Use YYYY-MM-DD."


# ===========================================================================
# FLASK APPLICATION AND WEB ROUTES
# ===========================================================================

load_dotenv()

app = Flask(__name__)
app.secret_key = "printingshop_secret_key"
# Allow up to 30 MB per request (25 MB combined upload limit + multipart overhead)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024



@app.before_request
def ensure_push_delivery():
    start_push_worker(db, send_fcm_push_notification)

register_loyalty_routes(app)

# ---------------------------------------------------------------------------
# ADMIN ACCOUNT CREDENTIALS
# ---------------------------------------------------------------------------
ADMIN_EMAIL = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""
# ---------------------------------------------------------------------------
# SMTP EMAIL CONFIGURATION
# ---------------------------------------------------------------------------
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")     


# In-memory storage para sa OTPs (with expiration, attempt counts, cooldowns)
otp_memory_store = {}
signup_otp_store = {}

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY") or "AIzaSyBDk0kto8_VpvtzkQBdjrVP2YHOuUZ17rQ"

def verify_password_with_firebase(email, password):
    """
    Verifies user email & password against Firebase Authentication REST API.
    Returns (True, user_data) or (False, error_code).
    """
    if not FIREBASE_API_KEY:
        return False, "NO_FIREBASE_API_KEY"
    try:
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
        res = requests.post(
            url,
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=8
        )
        if res.status_code == 200:
            return True, res.json()
        data = res.json()
        error_msg = data.get("error", {}).get("message", "INVALID_CREDENTIALS")
        return False, error_msg
    except Exception as e:
        print(f"Firebase REST signIn error: {e}")
        return False, "NETWORK_ERROR"

# ---------------------------------------------------------------------------
# BACKEND API SECURITY DECORATORS
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'email' not in session:
            return jsonify({"success": False, "status": "unauthorized", "message": "Please sign in to use this feature."}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            return jsonify({"success": False, "status": "forbidden", "message": "Admin access is required for this action."}), 403
        return f(*args, **kwargs)
    return decorated_function

# ---------------------------------------------------------------------------
# STRICT FIELD VALIDATION HELPERS (MATCHING FLUTTER APPVALIDATORS)
# ---------------------------------------------------------------------------
def validate_name(name):
    if not name or not name.strip():
        return False, "Please enter your full name."
    trimmed = name.strip()
    if len(trimmed) < 2:
        return False, "Name must contain at least 2 characters."
    if len(trimmed) > 50:
        return False, "Name must not exceed 50 characters."
    name_regex = r"^[a-zA-Z\s\-\.\ñ\Ñ]+$"
    if not re.match(name_regex, trimmed):
        return False, "Name contains an invalid character."
    return True, "Valid"

def is_valid_gmail(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@gmail\.com$'
    return bool(re.match(pattern, (email or '').strip().lower()))

def is_valid_ph_contact(contact):
    pattern = r'^09\d{9}$'
    return bool(re.match(pattern, (contact or '').strip()))

def validate_strong_password(password, full_name='', email=''):
    if not password:
        return False, "Password is required."
    if len(password) < 8:
        return False, "Password must contain at least 8 characters."
    if len(password) > 32:
        return False, "Password must not exceed 32 characters."
    if not re.search(r'[A-Z]', password) or not re.search(r'[a-z]', password):
        return False, "Password must contain uppercase and lowercase letters."
    if not re.search(r'[0-9!@#$%^&*(),.?":{}|<>_\-\+=]', password):
        return False, "Password must contain at least one number or symbol."
    if ' ' in password:
        return False, "Password must not contain spaces."

    # Flutter req5: Can't include name or email prefix
    name_lower = full_name.strip().lower() if full_name else ''
    email_prefix = email.strip().split('@')[0].lower() if email else ''
    pass_lower = password.lower()

    if name_lower and len(name_lower) >= 2 and name_lower in pass_lower:
        return False, "Password must not contain your name."
    if email_prefix and len(email_prefix) >= 2 and email_prefix in pass_lower:
        return False, "Password must not contain your email username."

    return True, "Valid"

def is_email_registered(email):
    try:
        email_clean = (email or '').strip().lower()
        if not email_clean:
            return False

        if ADMIN_EMAIL and email_clean == ADMIN_EMAIL:
            return True

        # 1. Check Firebase Authentication
        firebase_user = get_firebase_user_by_email(email_clean)
        if firebase_user:
            return True

        # 2. Check Firestore 'users' collection (used by Flutter app)
        if db:
            user_docs = db.collection('users').where('email', '==', email_clean).limit(1).get()
            if len(user_docs) > 0:
                return True

        # 3. Check Firestore 'customers' collection (used by website)
        customers = get_customers() or []
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email_clean:
                return True

        return False
    except Exception as e:
        print(f"⚠ Database Check Error: {e}")
        return False

def format_proper_order_info(order):
    try:
        legacy_labels = {'Walang napiling file': 'No file selected', 'Walang in-attach na file': 'No file attached'}
        for field in ['file', 'fileName']:
            if order.get(field) in legacy_labels:
                order[field] = legacy_labels[order[field]]
        raw_price = float(order.get('total') or order.get('price') or 0.0)
        order['formattedTotal'] = f"₱ {raw_price:,.2f}"

        raw_paper = str(order.get('paperSize') or '').lower()
        if 'short' in raw_paper:
            order['paperSizeDetailed'] = 'Short Letter (8.5" x 11") • 80 GSM'
        elif 'long' in raw_paper:
            order['paperSizeDetailed'] = 'Long Folio (8.5" x 13") • 80 GSM'
        elif 'a4' in raw_paper:
            order['paperSizeDetailed'] = 'A4 Size (8.27" x 11.69") • 80 GSM'
        elif 'tarp' in raw_paper:
            order['paperSizeDetailed'] = 'Tarpaulin (13 oz Premium Gloss)'
        else:
            order['paperSizeDetailed'] = order.get('paperSize') or 'Standard Store Supply'

        copies = int(order.get('copies') or order.get('quantity') or 1)
        pages = int(order.get('pages') or 1)
        order['quantityDetailed'] = f"{copies} {'copy' if copies == 1 else 'copies'} ({pages} {'page' if pages == 1 else 'pages'}/copy)"

        raw_color = str(order.get('colorMode') or '').lower()
        if 'full' in raw_color or 'color' in raw_color:
            order['colorModeDetailed'] = 'Full Color (High Quality Digital Print)'
        elif 'bw' in raw_color or 'black' in raw_color:
            order['colorModeDetailed'] = 'Black & White (Grayscale Monochromatic)'
        else:
            order['colorModeDetailed'] = order.get('colorMode') or 'N/A (Store Supply Item)'

        customer_note = str(order.get('customerNote') if order.get('customerNote') is not None else (order.get('note') or '')).strip()
        order['customerNote'] = customer_note
    except Exception as e:
        print(f"⚠ Formatting error: {e}")
    return order

def send_smtp_email(to_email, subject, body_html):
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = f"Kez C-Em Zek Print Shop <{SMTP_EMAIL}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body_html, 'html'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"❌ SMTP Error: {e}")
        return False

# In-memory store para sa chat messages thread
chat_memory_store = [
    {
        "id": "c1", 
        "customerId": "CUST-101", 
        "sender": "staff", 
        "message": "Hello! How can we help with your print job or order today?",
        "time": "10:00 AM"
    }
]

# ---------------------------------------------------------------------------
# PAGE ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

def sync_or_create_customer_profile(email, uid=None, full_name='', phone=''):
    """
    Ensures that a user registered via Firebase Auth (mobile or web)
    has a matching record in Firestore 'customers' (web customer portal)
    and Firestore 'users' (mobile app), preserving identity & compatibility.
    """
    email_clean = (email or '').strip().lower()
    if not email_clean:
        return None

    username_part = email_clean.split('@')[0]
    display_name = full_name.strip() if full_name else username_part
    user_uid = uid or ""

    if not user_uid:
        fb_user = get_firebase_user_by_email(email_clean)
        if fb_user:
            user_uid = fb_user.uid
            if not display_name or display_name == username_part:
                display_name = fb_user.display_name or display_name

    # Check Firestore 'users'
    user_mobile_doc = None
    if db and user_uid:
        try:
            m_doc = db.collection('users').document(user_uid).get()
            if m_doc.exists:
                user_mobile_doc = m_doc.to_dict()
                if not display_name or display_name == username_part:
                    display_name = user_mobile_doc.get('full_name') or user_mobile_doc.get('name') or display_name
                if not phone:
                    phone = user_mobile_doc.get('phone') or user_mobile_doc.get('contact') or phone
        except Exception as e:
            print(f"Error checking users collection: {e}")

    # Check Firestore 'customers'
    customers = get_customers() or []
    customer_found = None
    for c in customers:
        if isinstance(c, dict) and c.get('email', '').strip().lower() == email_clean:
            customer_found = c
            break

    now_str = datetime.datetime.now().strftime('%Y-%m-%d • %I:%M %p')

    if not customer_found:
        cust_id = f"CUST-{int(datetime.datetime.now().timestamp())}"
        new_customer = {
            "id": cust_id,
            "uid": user_uid or cust_id,
            "fullName": display_name,
            "contact": phone or "",
            "phone": phone or "",
            "email": email_clean,
            "username": username_part,
            "regDate": now_str,
            "rfidUid": f"RFID-KCZ-{random.randint(100000, 999999)}",
            "role": "customer"
        }
        create_customer(new_customer)
        customer_found = new_customer
    else:
        # Update UID if missing
        if user_uid and not customer_found.get('uid'):
            try:
                db.collection('customers').document(customer_found['id']).update({'uid': user_uid})
                customer_found['uid'] = user_uid
            except Exception as e:
                print(f"Error updating customer uid: {e}")

    # Ensure mobile Firestore 'users' record exists as well
    if db and user_uid:
        try:
            u_ref = db.collection('users').document(user_uid)
            if not u_ref.get().exists:
                u_ref.set({
                    "uid": user_uid,
                    "full_name": display_name,
                    "phone": phone or customer_found.get('contact') or "",
                    "email": email_clean,
                    "role": "customer",
                    "created_at": datetime.datetime.now(datetime.timezone.utc)
                })
        except Exception as e:
            print(f"Error ensuring mobile users record: {e}")

    return customer_found

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json or request.form or {}
        email = (data.get('email') or data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"success": False, "message": "Email and password are required."}), 400

        if email == ADMIN_EMAIL:
            if password == ADMIN_PASSWORD:
                session['username'] = "Admin"
                session['email'] = ADMIN_EMAIL
                session['role'] = 'admin'
                return jsonify({"success": True, "status": "success", "redirect": "/admin", "message": "Admin Login successful"})
            else:
                return jsonify({"success": False, "message": "Incorrect admin password."}), 400

        # Try Firebase Authentication first
        fb_valid, fb_res = verify_password_with_firebase(email, password)
        if fb_valid:
            uid = fb_res.get('localId')
            customer_profile = sync_or_create_customer_profile(email, uid=uid)
            display_name = (customer_profile.get('fullName') if customer_profile else None) or fb_res.get('displayName') or email.split('@')[0]

            session['username'] = display_name
            session['email'] = email
            session['role'] = 'customer'
            session['uid'] = uid

            return jsonify({
                "success": True,
                "status": "success",
                "redirect": "/customer",
                "message": "Login successful"
            })

        # Fallback to Firestore customer password check (for legacy customers)
        customers = get_customers() or []
        user_found = None
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                user_found = c
                break

        if user_found and str(user_found.get('password', '')) == password:
            username_clean = user_found.get('fullName') or (email.split('@')[0] if '@' in email else email)
            session['username'] = username_clean
            session['email'] = email
            session['role'] = 'customer'
            session['uid'] = user_found.get('uid', user_found.get('id'))

            return jsonify({
                "success": True,
                "status": "success",
                "redirect": "/customer",
                "message": "Login successful"
            })

        return jsonify({"success": False, "message": "Incorrect email or password."}), 400

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    return render_template('admin.html')

@app.route('/customer')
def customer_dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('customer.html')

@app.route('/order')
@app.route('/place-order')
def place_order_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('order.html')

@app.route('/store')
def store_page():
    return render_template('store.html')

# ---------------------------------------------------------------------------
# AUTHENTICATION & SMTP API ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = request.json or request.form or {}
        email = (data.get('email') or data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"success": False, "status": "error", "message": "Please enter your email and password."}), 400

        if email == ADMIN_EMAIL:
            if password == ADMIN_PASSWORD:
                session['username'] = "Admin"
                session['email'] = ADMIN_EMAIL
                session['role'] = "admin"
                return jsonify({
                    "success": True,
                    "status": "success",
                    "message": "Admin Login Successful",
                    "redirect": "/admin",
                    "redirectUrl": "/admin",
                    "user": {"email": session['email'], "role": session['role'], "username": "Admin"}
                }), 200
            else:
                return jsonify({"success": False, "status": "error", "message": "Incorrect admin password."}), 400

        # Try Firebase Authentication first
        fb_valid, fb_res = verify_password_with_firebase(email, password)
        if fb_valid:
            uid = fb_res.get('localId')
            customer_profile = sync_or_create_customer_profile(email, uid=uid)
            display_name = (customer_profile.get('fullName') if customer_profile else None) or fb_res.get('displayName') or email.split('@')[0]

            session['username'] = display_name
            session['email'] = email
            session['role'] = "customer"
            session['uid'] = uid

            return jsonify({
                "success": True,
                "status": "success",
                "message": "Login Successful",
                "redirect": "/customer",
                "redirectUrl": "/customer",
                "user": {"email": session['email'], "role": session['role'], "username": display_name}
            }), 200

        # Fallback to Firestore customer password check (for legacy customers)
        customers = get_customers() or []
        user_found = None
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                user_found = c
                break

        if not user_found:
            return jsonify({"success": False, "status": "error", "message": "This email address is not registered."}), 400

        stored_password = str(user_found.get('password', ''))
        if stored_password != password:
            return jsonify({"success": False, "status": "error", "message": "Incorrect password. Please try again."}), 400

        username_clean = user_found.get('fullName') or email.split('@')[0]
        session['username'] = username_clean
        session['email'] = email
        session['role'] = "customer"
        session['uid'] = user_found.get('uid', user_found.get('id'))

        return jsonify({
            "success": True,
            "status": "success",
            "message": "Login Successful",
            "redirect": "/customer",
            "redirectUrl": "/customer",
            "user": {"email": session['email'], "role": session['role'], "username": username_clean}
        }), 200

    except Exception as e:
        return jsonify({"success": False, "status": "error", "message": str(e)}), 400


def google_login_identity(id_token):
    """Accept only a verified Firebase session established through Google."""
    if not isinstance(id_token, str) or not id_token.strip():
        raise SignInRequired('Continue with Google again to obtain a valid sign-in token.')
    try:
        decoded = firebase_auth.verify_id_token(id_token, check_revoked=True)
    except Exception:
        raise SignInRequired('Your Google session could not be verified. Continue with Google again.') from None
    if (decoded.get('firebase', {}).get('sign_in_provider') != 'google.com'
            or decoded.get('email_verified') is not True
            or not decoded.get('uid') or not decoded.get('email')):
        raise SignInRequired('Sign in with a verified Google account to continue.')
    user = firebase_auth.get_user(decoded['uid'])
    if user.disabled:
        raise PermissionError('This account is disabled. Contact the shop administrator.')
    email = str(decoded['email']).strip().lower()
    if not user.email or user.email.strip().lower() != email:
        raise ValueError('Your account needs identity review. Contact the shop administrator.')
    return user.uid, email, user.display_name or decoded.get('name') or email.split('@')[0]


def sync_google_profile(uid, email, display_name):
    """Provision both profile copies together without replacing existing account data."""
    user_ref = db.collection('users').document(uid)
    account_ref = db.collection('loyalty_accounts').document(uid)

    @firestore.transactional
    def apply(transaction):
        canonical = user_ref.get(transaction=transaction)
        profile = canonical.to_dict() or {}
        # Normalize legacy email casing as well. These are small profile collections.
        matches = {}
        for collection in ['users', 'customers']:
            matches[collection] = [snapshot for snapshot in db.collection(collection).stream(transaction=transaction)
                                   if str((snapshot.to_dict() or {}).get('email') or '').strip().lower() == email]
        for snapshot in db.collection('archives').stream(transaction=transaction):
            archived = snapshot.to_dict() or {}
            data = archived.get('data') or {}
            if (archived.get('itemType') == 'user' and
                    (data.get('uid') == uid or str(data.get('email') or '').strip().lower() == email)):
                raise PermissionError('This account is archived. Contact the shop administrator.')
        documents = [canonical, *matches['users'], *matches['customers']]
        for snapshot in documents:
            data = snapshot.to_dict() or {}
            if (data.get('archived') or data.get('isArchived') or data.get('disabled')
                    or data.get('isDisabled') or str(data.get('status', '')).lower() in ['archived', 'disabled']):
                raise PermissionError('This account is archived or disabled. Contact the shop administrator.')
            if data.get('uid') and data['uid'] != uid:
                raise ValueError('Your account has conflicting identities. Contact the shop administrator to resolve it.')
            if data.get('loyaltyUid') and data['loyaltyUid'] != uid:
                raise ValueError('Your points account needs identity review. Contact the shop administrator.')
        if (profile.get('email') and str(profile['email']).strip().lower() != email
                or len(matches['customers']) > 1
                or any(s.id != uid and (s.to_dict() or {}).get('uid') != uid for s in matches['users'])):
            raise ValueError('Your account has an ambiguous legacy profile. Contact the shop administrator to resolve it.')
        mirror = matches['customers'][0] if matches['customers'] else None
        if mirror and profile.get('customerId') and str(profile['customerId']) != mirror.id:
            raise ValueError('Your customer profile has conflicting identifiers. Contact the shop administrator to resolve it.')
        customer_id = mirror.id if mirror else str(profile.get('customerId') or uid)
        if '/' in customer_id:
            raise ValueError('Your customer profile needs identity review. Contact the shop administrator.')
        mirror_ref = db.collection('customers').document(customer_id)
        mirror_data = (mirror.to_dict() or {}) if mirror else (mirror_ref.get(transaction=transaction).to_dict() or {})
        if not mirror and mirror_data:
            raise ValueError('Your customer profile needs identity review. Contact the shop administrator.')
        account = account_ref.get(transaction=transaction)
        role = str(profile.get('role') or 'customer').lower()
        if role not in ['customer', 'admin']:
            raise PermissionError('This account role is unavailable. Contact the shop administrator.')
        existing = {**mirror_data, **profile}
        name = (existing.get('fullName') or existing.get('full_name') or existing.get('name') or display_name)
        shared = {'uid': uid, 'email': email, 'role': role, 'customerId': customer_id,
                  'fullName': name, 'full_name': name}
        # Fill missing fields only. RFID, historical IDs and point mirrors stay intact.
        user_data = {**existing, **shared}
        user_data.pop('password', None)
        user_data.pop('passwordHash', None)
        mirror_updates = {**shared}
        for key in ['rfidUid', 'contact', 'phone', 'points', 'loyaltyPoints', 'loyaltyUid']:
            if key in existing and key not in mirror_data:
                mirror_updates[key] = existing[key]
        new_account = not canonical.exists and not matches['users'] and not mirror
        if new_account:
            user_data.update(points=0, loyaltyPoints=0, created_at=firestore.SERVER_TIMESTAMP)
            mirror_updates.update(points=0, loyaltyPoints=0, created_at=firestore.SERVER_TIMESTAMP)
            if not account.exists:
                transaction.set(account_ref, {'balance': 0, 'reserved': 0, 'createdAt': firestore.SERVER_TIMESTAMP})
        transaction.set(user_ref, user_data, merge=True)
        transaction.set(mirror_ref, mirror_updates, merge=True)
        return {'uid': uid, 'email': email, 'role': role, 'username': name}

    return apply(db.transaction())


@app.route('/api/google-login', methods=['POST'])
@api_errors
def api_google_login():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise SignInRequired('Continue with Google again to sign in.')
    uid, email, name = google_login_identity(data.get('idToken'))
    user = sync_google_profile(uid, email, name)
    session.clear()
    session.update(uid=user['uid'], username=user['username'], email=user['email'], role=user['role'])
    destination = '/admin' if user['role'] == 'admin' else '/customer'
    return jsonify(success=True, status='success', message='Google sign-in successful.',
                   redirect=destination, redirectUrl=destination, uid=user['uid'], role=user['role'], user=user)


@app.route('/api/request-signup-otp', methods=['POST'])
def request_signup_otp():
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "message": "Please enter your email address."}), 400

        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Please enter a valid @gmail.com address."}), 400

        if is_email_registered(email):
            return jsonify({"success": False, "message": "This email address is already registered."}), 400

        now = datetime.datetime.now()

        # Rate limiting: 60 seconds cooldown between resend requests
        existing = signup_otp_store.get(email)
        if existing:
            last_sent = existing.get("timestamp")
            if last_sent and (now - last_sent).total_seconds() < 60:
                wait_sec = int(60 - (now - last_sent).total_seconds())
                return jsonify({
                    "success": False,
                    "message": f"Please wait {wait_sec} seconds before requesting another verification code."
                }), 429

        otp = ''.join(random.choices(string.digits, k=6))
        signup_otp_store[email] = {
            "otp": otp,
            "timestamp": now,
            "attempts": 0,
            "expires_at": now + datetime.timedelta(minutes=5)
        }

        # Match mobile app email template and subject
        otp_html = f"""
        <div style="font-family: Arial, sans-serif; padding: 24px; background-color: #0b0f19; color: #ffffff; border-radius: 12px; border: 1px solid #1e293b; max-width: 500px;">
          <h2 style="color: #6366f1; margin-top: 0;">Kez C-Em Zek Printing Shop</h2>
          <p style="color: #cbd5e1;">Hello,</p>
          <p style="color: #cbd5e1;">Your 6-digit account verification code is:</p>
          <div style="background-color: #111827; padding: 14px 24px; border-radius: 8px; display: inline-block; margin: 12px 0;">
            <h1 style="color: #38bdf8; letter-spacing: 6px; margin: 0; font-size: 32px;">{otp}</h1>
          </div>
          <p style="color: #94a3b8; font-size: 12px;">This code will expire in 5 minutes. If you did not request this, please ignore this email.</p>
        </div>
        """

        email_sent = send_smtp_email(email, "Account Verification - Kez C-Em Zek", otp_html)

        if email_sent:
            return jsonify({
                "success": True, 
                "message": f"A 6-digit verification code has been sent to {email}."
            }), 200
        else:
            return jsonify({"success": False, "message": "Unable to send the verification email. Please check the SMTP configuration."}), 500

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/signup', methods=['POST'])
def api_signup():
    try:
        data = request.json or request.form or {}
        email = data.get('email', '').strip().lower()
        full_name = (data.get('fullName') or f"{data.get('firstName', '')} {data.get('lastName', '')}").strip()
        contact = data.get('contact', '').strip()
        password = data.get('password', '').strip()
        confirm_password = data.get('confirmPassword', '').strip()
        otp_input = data.get('otp', '').strip()

        # Strict Name Validation (AppValidators)
        is_name_valid, name_msg = validate_name(full_name)
        if not is_name_valid:
            return jsonify({"success": False, "message": name_msg}), 400

        # Strict Contact Validation (PH 11 digits starting with 09)
        if not is_valid_ph_contact(contact):
            return jsonify({"success": False, "message": "Enter an 11-digit Philippine mobile number (for example, 09123456789)."}), 400

        # Strict Gmail Validation
        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Please enter a valid @gmail.com address."}), 400

        if not confirm_password:
            return jsonify({"success": False, "message": "Please confirm your password."}), 400

        if password != confirm_password:
            return jsonify({"success": False, "message": "Passwords do not match."}), 400

        # Strict Strong Password Validation (Matching Flutter Requirements)
        is_strong, pass_msg = validate_strong_password(password, full_name=full_name, email=email)
        if not is_strong:
            return jsonify({"success": False, "message": f"Password requirement error: {pass_msg}"}), 400

        # Pre-check email existence
        if is_email_registered(email):
            return jsonify({"success": False, "message": "An account with this email address already exists."}), 400

        # Validate OTP, expiration, and attempt limits
        stored = signup_otp_store.get(email)
        if not stored:
            return jsonify({
                "success": False, 
                "message": "Verification is missing or has expired. Please request a new code."
            }), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            signup_otp_store.pop(email, None)
            return jsonify({
                "success": False,
                "message": "The verification code has expired after 5 minutes. Please request a new code."
            }), 400

        # Limit verification attempts to 5
        stored["attempts"] = stored.get("attempts", 0) + 1
        if stored["attempts"] > 5:
            signup_otp_store.pop(email, None)
            return jsonify({
                "success": False,
                "message": "Too many verification attempts. Please request a new code."
            }), 429

        if stored['otp'] != otp_input:
            remaining_attempts = max(0, 5 - stored["attempts"])
            return jsonify({
                "success": False,
                "message": f"Incorrect verification code. ({remaining_attempts} attempts remaining)"
            }), 400

        # OTP is verified! Remove from store to prevent replay attacks
        signup_otp_store.pop(email, None)

        # 1. Create account in Firebase Authentication
        firebase_uid = create_firebase_customer(email, password)
        if not firebase_uid:
            return jsonify({
                "success": False,
                "message": "Unable to create the account. The email address may already be registered."
            }), 400

        # Set Firebase Auth Display Name
        try:
            firebase_auth.update_user(firebase_uid, display_name=full_name)
        except Exception as e:
            print(f"Warning updating display name in Auth: {e}")

        # 2. Save customer record in Firestore 'customers' collection (for Web portal)
        username_clean = email.split('@')[0]
        cust_id = f"CUST-{int(datetime.datetime.now().timestamp())}"
        new_customer = {
            "id": cust_id,
            "uid": firebase_uid,
            "fullName": full_name,
            "contact": contact,
            "phone": contact,
            "email": email,
            "username": username_clean,
            "regDate": datetime.datetime.now().strftime('%Y-%m-%d • %I:%M %p'),
            "rfidUid": f"RFID-KCZ-{random.randint(100000, 999999)}",
            "role": "customer"
        }

        if not create_customer(new_customer):
            delete_firebase_customer(firebase_uid)
            return jsonify({
                "success": False,
                "message": "Unable to save the customer profile."
            }), 500

        # 3. Save matching record in Firestore 'users' collection (for Mobile App compatibility)
        if db:
            try:
                db.collection('users').document(firebase_uid).set({
                    "uid": firebase_uid,
                    "full_name": full_name,
                    "phone": contact,
                    "email": email,
                    "role": "customer",
                    "created_at": datetime.datetime.now(datetime.timezone.utc)
                })
            except Exception as dbe:
                print(f"Warning syncing users collection: {dbe}")

        welcome_html = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px; max-width: 500px;">
            <h2 style="color: #6366f1;">Welcome sa Kez C-Em Zek Print Shop! 🎉</h2>
            <p>Hi <b>{full_name}</b>,</p>
            <p>Your new account has been verified and registered successfully.</p>
            <br>
            <p><b>Registered Email:</b> {email}</p>
            <p><b>Contact Number:</b> {contact}</p>
        </div>
        """
        send_smtp_email(email, "Welcome sa Kez C-Em Zek Print Shop!", welcome_html)

        return jsonify({"success": True, "message": "Registration successful! You can now log in."}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "message": "Please enter your email address."}), 400

        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Please enter a valid @gmail.com address."}), 400

        if not is_email_registered(email):
            return jsonify({"success": False, "message": "This email address is not registered."}), 400

        now = datetime.datetime.now()

        # Rate limiting: 60 seconds cooldown between resend requests
        existing = otp_memory_store.get(email)
        if existing:
            last_sent = existing.get("timestamp")
            if last_sent and (now - last_sent).total_seconds() < 60:
                wait_sec = int(60 - (now - last_sent).total_seconds())
                return jsonify({
                    "success": False,
                    "message": f"Please wait {wait_sec} seconds before requesting another verification code."
                }), 429

        otp = ''.join(random.choices(string.digits, k=6))
        otp_memory_store[email] = {
            "otp": otp,
            "timestamp": now,
            "attempts": 0,
            "expires_at": now + datetime.timedelta(minutes=5),
            "verified": False
        }

        # Mobile app matching email layout
        email_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #0b0f19; color: #ffffff; border-radius: 10px; max-width: 500px;">
          <h2 style="color: #6366f1;">Kez C-Em Zek Printing Shop</h2>
          <p style="color: #cbd5e1;">Hello,</p>
          <p style="color: #cbd5e1;">Your password reset code is:</p>
          <div style="background-color: #111827; padding: 12px 20px; border-radius: 8px; display: inline-block; margin: 10px 0;">
            <h1 style="color: #38bdf8; letter-spacing: 5px; margin: 0;">{otp}</h1>
          </div>
          <p style="color: #94a3b8; font-size: 12px;">This code will expire in 5 minutes. If you did not request this, please ignore this email.</p>
        </div>
        """

        email_sent = send_smtp_email(email, "Password Reset OTP - Kez C-Em Zek", email_body)

        if email_sent:
            return jsonify({"success": True, "message": f"A password reset code has been sent to {email}."}), 200
        else:
            return jsonify({"success": False, "message": "Bigo sa pagpapadala ng OTP via SMTP."}), 500

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/verify-reset-otp', methods=['POST'])
def verify_reset_otp():
    """
    Step 2 verification endpoint: Validates OTP before user provides new password.
    Marks OTP session as verified.
    """
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()
        otp_input = data.get('otp', '').strip()

        if not email or not otp_input:
            return jsonify({"success": False, "message": "Please enter your email address and 6-digit verification code."}), 400

        stored = otp_memory_store.get(email)
        if not stored:
            return jsonify({"success": False, "message": "There is no active verification request, or it has expired."}), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "The verification code has expired after 5 minutes. Please request a new code."}), 400

        stored["attempts"] = stored.get("attempts", 0) + 1
        if stored["attempts"] > 5:
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "Too many attempts. Please request a new code."}), 429

        if stored['otp'] != otp_input:
            remaining = max(0, 5 - stored["attempts"])
            return jsonify({"success": False, "message": f"Incorrect verification code. ({remaining} attempts remaining)"}), 400

        # Mark as verified
        stored['verified'] = True
        return jsonify({"success": True, "message": "Code verified successfully. You can now enter your new password."}), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()
        otp_input = data.get('otp', '').strip()
        new_password = data.get('newPassword', '').strip()

        if not email or not otp_input or not new_password:
            return jsonify({"success": False, "message": "Please complete all required fields."}), 400

        is_strong, pass_msg = validate_strong_password(new_password, email=email)
        if not is_strong:
            return jsonify({"success": False, "message": f"The new password does not meet the requirements. {pass_msg}"}), 400

        if email == ADMIN_EMAIL:
            stored_admin = otp_memory_store.get(email)
            if stored_admin and (stored_admin.get('verified') or stored_admin['otp'] == otp_input):
                global ADMIN_PASSWORD
                ADMIN_PASSWORD = new_password
                otp_memory_store.pop(email, None)
                return jsonify({"success": True, "message": "Admin password updated successfully."}), 200

        stored = otp_memory_store.get(email)
        if not stored:
            return jsonify({"success": False, "message": "There is no active verification request, or it has expired."}), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "The verification code has expired. Please request a new code."}), 400

        if not stored.get('verified') and stored['otp'] != otp_input:
            stored["attempts"] = stored.get("attempts", 0) + 1
            if stored["attempts"] > 5:
                otp_memory_store.pop(email, None)
                return jsonify({"success": False, "message": "Too many verification attempts."}), 429
            return jsonify({"success": False, "message": "The verification code is incorrect."}), 400

        # 1. Update REAL Firebase Authentication password
        fb_user = get_firebase_user_by_email(email)
        auth_updated = False
        if fb_user:
            auth_updated = update_firebase_password(fb_user.uid, new_password)
            if not auth_updated:
                return jsonify({"success": False, "message": "Unable to update the password."}), 500

        # 2. Update Firestore 'customers' password field (legacy backward compatibility)
        updated_in_db = False
        try:
            if db:
                customers_ref = db.collection('customers')
                docs = customers_ref.where('email', '==', email).stream()
                for doc in docs:
                    doc.reference.update({'password': new_password})
                    updated_in_db = True
        except Exception as db_err:
            print(f"⚠ Firebase Customers Update Error: {db_err}")

        # In-memory customer list update
        customers = get_customers() or []
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                c['password'] = new_password
                updated_in_db = True
                break

        # Consume OTP session
        otp_memory_store.pop(email, None)

        if auth_updated or updated_in_db:
            return jsonify({"success": True, "message": "Password updated successfully. You can now sign in with your new password."}), 200
        else:
            return jsonify({"success": False, "message": "The account could not be found."}), 400

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ---------------------------------------------------------------------------
# REGISTERED USERS MANAGEMENT ENDPOINTS (UPDATED FOR DELETION)
# ---------------------------------------------------------------------------

@app.route('/api/users', methods=['GET', 'POST'])
@admin_required
def handle_users():
    if request.method == 'GET':
        try:
            customers = get_customers() or []
            users_list = []

            # Suportahan ang parehong list o dictionary format mula sa Firebase database
            if isinstance(customers, dict):
                for uid, c in customers.items():
                    if isinstance(c, dict):
                        c['id'] = c.get('id') or uid
                        users_list.append({
                            "id": c.get('id'),
                            "name": c.get('fullName') or c.get('name') or c.get('username') or 'N/A',
                            "email": c.get('email', 'N/A'),
                            "phone": c.get('contact') or c.get('phone', 'N/A'),
                            "dateJoined": c.get('regDate') or c.get('created_at') or 'Oct 01, 2026',
                            "role": c.get('role', 'customer'),
                            "rfidUid": c.get("rfidUid", ""),
                            "points": c.get("points", c.get("loyaltyPoints", 0)),
                            "lastTap": c.get("lastTap", "")
                        })
            elif isinstance(customers, list):
                for idx, c in enumerate(customers):
                    if isinstance(c, dict):
                        c_id = c.get('id') or str(idx)
                        users_list.append({
                            "id": c_id,
                            "name": c.get('fullName') or c.get('name') or c.get('username') or 'N/A',
                            "email": c.get('email', 'N/A'),
                            "phone": c.get('contact') or c.get('phone', 'N/A'),
                            "dateJoined": c.get('regDate') or c.get('created_at') or 'Oct 01, 2026',
                            "role": c.get('role', 'customer'),
                            "rfidUid": c.get("rfidUid", ""),
                            "points": c.get("points", c.get("loyaltyPoints", 0)),
                            "lastTap": c.get("lastTap", "")
                        })

            return jsonify(users_list), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'POST':
        # CREATE NEW USER (ADMIN CRUD)
        try:
            data = request.json or {}
            full_name = (data.get('name') or data.get('fullName') or '').strip()
            email = (data.get('email') or '').strip().lower()
            phone = (data.get('phone') or data.get('contact') or '').strip()
            password = data.get('password') or 'KczUser2026!'

            if not full_name or not email:
                return jsonify({"success": False, "message": "Name and Gmail address are required."}), 400

            if not email.endswith('@gmail.com'):
                return jsonify({"success": False, "message": "Only @gmail.com accounts are allowed."}), 400

            if any(c.get('email', '').strip().lower() == email for c in get_customers()):
                return jsonify({'success': False, 'message': 'A customer with this email already exists.'}), 409
            user_id = f"CUST-{uuid.uuid4().hex[:12]}"
            new_customer = {
                "id": user_id,
                "fullName": full_name,
                "email": email,
                "contact": phone or "09171234567",
                "phone": phone or "09171234567",
                "username": full_name.split()[0].lower() if full_name else "user",
                "regDate": datetime.datetime.now().strftime("%Y-%m-%d"),
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "password": password,
                "role": "customer",
                "rfidUid": f"RFID-KCZ-{random.randint(100000, 999999)}"
            }

            # Save in Firestore
            try:
                if db:
                    sync_customer_profile(user_id, new_customer)
                else:
                    return jsonify({'success': False, 'message': 'Database not connected.'}), 503
            except Exception as dbe:
                return jsonify({"success": False, "message": "Customer save failed. Please retry."}), 500

            return jsonify({
                "success": True,
                "status": "success",
                "message": f"Successfully created user account for {full_name}!",
                "user": new_customer
            }), 201
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/users/<user_id>', methods=['GET', 'PUT', 'DELETE'])
@admin_required
def handle_single_user(user_id):
    if request.method == 'GET':
        try:
            if db:
                doc = db.collection('customers').document(user_id).get()
                if doc.exists:
                    d = doc.to_dict()
                    d['id'] = doc.id
                    return jsonify({"success": True, "user": d}), 200
            return jsonify({"success": False, "message": "User not found"}), 404
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'PUT':
        # UPDATE EXISTING USER (ADMIN CRUD)
        try:
            data = request.json or {}
            update_data = {}
            if 'name' in data or 'fullName' in data:
                name_val = data.get('name') or data.get('fullName')
                update_data['fullName'] = name_val
                update_data['name'] = name_val
            if 'email' in data:
                update_data['email'] = data['email'].strip().lower()
            if 'phone' in data or 'contact' in data:
                phone_val = data.get('phone') or data.get('contact')
                update_data['phone'] = phone_val
                update_data['contact'] = phone_val
            if 'password' in data and data['password']:
                update_data['password'] = data['password']

            updated = False
            if db:
                # Direct document update
                doc_ref = db.collection('customers').document(user_id)
                if doc_ref.get().exists:
                    sync_customer_profile(user_id, {**doc_ref.get().to_dict(), **update_data})
                    updated = True
                else:
                    # Query by 'id' field
                    docs = db.collection('customers').where('id', '==', user_id).stream()
                    for doc in docs:
                        sync_customer_profile(doc.id, {**doc.to_dict(), **update_data})
                        updated = True

            return jsonify({
                "success": True,
                "status": "success",
                "message": f"User {user_id} profile updated successfully!"
            }), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    elif request.method == 'DELETE':
        # ARCHIVE USER (soft-delete - moves to System Archive, not permanent)
        try:
            user_data = {}

            # 1. Fetch user data before archiving
            try:
                if db:
                    doc_ref = db.collection('customers').document(user_id)
                    doc = doc_ref.get()
                    if doc.exists:
                        user_data = doc.to_dict()
                    else:
                        customers_ref = db.collection('customers')
                        docs = customers_ref.where('id', '==', user_id).stream()
                        for d in docs:
                            user_data = d.to_dict()
                            break
            except Exception as fetch_err:
                print(f"Could not fetch user data before archiving: {fetch_err}")

            user_data['id'] = user_id

            # 2. Archive the user to System Archives
            if not archive_item('user', user_id, user_data, 'admin_users'):
                return jsonify({'success': False, 'message': 'Archive failed; no records were removed.'}), 409

            return jsonify({"success": True, "status": "success", "message": f"User {user_id} has been moved to the System Archive."}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# STORE PRODUCTS ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/store/products', methods=['GET', 'PUT'])
def handle_store_products():
    if request.method == 'GET':
        try:
            products = get_store_products() or []
            return jsonify(products), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'PUT':
        if session.get('role') != 'admin':
            return jsonify({"success": False, "message": "Admin access is required to update store inventory."}), 403

        try:
            data = request.json or {}
            products_list = data.get('products', [])
            success = save_store_products(products_list)
            return jsonify({"success": success, "message": "Store inventory updated successfully!" if success else "Inventory update failed; refresh and retry."}), 200 if success else 409
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# SAMPLES & SERVICES SHOWCASE ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/samples', methods=['GET', 'POST'])
def handle_samples():
    if request.method == 'GET':
        try:
            samples = get_sample_products() or []
            return jsonify(samples), 200
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'POST':
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'message': 'Admin access is required to add sample showcase items.'}), 403
        try:
            data = request.json or {}
            saved = save_sample_product(data)
            if saved:
                return jsonify({'success': True, 'message': 'Sample product or service added successfully.', 'item': saved}), 200
            return jsonify({'success': False, 'message': 'Failed to save sample product.'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/samples/<sample_id>', methods=['DELETE', 'PUT'])
def handle_single_sample(sample_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Admin access is required to manage samples.'}), 403
    try:
        if request.method == 'DELETE':
            # Retrieve sample data for archive
            sample_data = None
            try:
                samples = get_sample_products() or []
                for s in samples:
                    if str(s.get('id')) == str(sample_id):
                        sample_data = s
                        break
            except Exception:
                pass
            if not sample_data:
                sample_data = {'id': sample_id, 'title': sample_id}

            if not archive_item('sample_product', sample_id, sample_data, 'admin_samples'):
                return jsonify({'success': False, 'message': 'Archive failed; no records were removed.'}), 409
            return jsonify({'success': True, 'message': 'Sample moved to Archive successfully.'}), 200
        elif request.method == 'PUT':
            data = request.json or {}
            data['id'] = sample_id
            saved = save_sample_product(data)
            return jsonify({'success': bool(saved), 'item': saved}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# SERVICES ENDPOINTS (Printing Services for customer order form)
# ---------------------------------------------------------------------------

@app.route('/api/services', methods=['GET', 'POST'])
def handle_services():
    if request.method == 'GET':
        try:
            services = get_services() or []
            return jsonify(services), 200
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'POST':
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'message': 'Admin access is required to add services.'}), 403
        try:
            data = request.json or {}
            saved = save_service(data)
            if saved:
                return jsonify({'success': True, 'message': 'Service added successfully.', 'item': saved}), 200
            return jsonify({'success': False, 'message': 'Failed to save service.'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/services/<service_id>', methods=['DELETE', 'PUT'])
def handle_single_service(service_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Admin access is required to manage services.'}), 403
    try:
        if request.method == 'DELETE':
            service_data = None
            try:
                services = get_services() or []
                for s in services:
                    if str(s.get('id')) == str(service_id):
                        service_data = s
                        break
            except Exception:
                pass
            if not service_data:
                service_data = {'id': service_id, 'name': service_id}

            if not archive_item('service', service_id, service_data, 'admin_services'):
                return jsonify({'success': False, 'message': 'Archive failed; no records were removed.'}), 409
            return jsonify({'success': True, 'message': 'Printing service moved to Archive successfully.'}), 200
        elif request.method == 'PUT':
            data = request.json or {}
            data['id'] = service_id
            saved = save_service(data)
            return jsonify({'success': bool(saved), 'item': saved}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# GCASH PAYMENT SETTINGS ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/settings/gcash', methods=['GET', 'POST'])
def handle_gcash_settings():
    if request.method == 'GET':
        try:
            settings = get_gcash_settings()
            return jsonify({'success': True, 'settings': settings}), 200
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif request.method == 'POST':
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
        try:
            data = request.json or {}
            acc_name = str(data.get('accountName') or '').strip()
            acc_number = str(data.get('accountNumber') or '').strip()
            qr_url = str(data.get('qrUrl') or '/static/images/gcash_qr.png').strip()

            if not acc_name or not acc_number:
                return jsonify({'success': False, 'message': 'Account name and GCash number are required.'}), 400

            # Clean number: strip dashes/spaces
            clean_num = re.sub(r'\D', '', acc_number)
            if not is_valid_ph_contact(clean_num):
                return jsonify({'success': False, 'message': 'Enter an 11-digit Philippine mobile number starting with 09.'}), 400

            updated_data = {
                "accountName": acc_name,
                "accountNumber": clean_num,
                "qrUrl": qr_url,
                "isActive": bool(data.get('isActive', True))
            }
            saved = save_gcash_settings(updated_data)
            return jsonify({'success': True, 'message': 'GCash payment details updated successfully!', 'settings': saved}), 200
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/settings/gcash/upload-qr', methods=['POST'])
def upload_gcash_qr_endpoint():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        if 'qrFile' not in request.files:
            return jsonify({'success': False, 'message': 'No file uploaded.'}), 400
        file = request.files['qrFile']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected.'}), 400

        # Save locally to static/images/gcash_qr.png or timestamped
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
        if ext not in ['png', 'jpg', 'jpeg', 'webp']:
            return jsonify({'success': False, 'message': 'Image formats only (PNG, JPG, WEBP).'}), 400

        save_name = f"gcash_qr.{ext}"
        save_path = os.path.join(os.path.dirname(__file__), 'static', 'images', save_name)
        file.save(save_path)

        web_url = f"/static/images/{save_name}?v={int(datetime.datetime.now().timestamp())}"

        # Auto update settings
        current = get_gcash_settings()
        current['qrUrl'] = web_url
        save_gcash_settings(current)

        return jsonify({'success': True, 'qrUrl': web_url, 'message': 'GCash QR Code updated successfully!'}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# FILE UPLOAD ENDPOINT  (Step 1 of two-step order submission)
# ---------------------------------------------------------------------------

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_order_file_endpoint():
    """
    Accept a multipart/form-data file upload, validate it server-side,
    upload it to Supabase Storage, and return the storage path + signed URL.

    The frontend calls this BEFORE submitting the order JSON so that
    no orphaned order records are created if the upload fails.
    """
    try:
        files = request.files.getlist('file') or request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'success': False, 'message': 'No file was sent. Please attach a file.'}), 400

        # Enforce max files per request
        if len(files) > sc.MAX_FILES_PER_REQUEST:
            return jsonify({
                'success': False,
                'message': f'Too many files. Maximum {sc.MAX_FILES_PER_REQUEST} files per upload.'
            }), 400

        # Validate each file individually
        validated = []
        combined_size = 0
        for f in files:
            ok, err = sc.validate_upload_file(f)
            if not ok:
                return jsonify({'success': False, 'message': err}), 400
            f.seek(0)
            size = len(f.read())
            f.seek(0)
            combined_size += size
            validated.append(f)

        # Combined size limit
        if combined_size > sc.MAX_COMBINED_SIZE_BYTES:
            mb = combined_size / (1024 * 1024)
            return jsonify({
                'success': False,
                'message': f'Total upload size ({mb:.1f} MB) exceeds the 25 MB combined limit.'
            }), 400

        # Upload to Supabase (use a temporary ID; the real order ID comes later)
        cust_email = session.get('email') or 'unknown'
        temp_id = f"tmp-{cust_email.split('@')[0]}-{datetime.datetime.now().strftime('%H%M%S%f')[:12]}"

        results = []
        uploaded_paths = []
        try:
            for f in validated:
                path, signed_url = sc.upload_order_file(
                    file_obj=f,
                    order_id=temp_id,
                    customer_id=cust_email,
                )
                uploaded_paths.append(path)
                results.append({
                    'fileName': f.filename,
                    'storagePath': path,
                    'fileUrl': signed_url,
                })
        except RuntimeError as upload_err:
            # Clean up any partially uploaded files
            for p in uploaded_paths:
                sc.delete_file(p)
            return jsonify({'success': False, 'message': str(upload_err)}), 500

        # Return single-file response if only one file (most common case)
        if len(results) == 1:
            return jsonify({
                'success': True,
                'fileName': results[0]['fileName'],
                'storagePath': results[0]['storagePath'],
                'fileUrl': results[0]['fileUrl'],
            }), 200

        return jsonify({'success': True, 'files': results}), 200

    except Exception as e:
        return jsonify({'success': False, 'message': f'Upload error: {str(e)}'}), 500


# ---------------------------------------------------------------------------
# ORDERS & ANALYTICS ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/pos/sales', methods=['GET', 'POST'])
def pos_sales():
    if not session.get('username'):
        return jsonify(success=False, message='Sign in first.'), 401
    if session.get('role') != 'admin':
        return jsonify(success=False, message='Admin access required.'), 403
    if db is None:
        return jsonify(success=False, message='POS requires an online connection.'), 503
    try:
        if request.method == 'GET':
            from firebase_admin import firestore
            snapshots = db.collection('pos_sales').order_by('recordedAt', direction=firestore.Query.DESCENDING).limit(20).stream()
            return jsonify(success=True, sales=[{k: v for k, v in d.to_dict().items() if k != 'recordedAt'} for d in snapshots])
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(success=False, message='Invalid sale data.'), 400
        sale = complete_sale(db, data, session.get('uid') or session.get('email') or 'admin')
        sale.pop('recordedAt', None)
        return jsonify(success=True, sale=sale)
    except (ValueError, TypeError) as error:
        return jsonify(success=False, message=str(error)), 409
    except PermissionError as error:
        return jsonify(success=False, message=str(error)), 403
    except Exception:
        app.logger.exception('POS operation failed')
        return jsonify(success=False, message='Connection interrupted. Retry this same sale to confirm its receipt.'), 503

@app.route('/api/orders', methods=['GET', 'POST'])
@login_required
def handle_orders():
    if request.method == 'POST':
        try:
            # File was already uploaded via /api/upload — we only receive JSON here
            data = request.json or {}

            file_name    = str(data.get('file', '')).strip()
            file_url_val = str(data.get('fileUrl', '')).strip()
            storage_path = str(data.get('storagePath', '')).strip()

            if not file_name:
                return jsonify({"success": False, "message": "Upload a document or design file before submitting your order."}), 400

            # Sanity-check: storagePath must start with expected prefix if provided
            if storage_path and not storage_path.startswith('orders/'):
                return jsonify({"success": False, "message": "Invalid file reference. Please re-upload the file."}), 400

            try:
                qty_val = int(data.get('quantity') or data.get('copies') or 1)
                pages_val = int(data.get('pages') or 1)
                total_amount = float(data.get('totalAmount') or data.get('total') or 0.0)

                if qty_val <= 0 or pages_val <= 0:
                    return jsonify({"success": False, "message": "Quantity and pages must be greater than zero."}), 400
                if qty_val > 100000:
                    return jsonify({"success": False, "message": "The maximum quantity is 100,000 units."}), 400
                if pages_val > 5000:
                    return jsonify({"success": False, "message": "The maximum page count is 5,000."}), 400
                if total_amount <= 0 or total_amount > 5000000:
                    return jsonify({"success": False, "message": "Invalid order total. The maximum is ₱5,000,000."}), 400
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "Check the numeric values for quantity, pages, and total."}), 400

            order_id = f"PRNT-{uuid.uuid4().hex[:20]}"
            cust_name = session.get('username') or data.get('customer') or 'Customer'
            if '@' in cust_name: cust_name = cust_name.split('@')[0]
            cust_email = session.get('email') or data.get('email') or f"{cust_name}@gmail.com"

            service_id = str(data.get('serviceId') or '')
            if service_id:
                matched_service = next((svc for svc in get_services() if svc['id'] == service_id), None)
                if not matched_service:
                    return jsonify({'success': False, 'message': 'Service is unavailable. Refresh the service list.'}), 400
                try:
                    validate_requirements(matched_service, data.get('customRequirements') or {})
                except ValueError as validation_error:
                    return jsonify({'success': False, 'message': str(validation_error)}), 400

            # Customer Note / Special Instructions (Optional, max 500 chars)
            customer_note = str(data.get('customerNote') if data.get('customerNote') is not None else (data.get('note') or '')).strip()
            if len(customer_note) > 500:
                return jsonify({"success": False, "message": "Order notes and special instructions must not exceed 500 characters."}), 400

            # GCash Payment Fields Validation (if GCash Online is selected)
            payment_method = str(data.get('payment') or 'GCash Online').strip()
            gcash_ref = str(data.get('gcashRef') or data.get('referenceNumber') or '').strip()
            gcash_receipt_url = str(data.get('gcashReceiptUrl') or '').strip()

            merchant_account = ''
            try:
                payment_method, gcash_ref = validate_payment(payment_method, gcash_ref)
                if 'gcash' in payment_method.lower():
                    merchant_account = validate_merchant(get_gcash_settings(), str(data.get('merchantAccountNumber') or ''))
                    if data.get('paperSize') == 'Store Supply' or data.get('items'):
                        gcash_receipt_url = validate_receipt(gcash_receipt_url)
                else:
                    gcash_receipt_url = ''
            except ValueError as error:
                return jsonify({'success': False, 'message': str(error)}), 400

            new_order = {
                "id": order_id,
                "customerId": session.get("uid") or cust_email,
                "customer": cust_name,
                "email": cust_email,
                "serviceId": service_id,
                "service": data.get('service') or 'Document Printing (Full Color)',
                "customRequirements": data.get("customRequirements") or {},
                "details": data.get("details") or data.get("customRequirements") or {},
                "items": data.get("items") or [],
                "stockDeducted": False,
                "quantity": qty_val,
                "copies": qty_val,
                "pages": pages_val,
                "paperSize": data.get('paperSize') or 'Short Letter (8.5" x 11")',
                "colorMode": data.get('colorMode', 'Full Color'),
                "customerNote": customer_note,
                "payment": payment_method,
                "gcashRef": gcash_ref,
                "gcashReceiptUrl": gcash_receipt_url,
                "merchantAccountNumber": merchant_account,
                "total": total_amount,
                "status": "Order Submitted",
                "file": file_name,
                "fileUrl": file_url_val,
                "storagePath": storage_path,
                "orderDate": datetime.datetime.now().strftime('%b %d, %Y • %I:%M %p'),
                "createdAt": datetime.datetime.now().isoformat(),
                "timestamp": datetime.datetime.now().isoformat()
            }
            formatted_new_order = format_proper_order_info(new_order)
            if not create_order(formatted_new_order):
                return jsonify({'success': False, 'message': 'Order could not be saved. Please retry.'}), 503
            return jsonify({"success": True, "orderId": order_id, "message": "Order submitted successfully!"}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'GET':
        try:
            orders = get_orders() or []

            if session.get('role') == 'customer':
                user_email = session.get('email', '').strip().lower()
                orders = [o for o in orders if isinstance(o, dict) and o.get('email', '').strip().lower() == user_email]

            formatted_orders = [format_proper_order_info(o) for o in orders if isinstance(o, dict)]
            formatted_orders.sort(key=lambda x: str(x.get('createdAt') or x.get('timestamp') or x.get('orderDate') or ''))
            return jsonify(formatted_orders), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/orders/<order_id>/cancel', methods=['POST'])
def cancel_order_endpoint(order_id):
    if not session.get('username'):
        return jsonify(success=False, message='Sign in first.'), 401
    if db is None:
        return jsonify(success=False, message='Cancellation requires an online connection.'), 503
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(success=False, message='Invalid cancellation request.'), 400
    try:
        order = cancel_customer_order(db, order_id, session.get('uid'), session.get('email'), data.get('reason', ''))
        return jsonify(success=True, order=order)
    except FileNotFoundError as error:
        return jsonify(success=False, message=str(error)), 404
    except PermissionError as error:
        return jsonify(success=False, message=str(error)), 403
    except TypeError as error:
        return jsonify(success=False, message=str(error)), 400
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 409
    except Exception:
        app.logger.exception('Order cancellation failed')
        return jsonify(success=False, message='Unable to confirm cancellation. Reconnect and retry this order.'), 503

@app.route('/api/orders/<order_id>/file', methods=['GET'])
@admin_required
def get_order_file_url(order_id):
    """
    Generate a fresh signed URL for the file attached to an order.
    Admin-only — never exposed to customers.
    """
    try:
        orders = get_orders() or []
        order = next((o for o in orders if isinstance(o, dict) and o.get('id') == order_id), None)
        if not order:
            return jsonify({'success': False, 'message': 'Order not found.'}), 404

        storage_path = order.get('storagePath', '')
        if not storage_path:
            # Fall back to existing fileUrl if no storagePath (legacy orders)
            existing_url = order.get('fileUrl', '')
            if existing_url and existing_url.startswith('http'):
                return jsonify({'success': True, 'fileUrl': existing_url}), 200
            return jsonify({'success': False, 'message': 'No file attached to this order.'}), 404

        signed_url = sc.get_signed_url(storage_path, expires_in=3600)
        if not signed_url:
            return jsonify({'success': False, 'message': 'Could not generate file access URL.'}), 500

        return jsonify({'success': True, 'fileUrl': signed_url}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/orders/<order_id>/status', methods=['PUT', 'POST'])
@admin_required
def update_status(order_id):
    try:
        data = request.json or {}
        new_status = (data.get('status') or '').strip()
        reason = (data.get('reason') or '').strip()

        if 'reject' in new_status.lower() and (not reason or len(reason) < 4):
            return jsonify({"success": False, "message": "Enter a clear rejection reason containing at least 4 characters."}), 400

        final_status = f"Rejected: {reason}" if 'reject' in new_status.lower() and reason else new_status
        success = update_order_status(order_id, final_status)
        if not success:
            return jsonify({"success": False, "error": "Invalid status transition"}), 400
        return jsonify({"success": True, "message": f"Order status updated to {final_status}"}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/analytics', methods=['GET'])
@admin_required
def get_analytics():
    try:
        orders = get_orders() or []
        total_sales, total_pages, print_rev, store_rev = 0.0, 0, 0.0, 0.0
        customers, status_counts, service_breakdown = set(), {}, {}

        for o in orders:
            price = float(o.get('total') or o.get('price') or 0.0)
            copies = int(o.get('copies') or o.get('quantity') or 1)
            pages_per_copy = int(o.get('pages') or 1)
            status = o.get('status') or 'Order Submitted'
            service = o.get('service') or 'Document Printing'
            cust = o.get('customer') or o.get('email')

            if str(status).lower() == 'cancelled':
                price = 0.0
            total_sales += price
            if o.get('source') != 'POS' and str(status).lower() != 'cancelled':
                total_pages += (copies * pages_per_copy)
            if cust: customers.add(str(cust).lower().strip())

            clean_status = "Order Submitted"
            s_lower = str(status).lower()
            if s_lower == 'cancelled': clean_status = "Cancelled"
            elif 'reject' in s_lower: clean_status = "Rejected"
            elif 'complete' in s_lower: clean_status = "Completed"
            elif 'ready' in s_lower or 'pickup' in s_lower: clean_status = "Ready for Pickup"
            elif 'print' in s_lower or 'prepar' in s_lower: clean_status = "Printing"
            elif 'approve' in s_lower: clean_status = "Approved"

            status_counts[clean_status] = status_counts.get(clean_status, 0) + 1
            service_breakdown[service] = service_breakdown.get(service, 0) + 1

            if o.get('source') == 'POS':
                store_rev += float(o.get('productTotal') or 0)
                print_rev += float(o.get('serviceTotal') or 0)
            elif any(k in service.lower() for k in ['store', 'supply', 'in-store', 'notebook', 'pen']):
                store_rev += price
            else:
                print_rev += price

        return jsonify({
            'totalSales': round(total_sales, 2),
            'formattedTotalSales': f"₱ {total_sales:,.2f}",
            'totalOrders': len(orders),
            'totalPagesPrinted': total_pages,
            'avgOrderValue': round((total_sales / len(orders)) if orders else 0.0, 2),
            'formattedAvgOrderValue': f"₱ {((total_sales / len(orders)) if orders else 0.0):,.2f}",
            'activeCustomers': len(customers),
            'storeRevenue': round(store_rev, 2),
            'formattedStoreRevenue': f"₱ {store_rev:,.2f}",
            'printRevenue': round(print_rev, 2),
            'formattedPrintRevenue': f"₱ {print_rev:,.2f}",
            'statusCounts': status_counts,
            'serviceBreakdown': service_breakdown
        }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ---------------------------------------------------------------------------
# LIVE CUSTOMER CHAT ENDPOINTS (MESSENGER-STYLE INBOX)
# ---------------------------------------------------------------------------

@app.route('/api/chat/conversations', methods=['GET'])
@admin_required
def get_conversations_endpoint():
    """
    Returns all customer conversations for the admin inbox,
    sorted strictly by the newest message activity at the top!
    """
    try:
        seed_initial_conversations_if_needed()
        conversations = get_all_conversations()
        return jsonify({"success": True, "conversations": conversations}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/chat/conversations/<conv_id>/messages', methods=['GET', 'POST'])
@login_required
def conversation_messages_endpoint(conv_id):
    role = session.get('role')
    user_email = (session.get('email') or '').strip().lower()

    # Security check: Customers can only access their own conversation!
    if role != 'admin':
        expected_conv_id = get_safe_conversation_id(user_email)
        if conv_id != expected_conv_id:
            return jsonify({"success": False, "message": "Unauthorized! You can only access your own chat conversation."}), 403

    if request.method == 'GET':
        try:
            # Mark messages as read based on who is reading
            mark_role = 'admin' if role == 'admin' else 'customer'
            messages = get_conversation_messages(conv_id, mark_read_for_role=mark_role)
            return jsonify({"success": True, "messages": messages}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'POST':
        try:
            data = request.json or {}
            message_text = str(data.get('message') or data.get('text') or '').strip()

            if not message_text:
                return jsonify({"success": False, "message": "Message cannot be empty."}), 400
            if len(message_text) > 2000:
                return jsonify({"success": False, "message": "Message must not exceed 2,000 characters."}), 400

            if role == 'admin':
                sender_role = 'admin'
                sender_name = 'Admin Staff'
                sender_email = user_email or ADMIN_EMAIL
            else:
                sender_role = 'customer'
                sender_name = session.get('username') or user_email.split('@')[0]
                sender_email = user_email

            msg_data = send_chat_message(conv_id, sender_email, sender_role, sender_name, message_text)
            if not msg_data:
                return jsonify({"success": False, "message": "Failed to send chat message."}), 500

            return jsonify({"success": True, "message": msg_data, "conversationId": conv_id}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/chat/conversations/<conv_id>/read', methods=['POST'])
@login_required
def mark_conversation_read_endpoint(conv_id):
    role = session.get('role')
    user_email = (session.get('email') or '').strip().lower()

    if role != 'admin':
        expected_conv_id = get_safe_conversation_id(user_email)
        if conv_id != expected_conv_id:
            return jsonify({"success": False, "message": "Unauthorized"}), 403

    try:
        mark_role = 'admin' if role == 'admin' else 'customer'
        get_conversation_messages(conv_id, mark_read_for_role=mark_role)
        return jsonify({"success": True, "message": "Conversation marked as read."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/chat/my-conversation', methods=['GET'])
@login_required
def get_my_conversation_endpoint():
    try:
        user_email = (session.get('email') or '').strip().lower()
        if not user_email:
            return jsonify({"success": False, "message": "Customer email not found in session."}), 401

        user_name = session.get('username') or user_email.split('@')[0]
        conv = get_or_create_conversation(user_email, customer_name=user_name)
        messages = get_conversation_messages(conv['id'], mark_read_for_role='customer')
        return jsonify({"success": True, "conversation": conv, "messages": messages}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/chat', methods=['GET', 'POST'])
@login_required
def handle_chat():
    """
    Legacy backwards compatibility endpoint.
    Automatically connects to the real Firestore conversations!
    """
    role = session.get('role')
    user_email = (session.get('email') or '').strip().lower()

    if request.method == 'GET':
        try:
            if role == 'customer':
                conv = get_or_create_conversation(user_email, customer_name=session.get('username'))
                msgs = get_conversation_messages(conv['id'], mark_read_for_role='customer')
                return jsonify(msgs), 200
            else:
                convs = get_all_conversations()
                if convs:
                    first_conv_id = convs[0]['id']
                    msgs = get_conversation_messages(first_conv_id, mark_read_for_role='admin')
                    return jsonify(msgs), 200
                return jsonify([]), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'POST':
        try:
            data = request.json or {}
            message_text = (data.get('message') or data.get('text') or '').strip()

            if not message_text:
                return jsonify({"success": False, "message": "Message cannot be empty."}), 400

            if role == 'customer':
                conv = get_or_create_conversation(user_email, customer_name=session.get('username'))
                conv_id = conv['id']
                sender_role = 'customer'
                sender_name = session.get('username') or user_email.split('@')[0]
                sender_email = user_email
            else:
                conv_id = data.get('conversationId')
                if not conv_id:
                    convs = get_all_conversations()
                    conv_id = convs[0]['id'] if convs else None
                if not conv_id:
                    return jsonify({"success": False, "message": "No conversation selected."}), 400
                sender_role = 'admin'
                sender_name = 'Admin Staff'
                sender_email = user_email or ADMIN_EMAIL

            msg_data = send_chat_message(conv_id, sender_email, sender_role, sender_name, message_text)
            return jsonify({"success": True, "message": msg_data}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/rfid/tap', methods=['POST'])
@login_required
def handle_rfid_tap():
    try:
        data = request.json or {}
        card_uid = data.get('rfidCardUid', 'CARD-8842-PH')
        earned_points = 0
        success = log_rfid_tap(card_uid, earned_points)
        if not success:
            return jsonify({'success': False, 'message': 'Card is not assigned or tap failed.'}), 400
        return jsonify({"success": True, "earnedPoints": earned_points, "message": f"RFID Card {card_uid} successfully logged."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



# ---------------------------------------------------------------------------
# IN-APP NOTIFICATIONS API
# ---------------------------------------------------------------------------

def _notification_identity():
    email = str(session.get('email') or '').strip().lower()
    uid = session.get('uid') or email
    if email and (session.get('notification_email') != email or not session.get('notification_uid')):
        user = get_firebase_user_by_email(email)
        if user:
            session['notification_uid'] = user.uid
            session['notification_email'] = email
    if session.get('notification_email') == email:
        uid = session.get('notification_uid') or uid
    recipient = 'ADMIN' if session.get('role') == 'admin' else (session.get('uid') or uid)
    return recipient, email, uid


@app.route('/api/notifications', methods=['GET'])
@login_required
def get_user_notifications():
    try:
        user_id, email, state_uid = _notification_identity()
        notifs = get_notifications(user_id, email, state_uid) or []
        # Sort newest first
        notifs.sort(key=lambda x: str(x.get('timestamp') or x.get('createdAt') or ''), reverse=True)
        return jsonify({'success': True, 'notifications': notifs}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def read_all_notifications():
    try:
        user_id, email, state_uid = _notification_identity()
        if not mark_all_notifications_read(user_id, email, state_uid):
            return jsonify({'success': False, 'error': 'Unable to save notification state.'}), 500
        return jsonify({'success': True}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notifications/clear', methods=['POST'])
@login_required
def clear_all_notifications():
    try:
        user_id, email, state_uid = _notification_identity()
        if not delete_all_notifications(user_id, email, state_uid):
            return jsonify({'success': False, 'error': 'Unable to clear notifications.'}), 500
        return jsonify({'success': True}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------------------------------------------------------------------
# PUSH NOTIFICATION & SERVICE WORKER API ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/firebase-messaging-sw.js', methods=['GET'])
def service_worker_file():
    """
    Serves the Firebase Cloud Messaging service worker from the root domain.
    Required by the browser Service Worker specification for root scope.
    """
    from flask import send_from_directory, make_response
    response = make_response(send_from_directory('static', 'firebase-messaging-sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.route('/api/notifications/public-config', methods=['GET'])
def get_notification_public_config():
    """
    Exposes client-safe public web configuration and VAPID key.
    Never exposes any server secret, private key, or service account credential.
    """
    return jsonify({
        "success": True,
        "firebaseConfig": {
            "apiKey": os.getenv("FIREBASE_API_KEY", "AIzaSyBDk0kto8_VpvtzkQBdjrVP2YHOuUZ17rQ"),
            "authDomain": "printingshop-7fd66.firebaseapp.com",
            "projectId": "printingshop-7fd66",
            "storageBucket": "printingshop-7fd66.firebasestorage.app",
            "messagingSenderId": "800500466642",
            "appId": "1:800500466642:web:762350381a60650b16fe0b"
        },
        "vapidKey": os.getenv("FIREBASE_VAPID_KEY", "BO_6tU9-14iS5_Nn2YgN_3f7hJ7y4y_dummy_vapid_key_if_needed")
    }), 200

@app.route('/api/notifications/fcm-token', methods=['POST'])
@login_required
def save_fcm_token_endpoint():
    """
    Associates an FCM push token with the authenticated session user.
    """
    try:
        data = request.json or {}
        token = (data.get('token') or '').strip()
        device = (data.get('device') or 'Web Browser').strip()

        if not token:
            return jsonify({"success": False, "message": "Missing FCM token."}), 400

        user_email = (session.get('email') or '').strip().lower()
        role = session.get('role', 'customer')

        if not user_email and role == 'admin':
            user_email = ADMIN_EMAIL

        success = register_fcm_token(user_email, role, token, device)
        if success:
            return jsonify({"success": True, "message": "Push notification token registered."}), 200
        else:
            return jsonify({"success": False, "message": "Failed to save push token."}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/notifications/fcm-token', methods=['DELETE'])
@login_required
def delete_fcm_token_endpoint():
    """
    Unregisters or removes a push token upon user opt-out or sign-out.
    """
    try:
        data = request.json or {}
        token = (data.get('token') or '').strip()
        if not token:
            return jsonify({"success": False, "message": "Missing FCM token."}), 400

        remove_fcm_token(token)
        return jsonify({"success": True, "message": "Push token deleted."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/notifications/test-push', methods=['POST'])
@login_required
def test_push_endpoint():
    """
    Sends a test push notification to verify browser notification delivery for the current user.
    """
    try:
        role = session.get('role', 'customer')
        email = (session.get('email') or '').strip().lower()
        if not email and role == 'admin':
            email = ADMIN_EMAIL

        result = send_fcm_push_notification(
            recipient_role=role,
            recipient_email=email,
            title="Kez C-Em Zek Test Push",
            body="Live notifications are active on this device!",
            click_url=f"/admin?tab=liveChat" if role == 'admin' else "/customer?tab=chat"
        )
        result.pop('delivered_tokens', None)
        return jsonify({"success": True, "result": result}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# ARCHIVE MANAGEMENT ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/archive', methods=['GET'])
def get_archives():
    is_admin = session.get('role') == 'admin' or session.get('username') == 'Admin'
    if not is_admin:
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        item_type = request.args.get('type', 'all')
        archives = get_archived_items(item_type)
        return jsonify({'success': True, 'archives': archives or []}), 200
    except Exception as e:
        print(f"Error in get_archives: {e}")
        return jsonify({'success': True, 'archives': []}), 200

@app.route('/api/archive/<archive_id>/restore', methods=['POST'])
def restore_archive(archive_id):
    is_admin = session.get('role') == 'admin' or session.get('username') == 'Admin'
    if not is_admin:
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        success, message = restore_archived_item(archive_id)
        return jsonify({'success': success, 'message': message}), 200 if success else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/archive/<archive_id>', methods=['DELETE'])
def delete_archive_permanent(archive_id):
    is_admin = session.get('role') == 'admin' or session.get('username') == 'Admin'
    if not is_admin:
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        success = permanently_delete_archived_item(archive_id)
        return jsonify({'success': success, 'message': 'Item permanently deleted from archive.'}), 200 if success else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/store/products/<product_id>', methods=['DELETE'])
def delete_store_product_endpoint(product_id):
    is_admin = session.get('role') == 'admin' or session.get('username') == 'Admin'
    if not is_admin:
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        current_products = get_store_products()
        target_prod = None
        for p in current_products:
            if str(p.get('id')) == str(product_id):
                target_prod = p
                break

        if not target_prod or not archive_item('store_product', product_id, target_prod, 'admin_store'):
            return jsonify({'success': False, 'message': 'Product is missing or archive failed.'}), 409
        return jsonify({'success': True, 'message': 'In-store product moved to Archive successfully.'}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Printing shop web backend')
    parser.add_argument('--push-worker', action='store_true', help='Run the standalone notification sender')
    args = parser.parse_args()
    if args.push_worker:
        if db is None:
            raise SystemExit('Firebase credentials are unavailable.')
        run(db, send_fcm_push_notification)
    else:
        if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
            start_push_worker(db, send_fcm_push_notification)
        print('=' * 60)
        print('KCZ Printing backend running on http://127.0.0.1:5000')
        print('=' * 60)
        app.run(debug=True, port=5000)
