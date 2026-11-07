"""Authoritative loyalty operations. Every balance mutation shares an order transaction."""
import datetime
import hashlib
import re
import secrets
from urllib.parse import urlparse

from firebase_admin import firestore
from catalog_rules import validate_requirements
from inventory_rules import stock_plan, valid_transition
from order_cancellation import can_cancel_order
from payment_rules import validate_payment, validate_merchant, validate_receipt
from loyalty_rules import account_values, discount_quote, money, service_total, whole


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
                raise ValueError('Hindi na puwedeng i-cancel dahil approved na o nagsimula na ang order.')
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
