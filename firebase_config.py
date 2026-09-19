import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# Determine path for Firebase Service Account Key
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "serviceAccountKey.json")

# Initialize Firebase Admin SDK
try:
    if not firebase_admin._apps:
        if os.path.exists(CREDENTIALS_PATH):
            cred = credentials.Certificate(CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
            print("🔥 Firebase Admin SDK initialized with Service Account Key!")
        else:
            firebase_admin.initialize_app()
            print("🔥 Firebase initialized with Default Credentials.")
    db = firestore.client()
except Exception as e:
    print(f"⚠️ Firebase initialization warning: {e}")
    db = None

COLLECTION_APP_ID = "printcraft-pro"

# Global In-Memory Order Storage (Synchronized with database)
_IN_MEMORY_ORDERS = [
    {
        "id": "PRNT-8942",
        "customerId": "CUST-101",
        "customer": "maria",
        "service": "Standard A4 Document",
        "pages": 15,
        "copies": 1,
        "paperSize": "A4",
        "colorMode": "Color",
        "sides": "Single",
        "total": 45.00,
        "orderDate": "2026-03-12",
        "pickupDateTime": "2026-03-13 15:30",
        "status": "Approved",
        "file": "Project_Report.pdf",
        "payment": "GCash",
        "timestamp": "2026-03-12T10:00:00"
    }
]

def get_collection(collection_name):
    """Returns Firestore sub-collection reference under app artifact space"""
    if db:
        return db.collection("artifacts").document(COLLECTION_APP_ID).collection("public").document("data").collection(collection_name)
    return None

# ===================================================================
# SCHEMA A: CUSTOMER DATA
# ===================================================================
def create_customer(customer_data):
    ref = get_collection("customers")
    if ref:
        ref.document(customer_data["id"]).set(customer_data)
    return customer_data

def get_customers():
    ref = get_collection("customers")
    if ref:
        try:
            docs = ref.stream()
            results = [doc.to_dict() for doc in docs]
            if results:
                return results
        except Exception:
            pass
    return [
        {"id": "CUST-101", "fullName": "Maria Santos", "contact": "09171234567", "email": "maria.santos@email.com", "regDate": "2026-01-15", "rfidUid": "CARD-8842-PH"},
        {"id": "CUST-102", "fullName": "Juan Dela Cruz", "contact": "09189876543", "email": "juan.delacruz@email.com", "regDate": "2026-02-01", "rfidUid": "CARD-9910-PH"}
    ]

def get_customer_profile(customer_id):
    ref = get_collection("customers")
    if ref:
        try:
            doc = ref.document(str(customer_id)).get()
            if doc.exists:
                return doc.to_dict()
        except Exception:
            pass
    return {
        "customerId": customer_id,
        "fullName": "Kez C-Em Zek Customer",
        "username": "customer",
        "email": "customer@kezcemzek.com",
        "contact": "09170000000",
        "address": "Pila, Laguna",
        "avatarUrl": "https://placehold.co/150x150/2563eb/ffffff?text=KCZ",
        "loyaltyPoints": 170,
        "rfidUid": "CARD-8842-PH"
    }

def save_customer_profile(profile_data):
    ref = get_collection("customers")
    if ref:
        ref.document(str(profile_data.get("customerId", "CUST-101"))).set(profile_data, merge=True)
    return True

# ===================================================================
# SCHEMA B & C: PRINTING ORDERS & PAYMENTS DATA
# ===================================================================
def create_order(order_data):
    # Synchronize with in-memory store immediately
    _IN_MEMORY_ORDERS.append(order_data)

    ref = get_collection("orders")
    if ref:
        try:
            ref.document(order_data["id"]).set(order_data)
        except Exception:
            pass

    create_payment({
        "id": f"PAY-{order_data['id'].replace('PRNT-', '')}",
        "orderId": order_data["id"],
        "method": order_data.get("payment", "GCash"),
        "amountPaid": float(order_data.get("total", 0.0) or order_data.get("totalAmount", 0.0) or 0.0),
        "status": "Paid" if order_data.get("payment") == "GCash" else "Pending",
        "transactionDate": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "refNumber": f"GC-{int(datetime.datetime.now().timestamp())}" if order_data.get("payment") == "GCash" else "COP-PENDING"
    })

    create_notification({
        "id": f"NOTIF-{int(datetime.datetime.now().timestamp())}",
        "userId": order_data.get('customer', 'CUSTOMER'),
        "notificationType": "New Order Submitted",
        "message": f"Order #{order_data['id']} placed successfully worth ₱{float(order_data.get('total', 0.0)):.2f}",
        "time": "Just now",
        "status": "Unread"
    })
    return order_data

def get_orders():
    ref = get_collection("orders")
    if ref:
        try:
            docs = ref.stream()
            orders = [doc.to_dict() for doc in docs]
            if orders:
                return orders
        except Exception:
            pass
    return _IN_MEMORY_ORDERS

def update_order_status(order_id, new_status):
    # Update in-memory orders store
    target_user = 'CUSTOMER'
    for o in _IN_MEMORY_ORDERS:
        if str(o.get('id')) == str(order_id):
            o['status'] = new_status
            target_user = o.get('customer', 'CUSTOMER')
            break

    # Update in Firestore database
    ref = get_collection("orders")
    if ref:
        try:
            order_doc = ref.document(order_id).get()
            if order_doc.exists:
                ref.document(order_id).update({"status": new_status})
        except Exception:
            pass

    create_notification({
        "id": f"NOTIF-{int(datetime.datetime.now().timestamp())}",
        "userId": target_user,
        "notificationType": f"Order Status Update",
        "message": f"Your order #{order_id} status has been updated to: {new_status}",
        "time": "Just now",
        "status": "Unread"
    })
    return True

def create_payment(payment_data):
    ref = get_collection("payments")
    if ref:
        try:
            ref.document(payment_data["id"]).set(payment_data)
        except Exception:
            pass

# ===================================================================
# SCHEMA D: INVENTORY & STORE PRODUCTS DATA
# ===================================================================
def get_store_products():
    ref = get_collection("store_products")
    if ref:
        try:
            docs = ref.stream()
            items = [doc.to_dict() for doc in docs]
            if items:
                return items
        except Exception:
            pass
    return [
        { "id": "p1", "name": "Spiral Notebook (80 Sheets)", "category": "notebooks", "price": 25.00, "stock": 140, "desc": "High quality ruled paper for school & office.", "imageUrl": "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?auto=format&fit=crop&w=300&q=80" },
        { "id": "p2", "name": "Stitched Composition Book", "category": "notebooks", "price": 35.00, "stock": 35, "desc": "Durable hardcover notebook 100 pages.", "imageUrl": "https://images.unsplash.com/photo-1589829085413-56de8ae18c73?auto=format&fit=crop&w=300&q=80" },
        { "id": "p3", "name": "Black Gel Pen (0.5mm)", "category": "pens", "price": 15.00, "stock": 320, "desc": "Smooth quick-drying gel ink.", "imageUrl": "https://images.unsplash.com/photo-1583485088034-697b5bc54ccd?auto=format&fit=crop&w=300&q=80" },
        { "id": "p4", "name": "Highlighter Markers (Pack of 3)", "category": "pens", "price": 45.00, "stock": 10, "desc": "Neon yellow, green, & pink highlighters.", "imageUrl": "https://images.unsplash.com/photo-1569683795645-b62e50fbf103?auto=format&fit=crop&w=300&q=80" },
        { "id": "p5", "name": "A4 Watercolor Sketchpad", "category": "art", "price": 120.00, "stock": 12, "desc": "200 GSM heavy cold-pressed paper.", "imageUrl": "https://images.unsplash.com/photo-1513364776144-60967b0f800f?auto=format&fit=crop&w=300&q=80" },
        { "id": "p6", "name": "Colored Pencils Set (12 colors)", "category": "art", "price": 85.00, "stock": 55, "desc": "Vibrant blendable colored pencils.", "imageUrl": "https://images.unsplash.com/photo-1513542789411-b6a5d4f31634?auto=format&fit=crop&w=300&q=80" }
    ]

def save_store_products(products):
    ref = get_collection("store_products")
    if ref:
        try:
            for p in products:
                ref.document(str(p["id"])).set(p)
        except Exception:
            pass
    return True

def get_inventory():
    ref = get_collection("inventory")
    if ref:
        try:
            docs = ref.stream()
            items = [doc.to_dict() for doc in docs]
            if items:
                return items
        except Exception:
            pass
    return [
        {"id": "INV-01", "itemName": "Standard A4 Bond Paper", "category": "Paper Stock", "paperTypeSize": "A4 80GSM", "currentStock": 850, "minStockLevel": 200, "stockAdded": 1000, "stockUsed": 150, "lowStockStatus": "Normal", "restockDate": "2026-03-01"},
        {"id": "INV-02", "itemName": "Premium Glossy Photo Paper", "category": "Paper Stock", "paperTypeSize": "A4 230GSM", "currentStock": 120, "minStockLevel": 150, "stockAdded": 300, "stockUsed": 180, "lowStockStatus": "Low Stock", "restockDate": "2026-02-20"}
    ]

# ===================================================================
# SCHEMA E: ARDUINO IOT & RFID REWARD DATA
# ===================================================================
def log_rfid_tap(rfid_data):
    ref = get_collection("rfid")
    if ref:
        try:
            ref.document(rfid_data["id"]).set(rfid_data)
        except Exception:
            pass
    return rfid_data

# ===================================================================
# SCHEMA F & G: CHAT & NOTIFICATIONS DATA
# ===================================================================
def get_notifications(user_id=None):
    ref = get_collection("notifications")
    if ref:
        try:
            docs = ref.stream()
            all_notifs = [doc.to_dict() for doc in docs]
            if user_id:
                if str(user_id).upper() == "ADMIN":
                    return [n for n in all_notifs if n.get("userId") == "ADMIN"]
                else:
                    clean_id = user_id.split('@')[0] if '@' in str(user_id) else user_id
                    return [
                        n for n in all_notifs 
                        if n.get("userId") in [user_id, clean_id, "CUSTOMER"] and n.get("userId") != "ADMIN"
                    ]
            return all_notifs
        except Exception:
            pass
    return []

def create_notification(notif_data):
    ref = get_collection("notifications")
    if ref:
        try:
            ref.document(notif_data["id"]).set(notif_data)
        except Exception:
            pass

def mark_all_notifications_read(user_id=None):
    ref = get_collection("notifications")
    if ref:
        try:
            docs = ref.stream()
            for doc in docs:
                data = doc.to_dict()
                if user_id:
                    if str(user_id).upper() == "ADMIN":
                        if data.get("userId") == "ADMIN":
                            doc.reference.update({"status": "Read"})
                    else:
                        clean_id = user_id.split('@')[0] if '@' in str(user_id) else user_id
                        if data.get("userId") in [user_id, clean_id, "CUSTOMER"] and data.get("userId") != "ADMIN":
                            doc.reference.update({"status": "Read"})
                else:
                    doc.reference.update({"status": "Read"})
        except Exception:
            pass
    return True

def delete_notification(notif_id):
    ref = get_collection("notifications")
    if ref:
        try:
            ref.document(notif_id).delete()
        except Exception:
            pass
    return True

def delete_all_notifications(user_id=None):
    ref = get_collection("notifications")
    if ref:
        try:
            docs = ref.stream()
            for doc in docs:
                data = doc.to_dict()
                if user_id:
                    if str(user_id).upper() == "ADMIN":
                        if data.get("userId") == "ADMIN":
                            doc.reference.delete()
                    else:
                        clean_id = user_id.split('@')[0] if '@' in str(user_id) else user_id
                        if data.get("userId") in [user_id, clean_id, "CUSTOMER"] and data.get("userId") != "ADMIN":
                            doc.reference.delete()
                else:
                    doc.reference.delete()
        except Exception:
            pass
    return True

# ===================================================================
# SCHEMA H: ANALYTICS SUMMARY ENGINE
# ===================================================================
def get_analytics_summary():
    orders = get_orders()
    customers = get_customers()
    
    # Filter valid non-rejected orders
    valid_orders = [o for o in orders if 'reject' not in str(o.get('status', '')).lower()]
    
    total_sales = sum(float(o.get("total", 0.0) or o.get("totalAmount", 0.0) or 0.0) for o in valid_orders)
    total_orders = len(orders)
    avg_order = (total_sales / len(valid_orders)) if valid_orders else 0.0
    
    # Calculate total pages printed
    total_pages_printed = 0
    for o in valid_orders:
        srv = str(o.get('service', '')).lower()
        if 'in-store' not in srv:
            p_val = int(o.get("pages") or 1)
            c_val = int(o.get("copies") or 1)
            total_pages_printed += max(1, p_val) * max(1, c_val)
    
    # Calculate store vs print revenue breakdown
    store_revenue = sum(
        float(o.get("total", 0.0) or o.get("totalAmount", 0.0) or 0.0) 
        for o in valid_orders 
        if 'in-store' in str(o.get('service', '')).lower() or 'store supply' in str(o.get('paperSize', '')).lower()
    )
    print_revenue = max(0.0, total_sales - store_revenue)

    # Calculate real-time status distribution
    status_counts = {
        "Submitted": 0,
        "Approved": 0,
        "Printing": 0,
        "Ready for Pickup": 0,
        "Completed": 0,
        "Rejected": 0
    }
    
    for o in orders:
        s = str(o.get("status", "")).lower()
        if "reject" in s:
            status_counts["Rejected"] += 1
        elif "complete" in s:
            status_counts["Completed"] += 1
        elif "ready" in s or "pickup" in s:
            status_counts["Ready for Pickup"] += 1
        elif "print" in s:
            status_counts["Printing"] += 1
        elif "approve" in s:
            status_counts["Approved"] += 1
        else:
            status_counts["Submitted"] += 1

    # Calculate real-time breakdown by Service Type
    service_breakdown = {}
    for o in valid_orders:
        srv = o.get("service", "Standard A4 Print")
        clean_srv = srv.split(':')[0].strip() if ':' in srv else srv
        service_breakdown[clean_srv] = service_breakdown.get(clean_srv, 0) + 1

    return {
        "totalSales": round(total_sales, 2),
        "totalOrders": total_orders,
        "avgOrderValue": round(avg_order, 2),
        "totalPagesPrinted": total_pages_printed,
        "storeRevenue": round(store_revenue, 2),
        "printRevenue": round(print_revenue, 2),
        "activeCustomers": len(customers),
        "statusCounts": status_counts,
        "serviceBreakdown": service_breakdown
    }