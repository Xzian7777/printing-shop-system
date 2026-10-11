import firebase_admin
from firebase_admin import credentials, firestore, auth
import datetime

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
        order_ref.update({'status': new_status})

        if new_status in ['Approved', 'Completed']:
            service_desc = str(order_data.get('service', '')).lower()
            order_qty = int(order_data.get('quantity') or order_data.get('copies') or 1)

            products_ref = db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('store_products')
            products_docs = products_ref.get()

            for doc in products_docs:
                pdata = doc.to_dict()
                pname = str(pdata.get('name', '')).strip().lower()

                if pname and (pname in service_desc or service_desc in pname):
                    current_stock = int(pdata.get('stock', 0))
                    new_stock = max(0, current_stock - order_qty)
                    products_ref.document(doc.id).update({'stock': new_stock})
                    print(f"✅ FIRESTORE STOCK DEDUCTED: {pname} -> {new_stock} (-{order_qty})")

        return True
    except Exception as e:
        print(f"Error updating order status: {e}")
        return False

# ---------------------------------------------------------------------------
# OTHER STORE & USER HELPER FUNCTIONS
# ---------------------------------------------------------------------------
def get_customers():
    try:
        if db is None:
            return []
        docs = db.collection('customers').get()
        customers = []
        for doc in docs:
            c_data = doc.to_dict()
            if 'id' not in c_data or not c_data['id']:
                c_data['id'] = doc.id
            customers.append(c_data)
        return customers
    except Exception as e:
        print(f"Error fetching customers from Firestore: {e}")
        return []

def create_customer(customer_data):
    try:
        db.collection('customers').document(customer_data['id']).set(customer_data)
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


def delete_firebase_customer(uid):
    try:
        auth.delete_user(uid)
        return True
    except Exception as e:
        print(f"Error deleting Firebase Auth user: {e}")
        return False

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
        if db is not None:
            products = []
            
            docs = db.collection('products').stream()
            for doc in docs:
                p_data = doc.to_dict()
                if 'id' not in p_data or not p_data['id']:
                    p_data['id'] = doc.id
                products.append(p_data)

            if not products:
                docs = db.collection('artifacts') \
                          .document('printcraft-pro') \
                          .collection('public') \
                          .document('data') \
                          .collection('store_products') \
                          .stream()
                for doc in docs:
                    p_data = doc.to_dict()
                    if 'id' not in p_data or not p_data['id']:
                        p_data['id'] = doc.id
                    products.append(p_data)

            return products
        return []
    except Exception as e:
        print(f"Error fetching store products: {e}")
        return []

def save_store_products(products_list):
    try:
        if db is not None:
            for prod in products_list:
                prod_id = str(prod.get('id', f"p-{int(datetime.datetime.now().timestamp())}"))
                db.collection('products').document(prod_id).set(prod, merge=True)
                
                db.collection('artifacts') \
                  .document('printcraft-pro') \
                  .collection('public') \
                  .document('data') \
                  .collection('store_products') \
                  .document(prod_id) \
                  .set(prod, merge=True)
            print("✅ Store products synced to Firestore!")
            return True
        return False
    except Exception as e:
        print(f"Error saving store products: {e}")
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

def log_rfid_tap(tap_data):
    try:
        db.collection('rfid_logs').document(tap_data['id']).set(tap_data)
        return True
    except Exception:
        return False

def get_notifications(user_id):
    try:
        docs = db.collection('notifications').get()
        return [doc.to_dict() for doc in docs if doc.to_dict().get('userId') in [user_id, 'ALL', 'ADMIN']]
    except Exception:
        return []

def create_notification(notif_data):
    try:
        db.collection('notifications').document(notif_data['id']).set(notif_data)
        return True
    except Exception:
        return False

def mark_all_notifications_read(user_id):
    try:
        docs = db.collection('notifications').get()
        for doc in docs:
            if doc.to_dict().get('userId') in [user_id, 'ALL', 'ADMIN']:
                db.collection('notifications').document(doc.id).update({'status': 'Read'})
        return True
    except Exception:
        return False

def delete_notification(notif_id):
    try:
        db.collection('notifications').document(notif_id).delete()
        return True
    except Exception:
        return False

def delete_all_notifications(user_id):
    try:
        docs = db.collection('notifications').get()
        for doc in docs:
            if doc.to_dict().get('userId') in [user_id, 'ALL', 'ADMIN']:
                db.collection('notifications').document(doc.id).delete()
        return True
    except Exception:
        return False

def get_analytics_summary():
    orders = get_orders()
    total_sales = sum(float(o.get('total', 0)) for o in orders if o.get('status') in ['Approved', 'Completed'])
    return {
        "totalOrders": len(orders),
        "totalSales": total_sales,
        "activeOrders": len([o for o in orders if o.get('status') not in ['Completed', 'Rejected']])
    }