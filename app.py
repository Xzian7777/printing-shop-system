from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from firebase_config import (
    db, get_orders, create_order, update_order_status, 
    get_customers, create_customer, get_customer_profile, save_customer_profile,
    get_store_products, save_store_products, get_inventory,
    log_rfid_tap, get_notifications, create_notification, mark_all_notifications_read,
    delete_notification, delete_all_notifications, get_analytics_summary
)
import datetime

app = Flask(__name__)
app.secret_key = "printingshop_secret_key"

# In-memory store for chat messages thread
chat_memory_store = [
    {
        "id": "c1", 
        "customerId": "CUST-101", 
        "sender": "staff", 
        "message": "Hello! Paano po namin kayo matutulungan sa inyong print job o order ngayon?", 
        "time": "10:00 AM"
    }
]

# ================= PAGE ROUTES =================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json or request.form or {}
        username = data.get('username') or data.get('email')
        role = data.get('role', 'customer')
        
        if not username:
            return jsonify({
                "status": "error",
                "success": False,
                "message": "Kailangan ang email o username."
            }), 400

        username_clean = username.split('@')[0] if '@' in username else username
        session['username'] = username_clean
        session['email'] = username
        session['role'] = role

        redirect_target = "/admin" if role == 'admin' else "/customer"

        return jsonify({
            "status": "success",
            "success": True,
            "message": "Login successful!",
            "role": role,
            "redirect": redirect_target,
            "redirect_url": redirect_target
        })
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

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

@app.route('/store')
def store_page():
    return render_template('store.html')


# ================= API ENDPOINTS =================

@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = request.json or request.form or {}
        email = data.get('email') or data.get('username') or ''
        role = data.get('role', 'customer')
        
        if not email:
            return jsonify({"status": "error", "success": False, "message": "Email is required"}), 400
            
        username_clean = email.split('@')[0] if '@' in email else email
        session['username'] = username_clean
        session['email'] = email
        session['role'] = role
        
        redirect_target = "/admin" if role == 'admin' else "/customer"
        user_info = {
            "email": email,
            "role": role,
            "username": username_clean
        }
        
        return jsonify({
            "status": "success",
            "success": True, 
            "redirect": redirect_target,
            "redirect_url": redirect_target,
            "user": user_info
        })
    except Exception as e:
        return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/signup', methods=['POST'])
def api_signup():
    try:
        data = request.json or {}
        email = data.get('email')
        username = data.get('username') or (email.split('@')[0] if email else 'user')
        
        new_customer = {
            "id": f"CUST-{int(datetime.datetime.now().timestamp())}",
            "fullName": f"{data.get('firstName', '')} {data.get('lastName', '')}".strip(),
            "contact": "09170000000",
            "email": email,
            "username": username,
            "regDate": datetime.datetime.now().strftime('%Y-%m-%d'),
            "rfidUid": "CARD-NEW-PH"
        }
        create_customer(new_customer)
        return jsonify({"status": "success", "success": True, "message": "Account created successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/orders', methods=['GET', 'POST'])
def handle_orders():
    if request.method == 'POST':
        try:
            data = request.json or {}
            order_id = f"PRNT-{datetime.datetime.now().strftime('%M%S%f')[:6]}"
            
            cust_name = session.get('username') or data.get('customer') or 'Guest'
            if '@' in cust_name:
                cust_name = cust_name.split('@')[0]

            new_order = {
                "id": order_id,
                "customerId": data.get('customerId', 'CUST-101'),
                "customer": cust_name,
                "service": data.get('service', 'Standard A4 Document'),
                "pages": data.get('pages', 1),
                "copies": data.get('copies', 1),
                "paperSize": data.get('paperSize', 'Custom Specs'),
                "colorMode": data.get('colorMode', 'Color'),
                "sides": data.get('sides', 'Single'),
                "payment": data.get('payment', 'GCash'),
                "total": float(data.get('totalAmount') or data.get('total') or 0.0),
                "status": "Order Submitted",
                "file": data.get('file', 'Document.pdf'),
                "orderDate": data.get('orderDate') or datetime.datetime.now().strftime('%b %d, %Y • %I:%M %p'),
                "timestamp": datetime.datetime.now().isoformat()
            }
            
            create_order(new_order)
            return jsonify({"status": "success", "success": True, "orderId": order_id, "message": "Order submitted successfully!"})
        except Exception as e:
            return jsonify({"status": "error", "success": False, "error": str(e)}), 500

    elif request.method == 'GET':
        try:
            orders = get_orders()
            customer_filter = request.args.get('user') or request.args.get('customer')
            
            if customer_filter and customer_filter.lower() not in ['admin', 'staff', 'all']:
                orders = [
                    o for o in orders 
                    if str(o.get('customer', '')).lower() == customer_filter.lower() or 
                       str(o.get('customerId', '')).lower() == customer_filter.lower()
                ]
                
            return jsonify(orders)
        except Exception as e:
            return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/orders/<order_id>/status', methods=['PUT'])
def update_status(order_id):
    try:
        data = request.json or {}
        new_status = data.get('status')
        reason = data.get('reason', '')
        
        final_status = f"Rejected: {reason}" if new_status == 'Rejected' and reason else new_status
        
        orders = get_orders()
        target_order = next((o for o in orders if str(o.get('id')) == str(order_id)), None)
        
        update_order_status(order_id, final_status)
        
        if new_status in ['Approved', 'Completed'] and target_order:
            service_desc = target_order.get('service', '')
            products = get_store_products()
            stock_updated = False
            
            for prod in products:
                prod_name = prod.get('name', '')
                if prod_name and prod_name in service_desc:
                    qty_to_deduct = 1
                    if f"{prod_name} (" in service_desc:
                        try:
                            part = service_desc.split(f"{prod_name} (")[1]
                            qty_str = part.split('x)')[0]
                            qty_to_deduct = int(qty_str)
                        except Exception:
                            qty_to_deduct = int(target_order.get('copies', 1))
                    else:
                        qty_to_deduct = int(target_order.get('copies', 1))
                    
                    prod['stock'] = max(0, int(prod.get('stock', 0)) - qty_to_deduct)
                    stock_updated = True
            
            if stock_updated:
                save_store_products(products)

        return jsonify({"status": "success", "success": True})
    except Exception as e:
        return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/store/products', methods=['GET', 'PUT', 'POST'])
def handle_store_products():
    if request.method == 'GET':
        try:
            products = get_store_products()
            return jsonify(products)
        except Exception as e:
            return jsonify({"status": "error", "success": False, "error": str(e)}), 500

    elif request.method in ['PUT', 'POST']:
        try:
            data = request.json or {}
            products = data.get('products') if isinstance(data, dict) and 'products' in data else data
            if isinstance(products, list):
                save_store_products(products)
            return jsonify({"status": "success", "success": True})
        except Exception as e:
            return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/store/products/<prod_id>', methods=['DELETE'])
def delete_store_product_by_id(prod_id):
    try:
        prod_id_str = str(prod_id)
        
        # 1. Direkta munang burahin ang Document sa Firestore collections
        if db:
            try:
                db.collection('store_products').document(prod_id_str).delete()
            except Exception as e1:
                print(f"Firestore store_products delete error: {e1}")
            try:
                db.collection('products').document(prod_id_str).delete()
            except Exception as e2:
                print(f"Firestore products delete error: {e2}")

        # 2. Kunin ang kasalukuyang listahan at alisin ang produktong buburahin
        products = get_store_products() or []
        updated_products = [p for p in products if str(p.get('id')) != prod_id_str]
        
        # 3. I-save ang bagong listahan
        save_store_products(updated_products)
        
        return jsonify({
            "status": "success", 
            "success": True, 
            "message": f"Product #{prod_id} permanently deleted!"
        }), 200
    except Exception as e:
        print(f"Error deleting product: {e}")
        return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    try:
        summary = get_analytics_summary()
        return jsonify(summary)
    except Exception as e:
        return jsonify({"status": "error", "success": False, "error": str(e)}), 500

@app.route('/api/notifications', methods=['GET', 'POST'])
def handle_notifications():
    if request.method == 'GET':
        user_id = request.args.get('userId') or session.get('username') or 'CUSTOMER'
        if session.get('role') == 'admin':
            user_id = 'ADMIN'
        notifs = get_notifications(user_id)
        return jsonify(notifs)
    elif request.method == 'POST':
        data = request.json or {}
        notif = {
            "id": f"NOTIF-{int(datetime.datetime.now().timestamp())}",
            "userId": data.get('userId', 'CUSTOMER'),
            "notificationType": data.get('notificationType', 'Alert'),
            "message": data.get('message', ''),
            "time": "Just now",
            "status": "Unread"
        }
        create_notification(notif)
        return jsonify({"status": "success", "success": True})

@app.route('/api/notifications/read', methods=['POST'])
def read_all_notifications():
    user_id = request.args.get('userId') or (request.json or {}).get('userId') or session.get('username') or 'CUSTOMER'
    if session.get('role') == 'admin':
        user_id = 'ADMIN'
    mark_all_notifications_read(user_id)
    return jsonify({"status": "success", "success": True})

@app.route('/api/notifications/<notif_id>', methods=['DELETE'])
def remove_notification(notif_id):
    delete_notification(notif_id)
    return jsonify({"status": "success", "success": True})

@app.route('/api/notifications/all', methods=['DELETE'])
def remove_all_notifications():
    user_id = request.args.get('userId') or (request.json or {}).get('userId') or session.get('username') or 'CUSTOMER'
    if session.get('role') == 'admin':
        user_id = 'ADMIN'
    delete_all_notifications(user_id)
    return jsonify({"status": "success", "success": True})

@app.route('/api/chat', methods=['GET', 'POST'])
def handle_chat():
    if request.method == 'GET':
        return jsonify(chat_memory_store)
    elif request.method == 'POST':
        data = request.json or {}
        msg = {
            "id": f"chat-{int(datetime.datetime.now().timestamp())}",
            "customerId": data.get('customerId', 'CUST-101'),
            "sender": data.get('sender', 'customer'),
            "message": data.get('message', ''),
            "time": datetime.datetime.now().strftime('%I:%M %p')
        }
        chat_memory_store.append(msg)
        return jsonify({"status": "success", "success": True})

@app.route('/api/rfid/tap', methods=['POST'])
def rfid_tap():
    data = request.json or {}
    card_uid = data.get('rfidCardUid', 'CARD-8842-PH')
    log_rfid_tap({
        "id": f"TAP-{int(datetime.datetime.now().timestamp())}",
        "rfidCardUid": card_uid,
        "pointsAdded": 20,
        "timestamp": datetime.datetime.now().isoformat()
    })
    return jsonify({"status": "success", "success": True, "earnedPoints": 20})

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Kez C-Em Zek Backend Running on http://127.0.0.1:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)