"""Local-only layout fixture. Uses synthetic records and never loads production services."""
from pathlib import Path
from flask import Flask, jsonify, render_template, request, session

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
app.secret_key = 'isolated-layout-fixture-only'
ORDER = dict(id='PRNT-123456-LONG-REFERENCE', customer='Sample customer with a very long display name', customerId='fixture-uid', service='Document Printing', paperSize='A4', copies=1, total=25, status='Order Submitted', payment='Cash on Pickup', createdAt='2026-11-14T08:00:00Z', customerNote='Please keep every page in its original order.', filename='A_very_long_sample_document_filename_for_responsive_testing.pdf')
PRODUCT = dict(id='fixture-product', name='Sample notebook with a long product name', price=25, stock=20, category='Notebooks', description='Synthetic product for layout verification', imageUrl='')
USER = dict(id='fixture-uid', uid='fixture-uid', name=ORDER['customer'], username=ORDER['customer'], email='long.customer.address@example.com', phone='09123456789', role='customer', points=10, rfidUid='AABBCCDDEEFF001122334455', createdAt=ORDER['createdAt'])
SERVICE = dict(id='fixture-service', name='Document Printing', basePrice=5, price=5, unit='page', active=True, category='Printing')

@app.after_request
def successful_fixture_responses(response):
    if request.path.startswith('/api/') and response.is_json:
        data = response.get_json()
        if isinstance(data, dict):
            data.setdefault('success', True)
            response.set_data(app.json.dumps(data))
    return response

@app.get('/')
@app.get('/<page>')
def page(page='index'):
    if page not in ['index', 'admin', 'customer', 'store', 'order', 'login']:
        return '', 404
    session.clear()
    session['fixtureNewCustomer'] = request.args.get('new') == '1'
    if page != 'login':
        session.update(username='Layout Test', email='fixture@example.com', uid='fixture-uid', role='admin' if page == 'admin' else 'customer')
    html = render_template(page + '.html')
    if request.args.get('large') == '1':
        html = html.replace('</head>', '<style>html{font-size:24px!important}</style></head>')
    return html

@app.get('/api/<path:path>')
def api(path):
    if path == 'orders': return jsonify([dict(ORDER, id=f'PRNT-{123456+i}', status=['Order Submitted', 'Approved', 'Printing', 'Ready for Pickup'][i % 4]) for i in range(14)])
    if path == 'store/products': return jsonify([PRODUCT, dict(PRODUCT, id='fixture-2', stock=0)])
    if path == 'services': return jsonify([SERVICE])
    if path == 'users': return jsonify([USER])
    if path == 'samples': return jsonify([])
    if path == 'archive': return jsonify(archives=[dict(archiveDocId='fixture-archive', originalId=PRODUCT['id'], itemType='product', data=PRODUCT, archivedAt=ORDER['createdAt'])])
    if path == 'notifications': return jsonify([dict(id='fixture-notification', title='Order ready for pickup', message='Your sample order is ready.', read=False)])
    if path.endswith('loyalty/rewards'): return jsonify(rewards=[dict(id='fixture-reward', name='Sample notebook reward', description='Synthetic reward', quantity=1, pointsCost=5, active=True, kind='product')])
    if path.endswith('loyalty/settings'): return jsonify(settings=dict(pointValueCents=100))
    if path.endswith('loyalty/catalog'): return jsonify(products=[PRODUCT], services=[SERVICE])
    if path == 'loyalty/me': return jsonify(balance=10, available=8, reserved=2, pointValueCents=100, events=[], cashOnPickupAllowed=not session.get('fixtureNewCustomer'), hasOrders=not session.get('fixtureNewCustomer'))
    if path == 'settings/gcash': return jsonify(accountName='Sample Merchant', accountNumber='09123456789', enabled=True)
    if path == 'chat/conversations': return jsonify(conversations=[dict(id='fixture-conversation', customerId='fixture-uid', customerName=USER['name'], customerEmail=USER['email'], lastMessage='Sample message', unreadCount=1)])
    if path.endswith('/messages'): return jsonify(messages=[])
    if path == 'pos/sales': return jsonify(sales=[])
    return jsonify({})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=False)
