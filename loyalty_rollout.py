"""Back up testing balances, reset them, then enable the new loyalty epoch."""
import argparse
import datetime
import json
import os
import uuid
from pathlib import Path


def rollout(db, auth, backup_path, apply=False):
    path = Path(backup_path).resolve()
    ref = db.collection('settings').document('loyalty_config')
    current = ref.get().to_dict() or {}
    if current.get('enabled') and apply:
        raise ValueError('Loyalty is already enabled. Refusing to reset active balances again.')
    if path.exists():
        report = json.loads(path.read_text(encoding='utf-8'))
        if current.get('epoch') and current['epoch'] != report['epoch']:
            raise ValueError('The backup belongs to another rollout.')
    else:
        users = list(auth.list_users().iterate_all())
        by_email = {str(user.email or '').lower(): user.uid for user in users if user.email}
        by_uid = {user.uid: user for user in users}
        records, unmatched = [], []
        for collection in ['users', 'customers']:
            for doc in db.collection(collection).stream():
                data = doc.to_dict() or {}
                email = str(data.get('email') or '').strip().lower()
                uid = by_email.get(email) or (doc.id if doc.id in by_uid else None)
                if (data.get('uid') and data['uid'] != uid) or (doc.id in by_uid and uid != doc.id):
                    uid = None
                if not uid:
                    unmatched.append(f'{collection}/{doc.id}')
                records.append({'path': f'{collection}/{doc.id}', 'email': email, 'uid': uid,
                                'points': data.get('points'), 'loyaltyPoints': data.get('loyaltyPoints')})
        report = {'epoch': uuid.uuid4().hex, 'createdAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'profiles': records, 'authUids': list(by_uid), 'unmatched': unmatched,
                  'accounts': [{'uid': d.id, 'data': d.to_dict()} for d in db.collection('loyalty_accounts').stream()]}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as output:
            json.dump(report, output, indent=2, default=str)
            output.flush()
            os.fsync(output.fileno())
    if not apply:
        return {'backup': str(path), 'profiles': len(report['profiles']), 'unmatched': len(report['unmatched']), 'applied': False}
    # Disabling the feature first makes a partial run safe to resume from the same backup.
    ref.set({'enabled': False, 'epoch': report['epoch'], 'pointValueCents': 100}, merge=True)
    writes = []
    for profile in report['profiles']:
        value = {'points': 0, 'loyaltyPoints': 0}
        if profile['uid']:
            value['loyaltyUid'] = profile['uid']
        writes.append((db.document(profile['path']), value))
    for uid in set(report['authUids']) | {a['uid'] for a in report['accounts']}:
        account = db.collection('loyalty_accounts').document(uid)
        writes.append((account, {'balance': 0, 'reserved': 0, 'epoch': report['epoch']}))
        writes.append((account.collection('events').document('reset_' + report['epoch']), {
            'type': 'reset', 'points': 0, 'orderId': '', 'balance': 0, 'reserved': 0,
            'createdAt': datetime.datetime.fromisoformat(report['createdAt']), 'epoch': report['epoch'],
        }))
    for start in range(0, len(writes), 400):
        batch = db.batch()
        for target, data in writes[start:start+400]:
            batch.set(target, data, merge=True)
        batch.commit()
    ref.set({'enabled': True, 'epoch': report['epoch'], 'pointValueCents': 100,
             'startedAt': datetime.datetime.fromisoformat(report['createdAt'])}, merge=True)
    return {'backup': str(path), 'profiles': len(report['profiles']), 'unmatched': len(report['unmatched']), 'applied': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from firebase_admin import auth
    from firebase_config import db
    if db is None:
        raise SystemExit('Firebase is unavailable. No balances were changed.')
    print(json.dumps(rollout(db, auth, args.backup, args.apply), indent=2))
