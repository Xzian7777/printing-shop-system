from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from firebase_config import (
    db, get_orders, create_order, update_order_status, 
    get_customers, create_customer, get_customer_profile, save_customer_profile,
    create_firebase_customer, delete_firebase_customer,
    get_store_products, save_store_products, get_inventory,
    log_rfid_tap, get_notifications, create_notification, mark_all_notifications_read,
    delete_notification, delete_all_notifications
)
from firebase_admin import auth as firebase_auth
import datetime
import smtplib
import random
import string
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "printingshop_secret_key"

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

# In-memory storage para sa OTPs
otp_memory_store = {}
signup_otp_store = {}

# ---------------------------------------------------------------------------
# BACKEND API SECURITY DECORATORS
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'email' not in session:
            return jsonify({"success": False, "status": "unauthorized", "message": "Unauthorized! Mag-login muna bago gamitin ang API na ito."}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            return jsonify({"success": False, "status": "forbidden", "message": "Forbidden! Para lamang sa Admin ang action na ito."}), 403
        return f(*args, **kwargs)
    return decorated_function

# ---------------------------------------------------------------------------
# STRICT FIELD VALIDATION HELPERS
# ---------------------------------------------------------------------------
def is_valid_gmail(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@gmail\.com$'
    return bool(re.match(pattern, email.strip().lower()))

def is_valid_ph_contact(contact):
    pattern = r'^09\d{9}$'
    return bool(re.match(pattern, contact.strip()))

def validate_strong_password(password):
    if len(password) < 8:
        return False, "Ang password ay kailangang may minimum na 8 characters."
    if not re.search(r'[A-Z]', password):
        return False, "Kailangang may kahit isang Uppercase letter (A-Z)."
    if not re.search(r'[a-z]', password):
        return False, "Kailangang may kahit isang Lowercase letter (a-z)."
    if not re.search(r'\d', password):
        return False, "Kailangang may kahit isang numero (0-9)."
    if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-\+=]', password):
        return False, "Kailangang may kahit isang Special Character (hal. @, #, $, !, %)."
    return True, "Valid"

def is_email_registered(email):
    try:
        if email.strip().lower() == ADMIN_EMAIL:
            return True

        customers = get_customers() or []
        email_clean = email.strip().lower()
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email_clean:
                return True
        return False
    except Exception as e:
        print(f"⚠ Database Check Error: {e}")
        return False

def format_proper_order_info(order):
    try:
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
        email = (data.get('email') or data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"success": False, "message": "Kailangan ang email at password!"}), 400

        if email == ADMIN_EMAIL:
            if password == ADMIN_PASSWORD:
                session['username'] = "Admin"
                session['email'] = ADMIN_EMAIL
                session['role'] = 'admin'
                return jsonify({"success": True, "status": "success", "redirect": "/admin", "message": "Admin Login successful"})
            else:
                return jsonify({"success": False, "message": "Mali ang Admin password!"}), 400

        customers = get_customers() or []
        user_found = None
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                user_found = c
                break

        if not user_found or str(user_found.get('password', '')) != password:
            return jsonify({"success": False, "message": "Mali ang email o password!"}), 400

        username_clean = user_found.get('fullName') or (email.split('@')[0] if '@' in email else email)
        session['username'] = username_clean
        session['email'] = email
        session['role'] = 'customer'
        
        return jsonify({
            "success": True,
            "status": "success",
            "redirect": "/customer",
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
# AUTHENTICATION & SMTP API ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = request.json or request.form or {}
        email = (data.get('email') or data.get('username') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"success": False, "status": "error", "message": "Pakilagay ang email at password."}), 400

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
                return jsonify({"success": False, "status": "error", "message": "Mali ang password ng Admin!"}), 400

        customers = get_customers() or []
        user_found = None

        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                user_found = c
                break

        if not user_found:
            return jsonify({"success": False, "status": "error", "message": "Hindi nakarehistro ang email na ito."}), 400

        stored_password = str(user_found.get('password', ''))
        if stored_password != password:
            return jsonify({"success": False, "status": "error", "message": "Mali ang password! Paki-subukan ulit."}), 400

        username_clean = user_found.get('fullName') or email.split('@')[0]
        session['username'] = username_clean
        session['email'] = email
        session['role'] = "customer"

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


@app.route('/api/google-login', methods=['POST'])
def api_google_login():
    try:
        data = request.json or {}
        id_token = data.get('idToken')
        client_email = (data.get('email') or '').strip().lower()
        client_name = (data.get('fullName') or '').strip()
        client_uid = (data.get('uid') or '').strip()

        if not id_token and not client_email:
            return jsonify({"success": False, "message": "Walang natanggap na Google token o email."}), 400

        email = client_email
        full_name = client_name
        uid = client_uid

        # Verify Google ID token gamit ang Firebase Admin SDK
        if id_token:
            try:
                decoded_token = firebase_auth.verify_id_token(id_token)
                email = decoded_token.get('email', email).strip().lower()
                uid = decoded_token.get('uid', uid)
                if not full_name:
                    full_name = decoded_token.get('name', '')
            except Exception as e:
                print(f"Token verification note: {e}")
                if not email:
                    return jsonify({"success": False, "message": f"Hindi ma-verify ang Google Token: {str(e)}"}), 401

        if not email:
            return jsonify({"success": False, "message": "Walang natagpuang email sa Google account."}), 400

        # Check kung Admin account ang nag-sign in
        if ADMIN_EMAIL and email == ADMIN_EMAIL:
            session['username'] = "Admin"
            session['email'] = ADMIN_EMAIL
            session['role'] = 'admin'
            return jsonify({
                "success": True,
                "status": "success",
                "message": "Admin Google Login Successful",
                "redirect": "/admin",
                "redirectUrl": "/admin",
                "user": {"email": session['email'], "role": session['role'], "username": "Admin"}
            }), 200

        # Customer account handling
        customers = get_customers() or []
        user_found = None
        for c in customers:
            if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                user_found = c
                break

        # Kung bagong user, awtomatikong irehistro sa Firestore
        if not user_found:
            username_part = email.split('@')[0]
            display_name = full_name if full_name else username_part
            new_customer = {
                "id": f"CUST-{int(datetime.datetime.now().timestamp())}",
                "uid": uid or f"google-{int(datetime.datetime.now().timestamp())}",
                "fullName": display_name,
                "contact": "",
                "email": email,
                "username": username_part,
                "regDate": datetime.datetime.now().strftime('%Y-%m-%d • %I:%M %p'),
                "rfidUid": f"RFID-KCZ-{random.randint(100000, 999999)}",
                "authProvider": "google"
            }
            create_customer(new_customer)
            user_found = new_customer

        username_clean = user_found.get('fullName') or email.split('@')[0]
        session['username'] = username_clean
        session['email'] = email
        session['role'] = 'customer'

        return jsonify({
            "success": True,
            "status": "success",
            "message": "Google Login Successful",
            "redirect": "/customer",
            "redirectUrl": "/customer",
            "user": {"email": session['email'], "role": session['role'], "username": username_clean}
        }), 200

    except Exception as e:
        print(f"Error in api_google_login: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/request-signup-otp', methods=['POST'])
def request_signup_otp():
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "message": "Ipasok ang iyong email address."}), 400

        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Maling email format! Siguraduhing valid na @gmail.com address."}), 400

        if is_email_registered(email):
            return jsonify({"success": False, "message": "Ang email na ito ay NAKAREHISTRO NA sa aming system."}), 400

        otp = ''.join(random.choices(string.digits, k=6))
        signup_otp_store[email] = {
            "otp": otp,
            "timestamp": datetime.datetime.now()
        }

        otp_html = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px; max-width: 500px;">
            <h2 style="color: #c026d3; text-align: center;">Kez C-Em Zek Print Shop</h2>
            <h3 style="color: #1e293b;">Account Registration Verification Code</h3>
            <p>Gamitin ang 6-digit OTP code sa ibaba para kumpirmahin ang paggawa ng iyong account:</p>
            <div style="background: #f1f5f9; padding: 15px; text-align: center; font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #c026d3; border-radius: 8px;">
                {otp}
            </div>
            <p style="font-size: 11px; color: #64748b; margin-top: 15px;">Ang code na ito ay mag-eexpire sa loob ng 5 minuto.</p>
        </div>
        """

        email_sent = send_smtp_email(email, "Sign Up Verification Code - Kez C-Em Zek", otp_html)

        if email_sent:
            return jsonify({"success": True, "message": f"Napadala na ang verification code sa {email}."}), 200
        else:
            return jsonify({"success": False, "message": "Bigo sa pagpapadala ng verification email. Paki-check ang SMTP configuration."}), 500

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

        if not full_name or len(full_name) < 2:
            return jsonify({"success": False, "message": "Pakilagay ang kumpletong pangalan (minimum 2 letters)."}), 400

        if not email or not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Maling email format! Dapat ay valid na @gmail.com address."}), 400

        if not is_valid_ph_contact(contact):
            return jsonify({"success": False, "message": "Maling contact format! Dapat ay 11-digit PH mobile number na nagsisimula sa 09."}), 400

        if not confirm_password:
            return jsonify({"success": False, "message": "Pakilagay ang Confirm Password."}), 400

        if password != confirm_password:
            return jsonify({"success": False, "message": "Hindi magkapareho ang Password at Confirm Password!"}), 400

        is_strong, pass_msg = validate_strong_password(password)
        if not is_strong:
            return jsonify({"success": False, "message": f"Mahina ang Password: {pass_msg}"}), 400

        if is_email_registered(email):
            return jsonify({"success": False, "message": "May nakarehistro nang account gamit ang email na ito."}), 400

        stored = signup_otp_store.get(email)
        if stored and stored['otp'] == otp_input:
            del signup_otp_store[email]
        else:
            return jsonify({"success": False, "message": "Mali o expired ang OTP verification code na ipinasok mo."}), 400

                # Create account in Firebase Authentication
        firebase_uid = create_firebase_customer(email, password)

        if not firebase_uid:
            return jsonify({
                "success": False,
                "message": "Hindi ma-create ang Firebase Authentication account. Maaaring registered na ang email."
            }), 400

                # Create user in Firebase Authentication
        firebase_uid = create_firebase_customer(email, password)

        if not firebase_uid:
            return jsonify({
                "success": False,
                "message": "Hindi ma-create ang Firebase Authentication account. Maaaring registered na ang email."
            }), 400

        # Save customer profile only — NO PASSWORD
        new_customer = {
            "id": f"CUST-{int(datetime.datetime.now().timestamp())}",
            "uid": firebase_uid,
            "fullName": full_name,
            "contact": contact,
            "email": email,
            "username": email.split('@')[0],
            "regDate": datetime.datetime.now().strftime('%Y-%m-%d • %I:%M %p'),
            "rfidUid": f"RFID-KCZ-{random.randint(100000, 999999)}"
        }

        if not create_customer(new_customer):
            # Roll back Firebase Authentication account
            delete_firebase_customer(firebase_uid)

            return jsonify({
                "success": False,
                "message": "Hindi ma-save ang customer profile."
            }), 500

        welcome_html = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px; max-width: 500px;">
            <h2 style="color: #c026d3;">Welcome sa Kez C-Em Zek Print Shop! 🎉</h2>
            <p>Hi <b>{full_name}</b>,</p>
            <p>Matagumpay na na-verify at naipasa ang iyong bagong account.</p>
            <br>
            <p><b>Registered Email:</b> {email}</p>
            <p><b>Contact Number:</b> {contact}</p>
        </div>
        """
        send_smtp_email(email, "Welcome sa Kez C-Em Zek Print Shop!", welcome_html)

        return jsonify({"success": True, "message": "Account created successfully! Maaari ka nang mag-login."}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    try:
        data = request.json or {}
        email = data.get('email', '').strip().lower()

        if not email:
            return jsonify({"success": False, "message": "Ipasok ang iyong email address."}), 400

        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Maling email format! Siguraduhing valid na @gmail.com address."}), 400

        if not is_email_registered(email):
            return jsonify({"success": False, "message": "Ang email na ito ay HINDI pa nakarehistro sa aming database."}), 400

        otp = ''.join(random.choices(string.digits, k=6))
        otp_memory_store[email] = {
            "otp": otp,
            "timestamp": datetime.datetime.now()
        }

        email_body = f"""
        <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #e2e8f0; border-radius: 12px; max-width: 500px;">
            <h2 style="color: #e11d48; text-align: center;">Kez C-Em Zek Print Shop</h2>
            <h3 style="color: #1e293b;">Password Reset OTP Request</h3>
            <p>Ginamit ang email na ito para mag-request ng password reset. Gamitin ang OTP code sa ibaba:</p>
            <div style="background: #f1f5f9; padding: 15px; text-align: center; font-size: 24px; font-weight: bold; letter-spacing: 4px; color: #0284c7; border-radius: 8px;">
                {otp}
            </div>
            <p style="font-size: 11px; color: #64748b; margin-top: 15px;">Kung hindi mo ito ni-request, balewalain lamang ang email na ito.</p>
        </div>
        """

        email_sent = send_smtp_email(email, "Your Password Reset OTP - Kez C-Em Zek", email_body)

        if email_sent:
            return jsonify({"success": True, "message": f"Napadala na ang Password Reset OTP sa {email}."}), 200
        else:
            return jsonify({"success": False, "message": "Bigo sa pagpapadala ng OTP via SMTP."}), 500

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
            return jsonify({"success": False, "message": "Kumpletuhin ang lahat ng required fields."}), 400

        is_strong, pass_msg = validate_strong_password(new_password)
        if not is_strong:
            return jsonify({"success": False, "message": f"Mahina ang Bagong Password: {pass_msg}"}), 400

        if email == ADMIN_EMAIL:
            stored_admin = otp_memory_store.get(email)
            if stored_admin and stored_admin['otp'] == otp_input:
                global ADMIN_PASSWORD
                ADMIN_PASSWORD = new_password
                del otp_memory_store[email]
                return jsonify({"success": True, "message": "Matagumpay na pinalitan ang Admin Password!"}), 200

        stored = otp_memory_store.get(email)

        if stored and stored['otp'] == otp_input:
            updated_in_db = False
            try:
                customers_ref = db.collection('customers')
                docs = customers_ref.where('email', '==', email).stream()
                for doc in docs:
                    doc.reference.update({'password': new_password})
                    updated_in_db = True
            except Exception as db_err:
                print(f"⚠ Firebase Update Error: {db_err}")

            customers = get_customers() or []
            for c in customers:
                if isinstance(c, dict) and c.get('email', '').strip().lower() == email:
                    c['password'] = new_password
                    updated_in_db = True
                    break

            del otp_memory_store[email]

            if updated_in_db:
                return jsonify({"success": True, "message": "Matagumpay na pinalitan ang password sa database! Maaari ka nang mag-login."}), 200
            else:
                return jsonify({"success": False, "message": "Hindi nahanap ang customer record sa database."}), 400
        else:
            return jsonify({"success": False, "message": "Mali ang OTP code na ipinasok mo."}), 400

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
                            "role": c.get('role', 'customer')
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
                            "role": c.get('role', 'customer')
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

            user_id = f"CUST-{random.randint(100, 999)}"
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
                    db.collection('customers').document(user_id).set(new_customer)
            except Exception as dbe:
                print(f"Firestore Create Customer Warning: {dbe}")

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
                    doc_ref.update(update_data)
                    updated = True
                else:
                    # Query by 'id' field
                    docs = db.collection('customers').where('id', '==', user_id).stream()
                    for doc in docs:
                        doc.reference.update(update_data)
                        updated = True

            return jsonify({
                "success": True,
                "status": "success",
                "message": f"User {user_id} profile updated successfully!"
            }), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    elif request.method == 'DELETE':
        # DELETE USER (ADMIN CRUD)
        try:
            deleted = False
            
            # 1. Subukang burahin gamit ang Firebase Realtime Database reference kung naka-configure
            try:
                user_ref = db.reference(f'customers/{user_id}')
                if user_ref.get():
                    user_ref.delete()
                    deleted = True
            except Exception:
                pass

            # 2. Subukang burahin kung Firestore ang ginagamit
            try:
                if db:
                    doc_ref = db.collection('customers').document(user_id)
                    if doc_ref.get().exists:
                        doc_ref.delete()
                        deleted = True
                    else:
                        customers_ref = db.collection('customers')
                        docs = customers_ref.where('id', '==', user_id).stream()
                        for doc in docs:
                            doc.reference.delete()
                            deleted = True
            except Exception as db_err:
                print(f"⚠ Database User Delete Error: {db_err}")

            return jsonify({"success": True, "status": "success", "message": f"User {user_id} successfully deleted from database."}), 200
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
            return jsonify({"success": False, "message": "Unauthorized! Admin lamang ang pwedeng magbago ng store inventory."}), 403

        try:
            data = request.json or {}
            products_list = data.get('products', [])
            success = save_store_products(products_list)
            return jsonify({"success": success, "message": "Store inventory updated successfully!"}), 200
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# ORDERS & ANALYTICS ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/orders', methods=['GET', 'POST'])
@login_required
def handle_orders():
    if request.method == 'POST':
        try:
            data = request.json or request.form.to_dict() or {}
            uploaded_file = request.files.get('file') or request.files.get('document')
            file_name = uploaded_file.filename if uploaded_file else data.get('file', '')

            if not file_name or file_name.strip() == '':
                return jsonify({"success": False, "message": "Kailangan mag-upload ng document o design file bago i-submit ang order."}), 400

            try:
                qty_val = int(data.get('quantity') or data.get('copies') or 1)
                pages_val = int(data.get('pages') or 1)
                total_amount = float(data.get('totalAmount') or data.get('total') or 0.0)

                if qty_val <= 0 or pages_val <= 0:
                    return jsonify({"success": False, "message": "Ang quantity at pages ay dapat mas mataas sa zero (0)."}), 400
                if total_amount <= 0:
                    return jsonify({"success": False, "message": "Invalid calculated order total."}), 400
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "May maling numeric values sa quantity, pages, o total."}), 400

            order_id = f"PRNT-{datetime.datetime.now().strftime('%M%S%f')[:6]}"
            cust_name = session.get('username') or data.get('customer') or 'Customer'
            if '@' in cust_name: cust_name = cust_name.split('@')[0]
            cust_email = session.get('email') or data.get('email') or f"{cust_name}@gmail.com"

            file_url_val = data.get('fileUrl') or f"Local Attached File: {file_name}"

            new_order = {
                "id": order_id,
                "customerId": data.get('customerId', 'CUST-101'),
                "customer": cust_name,
                "email": cust_email,
                "service": data.get('service') or 'Document Printing (Full Color)',
                "quantity": qty_val,
                "copies": qty_val,
                "pages": pages_val,
                "paperSize": data.get('paperSize') or 'Short Letter (8.5" x 11")',
                "colorMode": data.get('colorMode', 'Full Color'),
                "payment": data.get('payment') or 'GCash Online Payment',
                "total": total_amount,
                "status": "Order Submitted",
                "file": file_name,
                "fileUrl": file_url_val,
                "orderDate": datetime.datetime.now().strftime('%b %d, %Y • %I:%M %p'),
                "createdAt": datetime.datetime.now().isoformat(),
                "timestamp": datetime.datetime.now().isoformat()
            }
            formatted_new_order = format_proper_order_info(new_order)
            create_order(formatted_new_order)
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

@app.route('/api/orders/<order_id>/status', methods=['PUT', 'POST'])
@admin_required
def update_status(order_id):
    try:
        data = request.json or {}
        new_status = (data.get('status') or '').strip()
        reason = (data.get('reason') or '').strip()

        if 'reject' in new_status.lower() and (not reason or len(reason) < 4):
            return jsonify({"success": False, "message": "Kailangan maglagay ng malinaw na dahilan para sa pag-reject ng order (minimum 4 characters)."}), 400

        final_status = f"Rejected: {reason}" if 'reject' in new_status.lower() and reason else new_status
        success = update_order_status(order_id, final_status)
        return jsonify({"success": success, "message": f"Order status updated to {final_status}"}), 200
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

            total_sales += price
            total_pages += (copies * pages_per_copy)
            if cust: customers.add(str(cust).lower().strip())

            clean_status = "Order Submitted"
            s_lower = str(status).lower()
            if 'reject' in s_lower: clean_status = "Rejected"
            elif 'complete' in s_lower: clean_status = "Completed"
            elif 'ready' in s_lower or 'pickup' in s_lower: clean_status = "Ready for Pickup"
            elif 'print' in s_lower: clean_status = "Printing"
            elif 'approve' in s_lower: clean_status = "Approved"

            status_counts[clean_status] = status_counts.get(clean_status, 0) + 1
            service_breakdown[service] = service_breakdown.get(service, 0) + 1

            if any(k in service.lower() for k in ['store', 'supply', 'in-store', 'notebook', 'pen']):
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

@app.route('/api/chat', methods=['GET', 'POST'])
@login_required
def handle_chat():
    if request.method == 'GET':
        return jsonify(chat_memory_store), 200
    elif request.method == 'POST':
        data = request.json or {}
        message_text = (data.get('message') or '').strip()

        if not message_text:
            return jsonify({"success": False, "message": "Bawal magpadala ng blangkong chat message."}), 400

        msg = {
            "id": f"chat-{int(datetime.datetime.now().timestamp())}",
            "customerId": data.get('customerId', 'CUST-101'),
            "sender": data.get('sender', 'customer'),
            "message": message_text,
            "time": datetime.datetime.now().strftime('%I:%M %p')
        }
        chat_memory_store.append(msg)
        return jsonify({"success": True}), 200

@app.route('/api/rfid/tap', methods=['POST'])
@login_required
def handle_rfid_tap():
    try:
        data = request.json or {}
        card_uid = data.get('rfidCardUid', 'CARD-8842-PH')
        earned_points = 20
        log_rfid_tap(card_uid, earned_points)
        return jsonify({"success": True, "earnedPoints": earned_points, "message": f"RFID Card {card_uid} successfully logged."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Kez C-Em Zek Backend Running on http://127.0.0.1:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)