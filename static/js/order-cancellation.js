function customerCanCancel(order) {
    const status = Object.hasOwn(order, 'status') ? order.status : 'Order Submitted';
    return order.source !== 'POS' && ['Order Submitted', 'For Approval'].includes(status) &&
        order.approvedAt == null && (order.stockDeducted === undefined || order.stockDeducted === false);
}

let cancellationOrderId = null;
let cancellationBusy = false;

function openCustomerCancellation(id) {
    const order = allCustomerOrdersCache.find(o => o.id === id);
    if (!order || !customerCanCancel(order)) {
        alert('This order cannot be cancelled because it has been approved or processing has started.');
        return;
    }
    cancellationOrderId = id;
    document.getElementById('cancellationReference').textContent = id;
    document.getElementById('cancellationReason').value = '';
    document.getElementById('cancellationError').textContent = '';
    document.getElementById('customerCancellationModal').classList.remove('hidden');
}

function closeCustomerCancellation() {
    if (cancellationBusy) return;
    document.getElementById('customerCancellationModal').classList.add('hidden');
    cancellationOrderId = null;
}

async function confirmCustomerCancellation() {
    if (cancellationBusy || !cancellationOrderId) return;
    const reason = document.getElementById('cancellationReason').value.trim();
    const error = document.getElementById('cancellationError');
    if ([...reason].length > 500) { error.textContent = 'Reason must be at most 500 characters.'; return; }
    cancellationBusy = true;
    const button = document.getElementById('cancellationConfirm');
    button.disabled = true;
    error.textContent = '';
    try {
        const response = await fetch(`/api/orders/${encodeURIComponent(cancellationOrderId)}/cancel`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({reason})
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw Error(data.message || 'Unable to confirm cancellation. Reconnect and retry.');
        allCustomerOrdersCache = allCustomerOrdersCache.map(order => order.id === cancellationOrderId ? {...order, ...data.order} : order);
        cancellationBusy = false;
        closeCustomerCancellation();
        filterCustomerOrders('cancelled');
    } catch (failure) { error.textContent = failure.message; }
    finally { cancellationBusy = false; button.disabled = false; }
}
