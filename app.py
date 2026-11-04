from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from firebase_config import (
    db, get_orders, create_order, update_order_status, 
    get_customers, create_customer, get_customer_profile, save_customer_profile,
    create_firebase_customer, delete_firebase_customer,
    update_firebase_password, get_firebase_user_by_email,
    get_store_products, save_store_products, delete_store_product, get_inventory,
    get_sample_products, save_sample_product, delete_sample_product,
    get_services, save_service, delete_service,
    get_gcash_settings, save_gcash_settings,
    archive_item, get_archived_items, restore_archived_item, permanently_delete_archived_item,
    log_rfid_tap, get_notifications, create_notification, mark_all_notifications_read,
    delete_notification, delete_all_notifications,
    get_safe_conversation_id, get_or_create_conversation, get_all_conversations,
    get_conversation_messages, send_chat_message, seed_initial_conversations_if_needed,
    register_fcm_token, remove_fcm_token, send_fcm_push_notification
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
import supabase_config as sc

load_dotenv()

app = Flask(__name__)
app.secret_key = "printingshop_secret_key"
# Allow up to 30 MB per request (25 MB combined upload limit + multipart overhead)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024

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

import requests

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
# STRICT FIELD VALIDATION HELPERS (MATCHING FLUTTER APPVALIDATORS)
# ---------------------------------------------------------------------------
def validate_name(name):
    if not name or not name.strip():
        return False, "Mangyaring ilagay ang iyong buong pangalan."
    trimmed = name.strip()
    if len(trimmed) < 2:
        return False, "Ang pangalan ay dapat higit sa 1 character."
    if len(trimmed) > 50:
        return False, "Ang pangalan ay hindi pwedeng lumagpas sa 50 characters."
    name_regex = r"^[a-zA-Z\s\-\.\ñ\Ñ]+$"
    if not re.match(name_regex, trimmed):
        return False, "Lihis o bawal na character sa pangalan."
    return True, "Valid"

def is_valid_gmail(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@gmail\.com$'
    return bool(re.match(pattern, (email or '').strip().lower()))

def is_valid_ph_contact(contact):
    pattern = r'^09\d{9}$'
    return bool(re.match(pattern, (contact or '').strip()))

def validate_strong_password(password, full_name='', email=''):
    if not password:
        return False, "Required ang password."
    if len(password) < 8:
        return False, "Ang password ay kailangang may minimum na 8 characters."
    if len(password) > 32:
        return False, "Ang password ay hindi pwedeng lumagpas sa 32 characters."
    if not re.search(r'[A-Z]', password) or not re.search(r'[a-z]', password):
        return False, "Kailangang may Uppercase (A-Z) at Lowercase (a-z) letters."
    if not re.search(r'[0-9!@#$%^&*(),.?":{}|<>_\-\+=]', password):
        return False, "Kailangang may kahit isang numero o simbolo."
    if ' ' in password:
        return False, "Bawal ang space sa password."

    # Flutter req5: Can't include name or email prefix
    name_lower = full_name.strip().lower() if full_name else ''
    email_prefix = email.strip().split('@')[0].lower() if email else ''
    pass_lower = password.lower()

    if name_lower and len(name_lower) >= 2 and name_lower in pass_lower:
        return False, "Hindi maaaring naglalaman ang password ng iyong pangalan."
    if email_prefix and len(email_prefix) >= 2 and email_prefix in pass_lower:
        return False, "Hindi maaaring naglalaman ang password ng iyong email prefix."

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
            return jsonify({"success": False, "message": "Kailangan ang email at password!"}), 400

        if email == ADMIN_EMAIL:
            if password == ADMIN_PASSWORD:
                session['username'] = "Admin"
                session['email'] = ADMIN_EMAIL
                session['role'] = 'admin'
                return jsonify({"success": True, "status": "success", "redirect": "/admin", "message": "Admin Login successful"})
            else:
                return jsonify({"success": False, "message": "Mali ang Admin password!"}), 400

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

        return jsonify({"success": False, "message": "Mali ang email o password!"}), 400
        
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
            return jsonify({"success": False, "status": "error", "message": "Hindi nakarehistro ang email na ito."}), 400

        stored_password = str(user_found.get('password', ''))
        if stored_password != password:
            return jsonify({"success": False, "status": "error", "message": "Mali ang password! Paki-subukan ulit."}), 400

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

        now = datetime.datetime.now()

        # Rate limiting: 60 seconds cooldown between resend requests
        existing = signup_otp_store.get(email)
        if existing:
            last_sent = existing.get("timestamp")
            if last_sent and (now - last_sent).total_seconds() < 60:
                wait_sec = int(60 - (now - last_sent).total_seconds())
                return jsonify({
                    "success": False,
                    "message": f"Paki-antay ng {wait_sec} segundo bago humingi ng bagong verification code."
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
                "message": f"Napadala na ang 6-digit verification code sa {email}."
            }), 200
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

        # Strict Name Validation (AppValidators)
        is_name_valid, name_msg = validate_name(full_name)
        if not is_name_valid:
            return jsonify({"success": False, "message": name_msg}), 400

        # Strict Contact Validation (PH 11 digits starting with 09)
        if not is_valid_ph_contact(contact):
            return jsonify({"success": False, "message": "Dapat ay 11-digit PH mobile number (hal. 09123456789)."}), 400

        # Strict Gmail Validation
        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Maling email format! Dapat ay valid na @gmail.com address."}), 400

        if not confirm_password:
            return jsonify({"success": False, "message": "Pakilagay ang Confirm Password."}), 400

        if password != confirm_password:
            return jsonify({"success": False, "message": "Hindi magkapareho ang Password at Confirm Password!"}), 400

        # Strict Strong Password Validation (Matching Flutter Requirements)
        is_strong, pass_msg = validate_strong_password(password, full_name=full_name, email=email)
        if not is_strong:
            return jsonify({"success": False, "message": f"Password requirement error: {pass_msg}"}), 400

        # Pre-check email existence
        if is_email_registered(email):
            return jsonify({"success": False, "message": "May nakarehistro nang account gamit ang email na ito."}), 400

        # Validate OTP, expiration, and attempt limits
        stored = signup_otp_store.get(email)
        if not stored:
            return jsonify({
                "success": False, 
                "message": "Walang pending verification para sa email na ito o nag-expire na. Humingi muli ng bagong code."
            }), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            signup_otp_store.pop(email, None)
            return jsonify({
                "success": False,
                "message": "Expired na ang OTP code (5 minutes limit). Humingi ng bagong code."
            }), 400

        # Limit verification attempts to 5
        stored["attempts"] = stored.get("attempts", 0) + 1
        if stored["attempts"] > 5:
            signup_otp_store.pop(email, None)
            return jsonify({
                "success": False,
                "message": "Lumampas na sa pinapayagang verification attempts. Humingi ng panibagong code."
            }), 429

        if stored['otp'] != otp_input:
            remaining_attempts = max(0, 5 - stored["attempts"])
            return jsonify({
                "success": False,
                "message": f"Maling OTP verification code. ({remaining_attempts} attempt(s) remaining)"
            }), 400

        # OTP is verified! Remove from store to prevent replay attacks
        signup_otp_store.pop(email, None)

        # 1. Create account in Firebase Authentication
        firebase_uid = create_firebase_customer(email, password)
        if not firebase_uid:
            return jsonify({
                "success": False,
                "message": "Hindi ma-create ang Firebase Authentication account. Maaaring registered na ang email."
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
                "message": "Hindi ma-save ang customer profile sa database."
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
            <p>Matagumpay na na-verify at naipasa ang iyong bagong account.</p>
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
            return jsonify({"success": False, "message": "Ipasok ang iyong email address."}), 400

        if not is_valid_gmail(email):
            return jsonify({"success": False, "message": "Maling email format! Siguraduhing valid na @gmail.com address."}), 400

        if not is_email_registered(email):
            return jsonify({"success": False, "message": "Ang email na ito ay HINDI pa nakarehistro sa aming database."}), 400

        now = datetime.datetime.now()

        # Rate limiting: 60 seconds cooldown between resend requests
        existing = otp_memory_store.get(email)
        if existing:
            last_sent = existing.get("timestamp")
            if last_sent and (now - last_sent).total_seconds() < 60:
                wait_sec = int(60 - (now - last_sent).total_seconds())
                return jsonify({
                    "success": False,
                    "message": f"Paki-antay ng {wait_sec} segundo bago humingi ng bagong OTP code."
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
            return jsonify({"success": True, "message": f"Napadala na ang Password Reset OTP sa {email}."}), 200
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
            return jsonify({"success": False, "message": "Pakilagay ang email at 6-digit OTP code."}), 400

        stored = otp_memory_store.get(email)
        if not stored:
            return jsonify({"success": False, "message": "Walang aktibong OTP request para sa email na ito o expired na."}), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "Expired na ang OTP code (5 minutes limit). Humingi ng bagong code."}), 400

        stored["attempts"] = stored.get("attempts", 0) + 1
        if stored["attempts"] > 5:
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "Lumampas na sa pinapayagang attempts. Humingi ng bagong code."}), 429

        if stored['otp'] != otp_input:
            remaining = max(0, 5 - stored["attempts"])
            return jsonify({"success": False, "message": f"Maling OTP code. ({remaining} attempt(s) remaining)"}), 400

        # Mark as verified
        stored['verified'] = True
        return jsonify({"success": True, "message": "OTP verified successfully! Maaari mo nang ilagay ang bagong password."}), 200

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

        is_strong, pass_msg = validate_strong_password(new_password, email=email)
        if not is_strong:
            return jsonify({"success": False, "message": f"Mahina ang Bagong Password: {pass_msg}"}), 400

        if email == ADMIN_EMAIL:
            stored_admin = otp_memory_store.get(email)
            if stored_admin and (stored_admin.get('verified') or stored_admin['otp'] == otp_input):
                global ADMIN_PASSWORD
                ADMIN_PASSWORD = new_password
                otp_memory_store.pop(email, None)
                return jsonify({"success": True, "message": "Matagumpay na pinalitan ang Admin Password!"}), 200

        stored = otp_memory_store.get(email)
        if not stored:
            return jsonify({"success": False, "message": "Walang aktibong OTP request para sa email na ito o expired na."}), 400

        now = datetime.datetime.now()
        if now > stored.get("expires_at", now):
            otp_memory_store.pop(email, None)
            return jsonify({"success": False, "message": "Expired na ang OTP code. Humingi ng bagong code."}), 400

        if not stored.get('verified') and stored['otp'] != otp_input:
            stored["attempts"] = stored.get("attempts", 0) + 1
            if stored["attempts"] > 5:
                otp_memory_store.pop(email, None)
                return jsonify({"success": False, "message": "Lumampas na sa pinapayagang attempts."}), 429
            return jsonify({"success": False, "message": "Mali ang OTP code na ipinasok mo."}), 400

        # 1. Update REAL Firebase Authentication password
        fb_user = get_firebase_user_by_email(email)
        auth_updated = False
        if fb_user:
            auth_updated = update_firebase_password(fb_user.uid, new_password)
            if not auth_updated:
                return jsonify({"success": False, "message": "Hindi ma-update ang password sa Firebase Authentication."}), 500

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
            return jsonify({"success": True, "message": "Password updated successfully! Maaari ka nang mag-login gamit ang bagong password."}), 200
        else:
            return jsonify({"success": False, "message": "Hindi nahanap ang account record sa system."}), 400

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
            archive_item('user', user_id, user_data, 'admin_users')

            # 3. Remove from Firestore after archiving
            try:
                if db:
                    doc_ref = db.collection('customers').document(user_id)
                    if doc_ref.get().exists:
                        doc_ref.delete()
                    else:
                        customers_ref = db.collection('customers')
                        docs = customers_ref.where('id', '==', user_id).stream()
                        for d in docs:
                            d.reference.delete()
            except Exception as db_err:
                print(f"DB Archive-Delete Error: {db_err}")

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
            return jsonify({"success": False, "message": "Unauthorized! Admin lamang ang pwedeng magbago ng store inventory."}), 403

        try:
            data = request.json or {}
            products_list = data.get('products', [])
            success = save_store_products(products_list)
            return jsonify({"success": success, "message": "Store inventory updated successfully!"}), 200
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
            return jsonify({'success': False, 'message': 'Unauthorized! Admin lamang ang pwedeng magdagdag ng sample showcase.'}), 403
        try:
            data = request.json or {}
            saved = save_sample_product(data)
            if saved:
                return jsonify({'success': True, 'message': 'Sample product/service naidagdag nang matagumpay!', 'item': saved}), 200
            return jsonify({'success': False, 'message': 'Failed to save sample product.'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/samples/<sample_id>', methods=['DELETE', 'PUT'])
def handle_single_sample(sample_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin lamang ang pwedeng mag-manage ng samples.'}), 403
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

            archive_item('sample_product', sample_id, sample_data, 'admin_samples')
            delete_sample_product(sample_id)
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
            return jsonify({'success': False, 'message': 'Unauthorized! Admin lamang ang pwedeng magdagdag ng service.'}), 403
        try:
            data = request.json or {}
            saved = save_service(data)
            if saved:
                return jsonify({'success': True, 'message': 'Service naidagdag nang matagumpay!', 'item': saved}), 200
            return jsonify({'success': False, 'message': 'Failed to save service.'}), 500
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/services/<service_id>', methods=['DELETE', 'PUT'])
def handle_single_service(service_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin lamang ang pwedeng mag-manage ng services.'}), 403
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

            archive_item('service', service_id, service_data, 'admin_services')
            delete_service(service_id)
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
                return jsonify({'success': False, 'message': 'Kailangan ilagay ang Account Name at GCash Number.'}), 400

            # Clean number: strip dashes/spaces
            clean_num = re.sub(r'\D', '', acc_number)
            if not is_valid_ph_contact(clean_num):
                return jsonify({'success': False, 'message': 'Dapat ay 11-digit PH mobile number starting with 09.'}), 400

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
                return jsonify({"success": False, "message": "Kailangan mag-upload ng document o design file bago i-submit ang order."}), 400

            # Sanity-check: storagePath must start with expected prefix if provided
            if storage_path and not storage_path.startswith('orders/'):
                return jsonify({"success": False, "message": "Invalid file reference. Please re-upload the file."}), 400

            try:
                qty_val = int(data.get('quantity') or data.get('copies') or 1)
                pages_val = int(data.get('pages') or 1)
                total_amount = float(data.get('totalAmount') or data.get('total') or 0.0)

                if qty_val <= 0 or pages_val <= 0:
                    return jsonify({"success": False, "message": "Ang quantity at pages ay dapat mas mataas sa zero (0)."}), 400
                if qty_val > 100000:
                    return jsonify({"success": False, "message": "Ang pinakamataas na quantity ay 100,000 pcs lang."}), 400
                if pages_val > 5000:
                    return jsonify({"success": False, "message": "Ang pinakamataas na pages ay 5,000 lang."}), 400
                if total_amount <= 0 or total_amount > 5000000:
                    return jsonify({"success": False, "message": "Maling order total amount (maximum limit ₱5,000,000)."}), 400
            except (ValueError, TypeError):
                return jsonify({"success": False, "message": "May maling numeric values sa quantity, pages, o total."}), 400

            order_id = f"PRNT-{datetime.datetime.now().strftime('%M%S%f')[:6]}"
            cust_name = session.get('username') or data.get('customer') or 'Customer'
            if '@' in cust_name: cust_name = cust_name.split('@')[0]
            cust_email = session.get('email') or data.get('email') or f"{cust_name}@gmail.com"

            # Customer Note / Special Instructions (Optional, max 500 chars)
            customer_note = str(data.get('customerNote') if data.get('customerNote') is not None else (data.get('note') or '')).strip()
            if len(customer_note) > 500:
                return jsonify({"success": False, "message": "Ang order note / special instructions ay hindi pwedeng lumagpas sa 500 characters."}), 400

            # GCash Payment Fields Validation (if GCash Online is selected)
            payment_method = str(data.get('payment') or 'GCash Online').strip()
            gcash_ref = str(data.get('gcashRef') or data.get('referenceNumber') or '').strip()
            gcash_receipt_url = str(data.get('gcashReceiptUrl') or '').strip()

            if 'gcash' in payment_method.lower():
                clean_ref = re.sub(r'\D', '', gcash_ref)
                if len(clean_ref) < 8 or len(clean_ref) > 16:
                    return jsonify({
                        "success": False,
                        "message": "Pakilagay ang wastong GCash Reference Number (karaniwang 13 digits, hal. 1029384756123)."
                    }), 400
                gcash_ref = clean_ref

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
                "customerNote": customer_note,
                "payment": payment_method,
                "gcashRef": gcash_ref,
                "gcashReceiptUrl": gcash_receipt_url,
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
            return jsonify({"success": False, "message": "Kailangan maglagay ng malinaw na dahilan para sa pag-reject ng order (minimum 4 characters)."}), 400

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
                return jsonify({"success": False, "message": "Bawal magpadala ng blangkong chat message."}), 400
            if len(message_text) > 2000:
                return jsonify({"success": False, "message": "Masyadong mahaba ang mensahe (maximum 2,000 characters)."}), 400

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
                return jsonify({"success": False, "message": "Bawal magpadala ng blangkong chat message."}), 400

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
        earned_points = 20
        log_rfid_tap(card_uid, earned_points)
        return jsonify({"success": True, "earnedPoints": earned_points, "message": f"RFID Card {card_uid} successfully logged."}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



# ---------------------------------------------------------------------------
# IN-APP NOTIFICATIONS API
# ---------------------------------------------------------------------------

@app.route('/api/notifications', methods=['GET'])
@login_required
def get_user_notifications():
    try:
        user_id = session.get('uid') or session.get('email')
        role = session.get('role', 'customer')
        notifs = get_notifications(user_id if role != 'admin' else 'ADMIN') or []
        # Sort newest first
        notifs.sort(key=lambda x: str(x.get('timestamp') or x.get('createdAt') or ''), reverse=True)
        return jsonify({'success': True, 'notifications': notifs}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def read_all_notifications():
    try:
        user_id = session.get('uid') or session.get('email')
        role = session.get('role', 'customer')
        mark_all_notifications_read(user_id if role != 'admin' else 'ADMIN')
        return jsonify({'success': True}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notifications/clear', methods=['POST'])
@login_required
def clear_all_notifications():
    try:
        user_id = session.get('uid') or session.get('email')
        role = session.get('role', 'customer')
        delete_all_notifications(user_id if role != 'admin' else 'ADMIN')
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

        if target_prod:
            archive_item('store_product', product_id, target_prod, 'admin_store')

        # Delete document from active store collections so it does not reappear on refresh
        delete_store_product(product_id)
        return jsonify({'success': True, 'message': 'In-store product moved to Archive successfully.'}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 Kez C-Em Zek Backend Running on http://127.0.0.1:5000")
    print("=" * 60)
    app.run(debug=True, port=5000)
