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

# ---------------------------------------------------------------------------
# PAGE ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json or request.form or {}
        email = (data.get('email') or data.get('username') or 'user@example.com').strip()
        role = str(data.get('role', 'customer')).lower()
        
        is_admin = (role == 'admin') or ('admin' in email.lower())
        target_url = "/admin" if is_admin else "/customer"
        username_clean = email.split('@')[0] if '@' in email else email
        
        session['username'] = username_clean
        session['email'] = email
        session['role'] = 'admin' if is_admin else 'customer'
        
        return jsonify({
            "success": True,
            "status": "success",
            "redirect": target_url,
            "redirectUrl": target_url,
            "redirect_url": target_url,
            "url": target_url,
            "target": target_url,
            "message": "Login successful"
        })
        
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

@app.route('/store')
def store_page():
    return render_template('store.html')

# ---------------------------------------------------------------------------
# API ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = request.json or request.form or {}
        
        email = (data.get('email') or data.get('username') or data.get('user') or '').strip()
        password = (data.get('password') or data.get('pass') or '').strip()
        role = str(data.get('role', 'customer')).lower()
        
        is_admin = (role == 'admin') or ('admin' in email.lower())
        target_url = "/admin" if is_admin else "/customer"

        if is_admin:
            username_clean = "Admin"
            session['username'] = username_clean
            session['email'] = email if email else "admin@kezcemzek.com"
            session['role'] = "admin"
        else:
            username_clean = email.split('@')[0] if '@' in email else email
            session['username'] = username_clean
            session['email'] = email
            session['role'] = "customer"

        return jsonify({
            "success": True,
            "status": "success",
            "message": "Login Successful",
            "redirect": target_url,
            "redirectUrl": target_url,
            "redirect_url": target_url,
            "url": target_url,
            "target": target_url,
            "user": {"email": session['email'], "role": session['role'], "username": username_clean}
        }), 200

    except Exception as e:
        return jsonify({"success": False, "status": "error", "message": str(e)}), 200


@app.route('/api/signup', methods=['POST'])
def api_signup():
    try:
        data = request.json or request.form or {}
        email = data.get('email')
        username = data.get('username') or (email.split('@')[0] if email else 'user')
        
        new_customer = {
            "id": f"CUST-{int(datetime.datetime.now().timestamp())}",
            "fullName": f"{data.get('firstName', '')} {data.get('lastName', '')}".strip() or username,
            "contact": data.get('contact', '09170000000'),
            "email": email,
            "username": username,
            "regDate": datetime.datetime.now().strftime('%Y-%m-%d'),
            "rfidUid": "CARD-NEW-PH"
        }
        create_customer(new_customer)
        return jsonify({"success": True, "message": "Account created successfully!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/orders', methods=['GET', 'POST'])
def handle_orders():
    if request.method == 'POST':
        try:
            data = request.json or request.form.to_dict() or {}
            uploaded_file = request.files.get('file') or request.files.get('document')
            file_name = uploaded_file.filename if uploaded_file else data.get('file', 'Item_Purchase.pdf')

            order_id = f"PRNT-{datetime.datetime.now().strftime('%M%S%f')[:6]}"
            
            cust_name = session.get('username') or data.get('customer') or data.get('user') or 'Customer'
            if '@' in cust_name:
                cust_name = cust_name.split('@')[0]
                
            cust_email = session.get('email') or data.get('email') or f"{cust_name}@gmail.com"

            total_val = data.get('totalAmount') or data.get('total') or data.get('calculatedTotal')
            try:
                total_amount = float(total_val)
            except (TypeError, ValueError):
                total_amount = 35.00

            file_url_val = data.get('fileUrl') or data.get('file_url') or f"Local Attached File: {file_name}"

            qty_val = int(data.get('quantity') or data.get('copies') or 1)

            new_order = {
                "id": order_id,
                "customerId": data.get('customerId', 'CUST-101'),
                "customer": cust_name,
                "email": cust_email,
                "service": data.get('service') or data.get('selectedService') or 'Store Supply Item',
                "quantity": qty_val,
                "copies": qty_val,
                "pages": int(data.get('pages', 1)),
                "paperSize": data.get('paperSize') or data.get('size') or 'Store Supply',
                "colorMode": data.get('colorMode', 'N/A'),
                "sides": data.get('sides', 'N/A'),
                "payment": data.get('payment') or data.get('paymentMethod') or 'GCash Online',
                "total": total_amount,
                "status": "Order Submitted",
                "file": file_name,
                "fileUrl": file_url_val,
                "orderDate": datetime.datetime.now().strftime('%b %d, %Y • %I:%M %p'),
                "createdAt": datetime.datetime.now().isoformat(),
                "timestamp": datetime.datetime.now().isoformat()
            }
            
            create_order(new_order)

            # Notification
            try:
                create_notification({
                    "id": f"NOTIF-{int(datetime.datetime.now().timestamp())}",
                    "userId": "ADMIN",
                    "notificationType": "New Order",
                    "message": f"Bagong order {order_id} mula kay {cust_name} ({new_order['service']})",
                    "time": "Just now",
                    "status": "Unread"
                })
            except Exception:
                pass

            return jsonify({"success": True, "orderId": order_id, "message": "Order submitted successfully!"}), 200
            
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method == 'GET':
        try:
            orders = get_orders()
            customer_filter = request.args.get('user') or request.args.get('customer') or request.args.get('email')
            
            if customer_filter and customer_filter.lower() not in ['admin', 'staff', 'all']:
                filter_clean = customer_filter.lower().split('@')[0]
                orders = [
                    o for o in orders 
                    if filter_clean in str(o.get('customer', '')).lower() or 
                    filter_clean in str(o.get('email', '')).lower() or
                    filter_clean in str(o.get('customerId', '')).lower()
                ]
            
            orders.sort(key=lambda x: str(x.get('createdAt') or x.get('timestamp') or x.get('orderDate') or ''))
            return jsonify(orders), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/orders/<order_id>/status', methods=['PUT', 'POST'])
def update_status(order_id):
    try:
        data = request.json or request.form or {}
        new_status = data.get('status')
        reason = data.get('reason', '')
        
        final_status = f"Rejected: {reason}" if new_status == 'Rejected' and reason else new_status
        
        # Isasagawa ang update at automatic stock deduction
        success = update_order_status(order_id, final_status)

        if success:
            return jsonify({"success": True, "message": f"Order status updated to {final_status}"}), 200
        else:
            return jsonify({"success": False, "error": "Order not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/customers', methods=['GET'])
def get_customers_api():
    try:
        customers = get_customers()
        return jsonify(customers), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/store/products', methods=['GET', 'PUT', 'POST'])
def handle_store_products():
    if request.method == 'GET':
        try:
            products = get_store_products()
            return jsonify(products), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    elif request.method in ['PUT', 'POST']:
        try:
            data = request.json or request.form or {}
            products = data.get('products') if isinstance(data, dict) and 'products' in data else data
            if isinstance(products, list):
                save_store_products(products)
            return jsonify({"success": True}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    try:
        summary = get_analytics_summary()
        return jsonify(summary), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/notifications', methods=['GET', 'POST'])
def handle_notifications():
    if request.method == 'GET':
        user_id = request.args.get('userId') or session.get('username') or 'CUSTOMER'
        if session.get('role') == 'admin':
            user_id = 'ADMIN'
        notifs = get_notifications(user_id)
        return jsonify(notifs), 200
    elif request.method == 'POST':
        data = request.json or request.form or {}
        notif = {
            "id": f"NOTIF-{int(datetime.datetime.now().timestamp())}",
            "userId": data.get('userId', 'CUSTOMER'),
            "notificationType": data.get('notificationType', 'Alert'),
            "message": data.get('message', ''),
            "time": "Just now",
            "status": "Unread"
        }
        create_notification(notif)
        return jsonify({"success": True}), 200


@app.route('/api/notifications/read', methods=['POST'])
def read_all_notifications():
    user_id = request.args.get('userId') or (request.json or {}).get('userId') or session.get('username') or 'CUSTOMER'
    if session.get('role') == 'admin':
        user_id = 'ADMIN'
    mark_all_notifications_read(user_id)
    return jsonify({"success": True}), 200


@app.route('/api/notifications/<notif_id>', methods=['DELETE'])
def remove_notification(notif_id):
    delete_notification(notif_id)
    return jsonify({"success": True}), 200


@app.route('/api/notifications/all', methods=['DELETE'])
def remove_all_notifications():
    user_id = request.args.get('userId') or (request.json or {}).get('userId') or session.get('username') or 'CUSTOMER'
    if session.get('role') == 'admin':
        user_id = 'ADMIN'
    delete_all_notifications(user_id)
    return jsonify({"success": True}), 200


@app.route('/api/chat', methods=['GET', 'POST'])
def handle_chat():
    if request.method == 'GET':
        return jsonify(chat_memory_store), 200
    elif request.method == 'POST':
        data = request.json or request.form or {}
        msg = {
            "id": f"chat-{int(datetime.datetime.now().timestamp())}",
            "customerId": data.get('customerId', 'CUST-101'),
            "sender": data.get('sender', 'customer'),
            "message": data.get('message', ''),
            "time": datetime.datetime.now().strftime('%I:%M %p')
        }
        chat_memory_store.append(msg)
        return jsonify({"success": True}), 200


@app.route('/api/rfid/tap', methods=['POST'])
def rfid_tap():
    data = request.json or request.form or {}
    card_uid = data.get('rfidCardUid', 'CARD-8842-PH')
    log_rfid_tap({
        "id": f"TAP-{int(datetime.datetime.now().timestamp())}",
        "rfidCardUid": card_uid,
        "pointsAdded": 20,
        "timestamp": datetime.datetime.now().isoformat()
    })
    return jsonify({"success": True, "earnedPoints": 20}), 200


if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Kez C-Em Zek Backend Running on http://127.0.0.1:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)