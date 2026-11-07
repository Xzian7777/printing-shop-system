"""Durable FCM delivery for web and mobile actions, using the running backend."""
import datetime
import logging
import threading
import uuid

from firebase_admin import auth, firestore

log = logging.getLogger(__name__)
_lock = threading.Lock()
_thread = None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def customer_email(data):
    email = data.get('customerEmail') or data.get('email')
    if email and '@' in str(email):
        return str(email).strip().lower()
    owner = data.get('customerId') or data.get('uid')
    if owner and '@' in str(owner):
        return str(owner).strip().lower()
    if owner:
        try:
            return auth.get_user(str(owner)).email
        except auth.UserNotFoundError:
            return None
    return None


def job_payload(db, job, event_id):
    """Read the saved source, never a client-supplied recipient or message body."""
    if job.get('type') == 'order_status':
        order_id = job.get('orderId')
        status = str(job.get('status') or '')
        allowed = ['Approved', 'Printing', 'Preparing', 'Ready for Pickup', 'Completed']
        if not order_id or (status not in allowed and not status.startswith('Rejected')):
            return None
        order = db.document(f'artifacts/printcraft-pro/public/data/orders/{order_id}').get()
        if not order.exists:
            return None
        data = order.to_dict()
        email = customer_email(data)
        if not email:
            return None
        return dict(
            recipient_role='customer', recipient_email=email,
            title=f'Order {status}: {order_id}'[:200],
            body=f"Your {data.get('service') or 'order'} is now {status}."[:300],
            click_url='/customer?tab=myOrders',
            data_payload={'type': 'order_status', 'order_id': order_id,
                          'status': status, 'event_id': event_id},
        )
    if job.get('type') == 'chat':
        conv_id, msg_id = job.get('conversationId'), job.get('messageId')
        if not conv_id or not msg_id:
            return None
        ref = db.collection('conversations').document(conv_id)
        conv, message = ref.get(), ref.collection('messages').document(msg_id).get()
        if not conv.exists or not message.exists:
            return None
        data, msg = conv.to_dict(), message.to_dict()
        sender = str(msg.get('senderRole') or msg.get('sender') or '').lower()
        if sender not in ['admin', 'staff', 'customer']:
            return None
        to_customer = sender in ['admin', 'staff']
        email = customer_email(data) if to_customer else None
        if to_customer and not email:
            return None
        return dict(
            recipient_role='customer' if to_customer else 'admin', recipient_email=email,
            title='New message from KCZ Printing' if to_customer else 'New customer message',
            body=str(msg.get('message') or msg.get('text') or 'New message')[:300],
            click_url='/customer?tab=chat' if to_customer else f'/admin?tab=liveChat&convId={conv_id}',
            data_payload={'type': 'chat', 'conversation_id': conv_id, 'event_id': event_id},
        )
    return None


@firestore.transactional
def claim_job(transaction, ref, now, lease):
    snap = ref.get(transaction=transaction)
    data = snap.to_dict() or {}
    if data.get('state') not in ['pending', 'retry', 'sending']:
        return None
    if data.get('availableAt', now) > now or data.get('leaseUntil', now) > now:
        return None
    created = data.get('createdAt')
    if isinstance(created, datetime.datetime) and now - created > datetime.timedelta(days=1):
        transaction.update(ref, {'state': 'expired', 'finishedAt': now})
        return None
    transaction.update(ref, {
        'state': 'sending', 'lease': lease,
        'leaseUntil': now + datetime.timedelta(minutes=5),
        'attempts': int(data.get('attempts', 0)) + 1,
    })
    return data


@firestore.transactional
def finish_job(transaction, ref, lease, updates):
    snap = ref.get(transaction=transaction)
    if snap.exists and snap.to_dict().get('lease') == lease:
        transaction.update(ref, {**updates, 'leaseUntil': utc_now()})


def deliver_job(db, ref, send):
    lease = uuid.uuid4().hex
    job = claim_job(db.transaction(), ref, utc_now(), lease)
    if job is None:
        return
    delivered = list(job.get('deliveredTokens', []))
    try:
        payload = job_payload(db, job, ref.id)
        if payload is None:
            finish_job(db.transaction(), ref, lease, {'state': 'unresolved', 'finishedAt': utc_now()})
            return
        payload['data_payload']['_exclude_tokens'] = delivered
        result = send(**payload)
        delivered = list(set(delivered + result.get('delivered_tokens', [])))
        if result.get('success'):
            finish_job(db.transaction(), ref, lease, {
                'state': 'sent', 'finishedAt': utc_now(), 'deliveredTokens': delivered,
            })
            return
    except Exception:
        log.exception('Push job %s failed', ref.id)
    delay = min(1800, 15 * 2 ** min(int(job.get('attempts', 0)), 7))
    finish_job(db.transaction(), ref, lease, {
        'state': 'retry', 'availableAt': utc_now() + datetime.timedelta(seconds=delay),
        'deliveredTokens': delivered,
    })


def run(db, send):
    while True:
        try:
            # One field index, with persistent leases for concurrent web workers.
            jobs = db.collection('push_jobs').where('state', 'in', ['pending', 'retry', 'sending'])
            for snapshot in jobs.stream():
                deliver_job(db, snapshot.reference, send)
        except Exception:
            log.exception('Push delivery poll failed')
        threading.Event().wait(5)


def start_push_worker(db, send):
    global _thread
    if db is None:
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=run, args=(db, send), name='fcm-delivery', daemon=True)
            _thread.start()


if __name__ == '__main__':
    from firebase_config import db, send_fcm_push_notification
    if db is None:
        raise SystemExit('Firebase credentials are unavailable.')
    run(db, send_fcm_push_notification)
