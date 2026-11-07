import datetime
import re
import secrets

from firebase_admin import firestore
from pos_rules import validate_sale, order_record


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
            from loyalty_backend import config, read_account, save_account
            from loyalty_rules import account_values, discount_quote, money, whole, service_total
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
                from catalog_rules import validate_requirements
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
