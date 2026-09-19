// Main Client Engine & Order Calculation

function switchCustomerTab(tabId) {
    // Hide all section contents
    document.querySelectorAll('.cust-section-content').forEach(el => el.classList.add('hidden'));

    // Reset all tab button styles
    document.querySelectorAll('.cust-tab-btn').forEach(btn => {
        btn.className = "cust-tab-btn px-5 py-3 rounded-xl text-slate-600 hover:bg-slate-100 transition-all flex items-center gap-2 cursor-pointer";
    });

    // Show active section content
    const activeSection = document.getElementById(`custSection-${tabId}`);
    const activeBtn = document.getElementById(`tabBtn-${tabId}`);

    if (activeSection) activeSection.classList.remove('hidden');
    if (activeBtn) {
        activeBtn.className = "cust-tab-btn px-5 py-3 rounded-xl bg-blue-600 text-white shadow-xs transition-all flex items-center gap-2 cursor-pointer";
    }

    if (tabId === 'myOrders' && typeof fetchCustomerOrders === 'function') {
        fetchCustomerOrders();
    }
    if (tabId === 'chat' && typeof fetchCustomerChatMessages === 'function') {
        fetchCustomerChatMessages();
    }
}

function recalculateTotal() {
    const sizeSelect = document.getElementById('custSizeSelect');
    if (!sizeSelect) return;
    const basePrice = parseFloat(sizeSelect.options[sizeSelect.selectedIndex].getAttribute('data-price')) || 3.0;

    const stockSelect = document.getElementById('custStockSelect');
    const stockMult = parseFloat(stockSelect.options[stockSelect.selectedIndex].getAttribute('data-mult')) || 1.0;

    const pages = Math.max(1, parseInt(document.getElementById('custPageCount')?.value) || 1);
    const qty = Math.max(1, parseInt(document.getElementById('custQtyCount')?.value) || 1);

    const total = basePrice * stockMult * pages * qty;
    const totalElem = document.getElementById('custCalculatedTotal');
    if (totalElem) totalElem.textContent = `₱${total.toFixed(2)}`;
}

function handleFileSelected(e) {
    const file = e.target.files[0];
    if (file) {
        const display = document.getElementById('orderFileNameDisplay');
        if (display) display.textContent = `📄 Selected: ${file.name}`;
    }
}

function showToast(msg) {
    const toast = document.getElementById('toastNotification');
    const msgElem = document.getElementById('toastMessage');
    if (toast && msgElem) {
        msgElem.textContent = msg;
        toast.classList.remove('hidden');
        setTimeout(() => toast.classList.add('hidden'), 3500);
    }
}

window.onload = function() {
    recalculateTotal();
};