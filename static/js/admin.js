function escapeAdminText(value) {
    return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

// Staff Admin Dashboard & Orders Table Rendering Logic
function fetchAdminOrders() {
    fetch('/api/orders')
    .then(res => res.json())
    .then(data => {
        const orders = Array.isArray(data) ? data : (data.orders || []);
        const tbody = document.getElementById('adminOrderTableBody');
        if (!tbody) return;

        tbody.innerHTML = orders.map(o => {
            const serviceName = o.service || o.serviceType || '';
            const totalAmount = parseFloat(o.total || o.totalAmount || 0);
            const currentStatus = o.status || 'Order Submitted';
            const orderId = o.id || '';

            // Tingnan kung Store Supply / In-Store Item (pen, notebooks, folders, etc.)
            const isStoreItem = serviceName.toLowerCase().includes('store') || 
                                serviceName.toLowerCase().includes('supply') || 
                                serviceName.toLowerCase().includes('in-store');

            // Dynamic Main Action Button – enforce strict workflow
            let nextStatus = null;
            let mainActionText = '';
            let mainActionBg = '';

            const transitionMap = {
                'Order Submitted': 'Approved',
                'Approved': isStoreItem ? 'Preparing' : 'Printing',
                'Printing': 'Ready for Pickup',
                'Preparing': 'Ready for Pickup',
                'Ready for Pickup': 'Completed'
            };

            if (currentStatus in transitionMap) {
                nextStatus = transitionMap[currentStatus];
                switch (nextStatus) {
                    case 'Approved':
                        mainActionText = 'Approve Order';
                        mainActionBg = 'bg-emerald-600 hover:bg-emerald-700';
                        break;
                    case 'Printing':
                        mainActionText = '🖨️ Start Processing';
                        mainActionBg = 'bg-blue-600 hover:bg-blue-700';
                        break;
                    case 'Preparing':
                        mainActionText = '📦 Prepare Item';
                        mainActionBg = 'bg-indigo-600 hover:bg-indigo-700';
                        break;
                    case 'Ready for Pickup':
                        mainActionText = '🚚 Ready for Pickup';
                        mainActionBg = 'bg-orange-500 hover:bg-orange-600';
                        break;
                    case 'Completed':
                        mainActionText = '✓ Complete Order';
                        mainActionBg = 'bg-emerald-600 hover:bg-emerald-700';
                        break;
                }
            } else {
                // No forward transition (e.g., already Completed)
                mainActionText = currentStatus === 'Completed' ? '✓ Completed' : 'No Action';
                mainActionBg = 'bg-slate-700';
                nextStatus = currentStatus;
            }

            const customerNote = (o.customerNote || o.note || '').trim();

            return `
                <tr class="border-b border-slate-100 hover:bg-slate-50/50">
                    <td class="p-3 font-mono font-bold text-xs">${escapeAdminText(orderId.substring(0, 8))}</td>
                    <td class="p-3 text-xs font-semibold">${escapeAdminText(o.customer || o.userId || 'Customer')}</td>
                    <td class="p-3 text-xs">
                        <div>${escapeAdminText(serviceName)}</div>
                        ${customerNote ? `
                            <div class="mt-0.5 inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 text-[10px]" title="Note: ${escapeAdminText(customerNote)}">
                                <span>📝</span> <span class="max-w-[140px] truncate">${escapeAdminText(customerNote)}</span>
                            </div>
                        ` : ''}
                    </td>
                    <td class="p-3 font-mono text-xs font-bold">₱${totalAmount.toFixed(2)}</td>
                    <td class="p-3">
                        <span class="bg-slate-100 text-slate-700 px-2 py-0.5 rounded text-[11px] font-bold">${escapeAdminText(currentStatus)}</span>
                    </td>
                    <td class="p-3 text-right">
                        <div class="relative inline-block text-left">
                            <div class="inline-flex rounded-xl shadow-sm">
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, ${escapeAdminText(JSON.stringify(String(nextStatus)))})" class="${mainActionBg} text-white px-3 py-1.5 rounded-l-xl text-xs font-bold transition-all">
                                    ${mainActionText}
                                </button>
                                <button onclick="toggleDropdown(event, ${escapeAdminText(JSON.stringify(String(orderId)))})" class="${mainActionBg} text-white px-2 py-1.5 rounded-r-xl border-l border-white/20 text-xs font-bold">
                                    ▼
                                </button>
                            </div>

                            <!-- MALINIS AT WALANG OVERLAPPING NA DROPDOWN MENU -->
                            <div id="drop-${escapeAdminText(orderId)}" class="hidden absolute right-0 mt-1 w-48 bg-white rounded-xl shadow-xl border border-slate-100 z-50 py-1.5 text-left overflow-hidden">
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Approved')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-emerald-50 hover:text-emerald-700 flex items-center gap-2">
                                    ✓ Approve Order
                                </button>
                                ${isStoreItem ? `
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Preparing')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-indigo-50 hover:text-indigo-700 flex items-center gap-2">
                                    📦 Mark as Processing
                                </button>
                                ` : `
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Printing')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-blue-50 hover:text-blue-700 flex items-center gap-2">
                                    🖨️ Mark as Processing
                                </button>
                                `}
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Ready for Pickup')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-orange-50 hover:text-orange-700 flex items-center gap-2">
                                    🚚 Ready for Pickup
                                </button>
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Completed')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-100 flex items-center gap-2">
                                    ✓ Complete Order
                                </button>
                                <div class="border-t border-slate-100 my-1"></div>
                                <button onclick="updateStatus(${escapeAdminText(JSON.stringify(String(orderId)))}, 'Rejected')" class="w-full text-left px-3 py-1.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 flex items-center gap-2">
                                    ✕ Reject Order
                                </button>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
    });
}

function toggleDropdown(event, id) {
    event.stopPropagation();
    document.querySelectorAll('[id^="drop-"]').forEach(el => {
        if (el.id !== `drop-${id}`) el.classList.add('hidden');
    });
    const target = document.getElementById(`drop-${id}`);
    if (target) target.classList.toggle('hidden');
}

// Isara ang dropdown kapag nag-click kahit saan sa labas
document.addEventListener('click', function(e) {
    if (!e.target.closest('.relative')) {
        document.querySelectorAll('[id^="drop-"]').forEach(el => el.classList.add('hidden'));
    }
});

function updateStatus(orderId, status) {
    fetch('/api/orders/update-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ orderId: orderId, status: status })
    })
    .then(res => res.json())
    .then(data => {
        if (typeof showToast === 'function') {
            showToast(`Order #${escapeAdminText(orderId)} status updated to ${status}`);
        }
        fetchAdminOrders();
    })
    .catch(err => console.error("Update error:", err));
}