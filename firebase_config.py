import firebase_admin
from firebase_admin import credentials, firestore, auth, messaging
import datetime
import uuid
import re
from inventory_rules import stock_plan, valid_transition
from catalog_rules import validate_service
from payment_rules import shared_qr_settings
from pathlib import Path

# Initialize Firebase Admin SDK
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate("firebase-service-account.json")
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Error initializing Firebase: {e}")
    db = None

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
                'message': f'Ang iyong {svc} ({order_id}) ay nasa status na: {new_status}.',
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
                "Hello po, available pa ba ito?",
                "Thank you po!",
                "Saan po makikita order ko?",
                "Magkano po tarpaulin print?"
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
