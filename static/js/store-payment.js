let storeGcashSettings = null;
let storeGcashRequest = null;
let storeGcashReceipt = '';
let storeReceiptPending = false;
let storeCheckoutPending = false;

function storeUsesGcash() {
    return document.querySelector('input[name="storePaymentMethod"]:checked')?.value === 'GCash';
}

function updateStorePaymentControls() {
    const panel = document.getElementById('storeGcashPanel');
    if (!panel) return;
    const gcash = storeUsesGcash();
    panel.classList.toggle('hidden', !gcash);
    const ready = storeGcashSettings && storeGcashSettings.isActive !== false && storeGcashSettings.accountNumber;
    const state = document.getElementById('storeGcashStatus');
    state.textContent = !storeGcashSettings ? 'Loading GCash details…' : ready ? '' : 'GCash is unavailable. Choose Cash Pickup.';
    document.getElementById('storeGcashAccountName').textContent = ready ? storeGcashSettings.accountName || 'GCash' : '';
    document.getElementById('storeGcashAccountNumber').textContent = ready ? storeGcashSettings.accountNumber : '';
    const qr = document.getElementById('storeGcashQr');
    const source = ready ? (storeGcashSettings.qrImageSource === storeGcashSettings.qrUrl && storeGcashSettings.qrImageData ? storeGcashSettings.qrImageData : storeGcashSettings.qrUrl) : '';
    qr.hidden = !source;
    if (source) qr.src = source;
    const button = document.getElementById('storeCheckoutButton');
    const invalidPrices = Object.values(cartState).some(item => !validStorePrice(item.price));
    const priceError = 'A product price needs to be updated by the shop.';
    const error = document.getElementById('storePaymentError');
    if (invalidPrices) error.textContent = priceError;
    else if (error.textContent === priceError) error.textContent = '';
    button.disabled = invalidPrices || storeCheckoutPending || storeReceiptPending || Object.keys(cartState).length === 0 || (gcash && !ready);
    button.textContent = storeCheckoutPending ? 'Submitting order…' : 'Confirm & Place Store Order';
}

async function loadStoreGcashSettings() {
    if (storeGcashRequest) return storeGcashRequest;
    storeGcashRequest = (async () => {
        try {
            const response = await fetch('/api/settings/gcash');
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error('Unable to load GCash details.');
            storeGcashSettings = result.settings;
        } catch (error) {
            storeGcashSettings = {isActive: false};
            document.getElementById('storePaymentError').textContent = error.message;
        } finally {
            storeGcashRequest = null;
            updateStorePaymentControls();
        }
    })();
    return storeGcashRequest;
}

async function selectStoreReceipt(input) {
    storeGcashReceipt = '';
    const preview = document.getElementById('storeReceiptPreview');
    preview.hidden = true;
    const status = document.getElementById('storeReceiptStatus');
    const file = input.files?.[0];
    if (!file) { status.textContent = ''; return; }
    storeReceiptPending = true;
    updateStorePaymentControls();
    try {
        if (!['image/png', 'image/jpeg'].includes(file.type) || !/\.(png|jpe?g)$/i.test(file.name)) throw new Error('Choose a PNG or JPG receipt.');
        if (!file.size || file.size > 10 * 1024 * 1024) throw new Error('Receipt must be between 1 byte and 10 MB.');
        const image = await createImageBitmap(file);
        const scale = Math.min(1, 1400 / Math.max(image.width, image.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(image.width * scale));
        canvas.height = Math.max(1, Math.round(image.height * scale));
        const context = canvas.getContext('2d');
        context.fillStyle = '#fff';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        image.close();
        let receipt = canvas.toDataURL('image/jpeg', 0.82);
        if (receipt.length > 650000) receipt = canvas.toDataURL('image/jpeg', 0.55);
        if (receipt.length > 650000) throw new Error('Receipt is too large. Choose a smaller image.');
        storeGcashReceipt = receipt;
        preview.src = receipt;
        preview.hidden = false;
        status.textContent = file.name;
    } catch (error) {
        input.value = '';
        status.textContent = error.message;
    } finally {
        storeReceiptPending = false;
        updateStorePaymentControls();
    }
}

function clearStoreReceipt() {
    storeGcashReceipt = '';
    document.getElementById('storeReceiptFile').value = '';
    document.getElementById('storeReceiptPreview').hidden = true;
    document.getElementById('storeReceiptStatus').textContent = '';
    updateStorePaymentControls();
}

async function submitStoreCheckout() {
    if (storeCheckoutPending || storeReceiptPending) return;
    if (!isLoggedIn) { openLoginPromptModal(); return; }
    const items = Object.values(cartState);
    const error = document.getElementById('storePaymentError');
    error.textContent = '';
    if (items.some(item => !validStorePrice(item.price))) { error.textContent = 'A product price needs to be updated by the shop.'; return; }
    if (!items.length) { error.textContent = 'Add store items before checkout.'; return; }
    const payment = document.querySelector('input[name="storePaymentMethod"]:checked')?.value || 'Cash on Pickup';
    const gcash = payment === 'GCash';
    const reference = document.getElementById('storeGcashReference').value.trim();
    if (gcash && (!storeGcashSettings || storeGcashSettings.isActive === false || !storeGcashSettings.accountNumber)) {
        error.textContent = 'GCash is unavailable. Choose Cash Pickup.'; return;
    }
    if (gcash && !/^[0-9]{8,16}$/.test(reference)) {
        error.textContent = 'Enter a GCash reference number with 8–16 digits, usually 13.'; return;
    }
    const total = items.reduce((sum, item) => sum + Number(item.price) * item.qty, 0);
    if (!Number.isFinite(total) || total <= 0 || total > 5000000) { error.textContent = 'Order total must be greater than zero and at most 5,000,000.'; return; }
    const currentUser = JSON.parse(localStorage.getItem('kcz_logged_user') || '{}');
    const data = {
        customer: (currentUser.email || 'Customer').split('@')[0],
        items: items.map(item => ({id:item.id, productId:item.id, name:item.name, qty:item.qty, price:item.price})),
        service: 'In-Store: ' + items.map(item => `${item.name} (${item.qty}x)`).join(', '),
        copies: items.reduce((sum, item) => sum + item.qty, 0), pages: 1,
        paperSize: 'Store Supply', colorMode: 'N/A', sides: 'N/A', file: 'Store_Receipt.pdf',
        total, payment, gcashRef: gcash ? reference : '', gcashReceiptUrl: gcash ? storeGcashReceipt : '',
        merchantAccountNumber: gcash ? storeGcashSettings.accountNumber : '', orderDate: new Date().toLocaleString(),
    };
    storeCheckoutPending = true;
    updateStorePaymentControls();
    try {
        const response = await Loyalty.submitOrder(data, 'storePoints');
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.message || result.error || 'Unable to save order.');
        cartState = {};
        updateCartDrawerUI();
        clearStoreReceipt();
        toggleCartDrawer(false);
        alert(`Store order ${result.orderId} submitted for shop approval.`);
        window.location.href = '/customer?tab=myOrders';
    } catch (issue) { error.textContent = issue.message; }
    finally { storeCheckoutPending = false; updateStorePaymentControls(); }
}
