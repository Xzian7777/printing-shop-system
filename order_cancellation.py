from firebase_admin import firestore


def can_cancel_order(order):
    return (order.get('source') != 'POS' and
            order.get('status', 'Order Submitted') in ['Order Submitted', 'For Approval'] and
            order.get('approvedAt') is None and order.get('stockDeducted', False) is False)


def cancel_customer_order(db, order_id, uid, email, reason=''):
    if not isinstance(reason, str) or len(reason.strip()) > 500:
        raise TypeError('Cancellation reason must be at most 500 characters.')
    if not order_id or '/' in order_id:
        raise FileNotFoundError('Order not found.')
    ref = db.collection('artifacts').document('printcraft-pro').collection('public').document('data').collection('orders').document(order_id)
    clean_email = str(email or '').strip().lower()

    @firestore.transactional
    def apply(transaction):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise FileNotFoundError('Order not found.')
        order = snapshot.to_dict()
        owner_email = str(order.get('email') or order.get('customerEmail') or '').strip().lower()
        owner_uid = order.get('customerId') or order.get('uid')
        if not ((uid and owner_uid == uid) or (clean_email and owner_email == clean_email)):
            raise PermissionError('You can only cancel your own order.')
        if order.get('status') == 'Cancelled':
            return {'id': order_id, 'status': 'Cancelled', 'cancellationReason': order.get('cancellationReason', '')}
        if not can_cancel_order(order):
            raise ValueError('Hindi na puwedeng i-cancel dahil approved na o nagsimula na ang order.')
        transaction.update(ref, {
            'status': 'Cancelled', 'cancelledAt': firestore.SERVER_TIMESTAMP,
            'cancelledBy': uid or clean_email, 'cancelledByEmail': clean_email,
            'cancellationReason': reason.strip(),
        })
        return {'id': order_id, 'status': 'Cancelled', 'cancellationReason': reason.strip()}

    return apply(db.transaction())
