import firebase_admin
from firebase_admin import credentials, firestore, auth, messaging
import datetime
import uuid
import re

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
        # Validate status transition according to workflow
        current_status = order_data.get('status')
        allowed_flow = {
            "Order Submitted": ["Approved"],
            "Approved": ["Printing", "Preparing"],
            "Printing": ["Ready for Pickup"],
            "Preparing": ["Ready for Pickup"],
            "Ready for Pickup": ["Completed"]
        }
        if new_status == "Rejected":
            valid = True
        else:
            valid = new_status in allowed_flow.get(current_status, [])
        if not valid:
            print(f"❌ Invalid status transition from {current_status} to {new_status}.")
            return False
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
        conv_ref.collection('messages').document(msg_id).set(msg_data)

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

        conv_ref.set(conv_update, merge=True)

        # Trigger background Push Notification to counter-party
        try:
            conv_data = conv_doc.to_dict() if conv_doc and conv_doc.exists else {}
            customer_email = conv_data.get('customerEmail') or (sender_email if sender_role == 'customer' else '')
            customer_name = conv_data.get('customerName') or sender_name or 'Customer'

            snippet = message_text.strip()
            if len(snippet) > 85:
                snippet = snippet[:82] + "..."

            if sender_role == 'customer':
                # Customer sent a message -> notify Admin / Staff
                notif_title = f"New message from {customer_name}"
                notif_url = f"/admin?tab=liveChat&convId={conv_id}"
                send_fcm_push_notification(
                    recipient_role='admin',
                    recipient_email=None,
                    title=notif_title,
                    body=snippet,
                    click_url=notif_url,
                    data_payload={
                        'conversation_id': conv_id,
                        'sender_role': 'customer',
                        'sender_email': sender_email,
                        'customer_name': customer_name
                    }
                )
            elif sender_role in ['admin', 'staff']:
                # Admin/Staff replied -> notify the Customer
                notif_title = "New reply from Kez C-Em Zek"
                notif_url = "/customer?tab=chat"
                send_fcm_push_notification(
                    recipient_role='customer',
                    recipient_email=customer_email,
                    title=notif_title,
                    body=snippet,
                    click_url=notif_url,
                    data_payload={
                        'conversation_id': conv_id,
                        'sender_role': 'admin',
                        'sender_email': sender_email
                    }
                )
        except Exception as notif_err:
            print(f"Non-fatal push notification trigger error: {notif_err}")

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
            if tok and tok not in active_tokens:
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
        if data_payload and isinstance(data_payload, dict):
            for k, v in data_payload.items():
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
            fcm_options=messaging.WebpushFCMOptions(
                link=click_url or '/'
            )
        )

        sent_count = 0
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
                    token=token
                )
                messaging.send(msg)
                sent_count += 1
            except messaging.UnregisteredError:
                invalid_tokens.append(token)
            except Exception as token_err:
                err_str = str(token_err).lower()
                if 'unregistered' in err_str or 'invalid' in err_str or 'not found' in err_str:
                    invalid_tokens.append(token)
                else:
                    print(f"Push send warning for token: {token_err}")

        # Clean up expired or unregistered tokens
        for inv_tok in invalid_tokens:
            try:
                ref = doc_refs_by_token.get(inv_tok)
                if ref:
                    ref.delete()
            except Exception:
                pass

        return {
            "success": True,
            "sent_count": sent_count,
            "invalid_cleaned": len(invalid_tokens)
        }
    except Exception as e:
        print(f"Error in send_fcm_push_notification: {e}")
        return {"success": False, "sent_count": 0, "error": str(e)}
