"""Shared session/Firebase-token API used by both clients."""
import datetime
import re
import uuid
from functools import wraps

from flask import Blueprint, jsonify, request, session
from firebase_admin import auth, firestore
import firebase_config as firebase
from loyalty_backend import config, orders_ref, submit_order, apply_order_status
from loyalty_rules import account_values, money, whole


class SignInRequired(Exception):
    pass


def customer_identity(uid=None, email=None):
    user = auth.get_user(uid) if uid else auth.get_user_by_email(email)
    if user.disabled:
        raise PermissionError('Customer account is disabled.')
    profile = firebase.db.collection('users').document(user.uid).get().to_dict() or {}
    if profile.get('email') and str(profile['email']).strip().lower() != user.email.lower():
        raise ValueError('Resolve the customer profile email mapping before using points.')
    mirrors = list(firebase.db.collection('customers').where('email', '==', user.email.lower()).stream())
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
        settings = config(firebase.db)
        ref = firebase.db.collection('loyalty_accounts').document(actor['uid'])
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
        config(firebase.db)
        records = [{**d.to_dict(), 'id': d.id} for d in firebase.db.collection('loyalty_rewards').stream()
                   if d.to_dict().get('active')]
        return jsonify(success=True, rewards=records)

    @api.post('/api/loyalty/orders')
    @api_errors
    def order():
        actor = identity()
        if actor['role'] == 'admin':
            raise PermissionError('Use POS to place a customer counter order.')
        record = submit_order(firebase.db, actor, body())
        return jsonify(success=True, orderId=record['id'], order=record)

    @api.route('/api/admin/loyalty/rewards', methods=['GET', 'POST'])
    @api.route('/api/admin/loyalty/rewards/<reward_id>', methods=['PUT'])
    @api_errors
    def manage_rewards(reward_id=None):
        identity(admin=True)
        if request.method == 'GET':
            return jsonify(success=True, rewards=[{**d.to_dict(), 'id': d.id} for d in firebase.db.collection('loyalty_rewards').stream()])
        data = body()
        name = str(data.get('name') or '').strip()
        description = str(data.get('description') or '').strip()
        kind = data.get('kind')
        key = safe_key(data.get('catalogId'))
        if not name or len(name) > 100 or len(description) > 500 or kind not in ['product', 'service']:
            raise ValueError('Provide a name, description up to 500 characters, and a product or service.')
        catalog = firebase.db.collection('products' if kind == 'product' else 'services').document(key).get().to_dict() or {}
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
            from catalog_rules import validate_requirements
            from loyalty_rules import service_total
            validate_requirements(catalog, requirements)
            service_total(key, catalog.get('basePrice'), {'quantity': quantity, 'pages': pages, 'details': specifications})
        ref = firebase.db.collection('loyalty_rewards').document(safe_key(reward_id) if reward_id else uuid.uuid4().hex)
        ref.set({'name': name, 'description': description, 'kind': kind, 'catalogId': key,
                 'pointsCost': points, 'quantity': quantity, 'pages': pages, 'specifications': specifications,
                 'requirements': requirements, 'active': data.get('active') is True,
                 'updatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat()})
        return jsonify(success=True, id=ref.id)

    @api.route('/api/admin/loyalty/settings', methods=['GET', 'PUT'])
    @api_errors
    def settings():
        identity(admin=True)
        current = config(firebase.db)
        if request.method == 'PUT':
            value = money(body().get('pointValue'))
            whole(value, 'Point value in centavos', 1, 100000)
            firebase.db.collection('settings').document('loyalty_config').update({'pointValueCents': value})
            current['pointValueCents'] = value
        return jsonify(success=True, settings=current)

    @api.get('/api/admin/loyalty/catalog')
    @api_errors
    def catalog():
        identity(admin=True)
        return jsonify(success=True, products=firebase.get_store_products(), services=firebase.get_services())

    @api.get('/api/admin/loyalty/customers')
    @api_errors
    def customers():
        identity(admin=True)
        query = str(request.args.get('q') or '').strip().lower()
        if len(query) < 2:
            return jsonify(success=True, customers=[])
        found = {}
        rate = config(firebase.db).get('pointValueCents', 100)
        for data in firebase.get_customers():
            if not any(query in str(data.get(k) or '').lower() for k in ['email', 'fullName', 'name', 'rfidUid']):
                continue
            try:
                actor = customer_identity(email=data.get('email'))
                if actor['role'] == 'admin':
                    continue
                balance = firebase.db.collection('loyalty_accounts').document(actor['uid']).get().to_dict() or {}
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
                profile = firebase.db.collection('users').document(actor['uid']).get().to_dict() or {}
                card = profile.get('rfidUid', '')
            matches = []
            for collection in ['users', 'customers']:
                matches.extend(firebase.db.collection(collection).where('rfidUid', '==', card).stream())
            owners = {customer_identity(email=doc.to_dict().get('email'))['uid'] for doc in matches}
            if not card or len(owners) != 1:
                raise ValueError('Card is unassigned or ambiguous.')
            if actor['role'] != 'admin' and actor['uid'] not in owners:
                raise PermissionError('This card belongs to another customer.')
            if not firebase.log_rfid_tap(card, 0):
                raise ValueError('Card identification failed.')
            return jsonify(success=True, earnedPoints=0, message='Card identified. Points are earned on paid completed purchases.')
        if path == '/api/orders' and request.method == 'POST':
            data = body()
            if data.get('pointsUsed') or data.get('rewardId') or request.headers.get('Authorization'):
                actor = identity()
                if actor['role'] == 'admin':
                    raise PermissionError('Use POS for customer counter orders.')
                record = submit_order(firebase.db, actor, data)
                return jsonify(success=True, orderId=record['id'], order=record)
        match = re.fullmatch(r'/api/orders/([^/]+)/(status|cancel)', path)
        if match and request.method in ['POST', 'PUT']:
            actor = identity(admin=match[2] == 'status')
            data = body()
            target = 'Cancelled' if match[2] == 'cancel' else str(data.get('status') or '')
            # UID is resolved through Firebase Auth for legacy order owners.
            snapshot = orders_ref(firebase.db).document(match[1]).get()
            order_data = snapshot.to_dict() or {}
            if not order_data.get('loyaltyUid') and order_data.get('source') != 'POS':
                try:
                    owner = customer_identity(email=order_data.get('email') or order_data.get('customerEmail'))
                    if owner['role'] != 'admin':
                        actor['orderCustomerUid'] = owner['uid']
                except (auth.UserNotFoundError, ValueError, PermissionError):
                    pass
            result = apply_order_status(firebase.db, match[1], target, actor,
                                        data.get('reason', ''), data.get('paymentConfirmed', False))
            return jsonify(success=True, order=result)
        if path == '/api/pos/sales':
            actor = identity(admin=True)
            if request.method == 'GET':
                records = [{k: v for k, v in doc.to_dict().items() if k != 'recordedAt'} for doc in
                           firebase.db.collection('pos_sales').order_by('recordedAt', direction=firestore.Query.DESCENDING).limit(20).stream()]
                return jsonify(success=True, sales=records)
            data = body()
            uid = data.get('customerUid')
            customer = customer_identity(uid=safe_key(uid)) if uid else None
            if customer and customer['role'] == 'admin':
                raise ValueError('Select a customer account.')
            if customer and not str(data.get('buyerName') or '').strip():
                data['buyerName'] = customer['name']
            from pos_backend import complete_sale
            sale = complete_sale(firebase.db, data, actor['uid'], customer)
            return jsonify(success=True, sale={k: v for k, v in sale.items() if k != 'recordedAt'})
        return None

    app.register_blueprint(api)
