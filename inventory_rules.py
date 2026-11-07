"""Shared inventory rules for order status updates (no database side effects)."""
import re


def order_items(order):
    items = order.get('items') or []
    if items:
        return [dict(item, id=item.get('productId') or item.get('id'),
                     qty=int(item.get('qty') or item.get('quantity') or 1)) for item in items]
    service = str(order.get('service') or '')
    if not service.lower().startswith('in-store:'):
        return []
    return [{'name': name.strip(), 'qty': int(qty)}
            for name, qty in re.findall(r'([^,]+?)\s*\((\d+)x\)', service.split(':', 1)[1])]


def stock_plan(order, products, restore=False):
    """Resolve IDs first, exact legacy names second; aggregate repeated lines."""
    quantities = {}
    for item in order_items(order):
        item_id = str(item.get('id') or '')
        name = str(item.get('name') or '').strip().lower()
        matches = [(key, data) for key, data in products.items()
                   if (item_id and key == item_id) or
                   (not item_id and str(data.get('name') or '').strip().lower() == name)]
        if len(matches) != 1:
            raise ValueError(f"Product not found or ambiguous: {item.get('name') or item_id}")
        key, product = matches[0]
        if product.get('archived') is True and not restore:
            raise ValueError(f"Product is archived: {name}")
        qty = int(item['qty'])
        if qty <= 0:
            raise ValueError('Item quantity must be positive.')
        quantities[key] = quantities.get(key, 0) + qty
    plan = {}
    for key, qty in quantities.items():
        current = int(products[key].get('stock') or 0)
        if not restore and current < qty:
            raise ValueError(f"Insufficient stock: {products[key].get('name', key)}")
        plan[key] = current + qty if restore else current - qty
    return plan


def valid_transition(current, target):
    current = str(current or 'Order Submitted')
    if current == 'Cancelled':
        return False
    if target.startswith('Rejected'):
        return current not in ('Completed',) and not current.startswith('Rejected')
    return target in {
        'Order Submitted': ['Approved'], 'For Approval': ['Approved'],
        'Approved': ['Printing', 'Preparing'], 'Printing': ['Ready for Pickup'],
        'Preparing': ['Ready for Pickup'], 'Ready for Pickup': ['Completed'],
    }.get(current, [])
