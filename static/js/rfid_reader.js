let rfidReaders = [];
let rfidReaderRefreshPending = false;
let rfidLastEvent = '';

async function rfidRequest(path, data) {
    const response = await fetch(path, data === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.message || 'RFID operation failed.');
    return result;
}

async function pairRfidReader(event) {
    event.preventDefault();
    const button = event.submitter || event.target.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    try {
        const result = await rfidRequest('/api/admin/rfid/readers', {
            readerId: document.getElementById('rfidDeviceId').value.trim(),
            name: document.getElementById('rfidDeviceName').value.trim() || 'Counter reader',
            rotateKey: document.getElementById('rfidRotateKey').checked
        });
        document.getElementById('rfidPrivateKey').value = result.readerKey;
        document.getElementById('rfidKeyPanel').classList.remove('hidden');
        await refreshRfidReaders();
        document.getElementById('rfidReaderSelect').value = result.readerId;
        renderRfidReaderState();
        showActionNotice('Reader paired. Copy the private key into bridge setup.');
    } catch (error) { showActionNotice(error.message, 'error'); }
    finally { button.disabled = false; }
}

async function copyRfidReaderKey() {
    try {
        await navigator.clipboard.writeText(document.getElementById('rfidPrivateKey').value);
        showActionNotice('Private bridge key copied.');
    } catch (_) { showActionNotice('Select and copy the private key from the field.', 'error'); }
}

async function refreshRfidReaders() {
    if (rfidReaderRefreshPending) return;
    rfidReaderRefreshPending = true;
    try {
        const result = await rfidRequest('/api/admin/rfid/readers');
        rfidReaders = result.readers;
        const select = document.getElementById('rfidReaderSelect');
        const selected = select.value;
        select.replaceChildren(...rfidReaders.map(reader => new Option(`${reader.name} (${reader.readerId})`, reader.readerId)));
        if (rfidReaders.some(reader => reader.readerId === selected)) select.value = selected;
        renderRfidReaderState();
    } catch (error) { document.getElementById('rfidReaderStatus').textContent = error.message; }
    finally { rfidReaderRefreshPending = false; }
}

function renderRfidReaderState() {
    const reader = rfidReaders.find(item => item.readerId === document.getElementById('rfidReaderSelect').value);
    const status = document.getElementById('rfidReaderStatus');
    if (!reader) { status.textContent = 'Pair the USB reader to begin.'; return; }
    const capture = reader.capture || {};
    const scan = reader.lastScan || {};
    if (capture.id && !capture.used && new Date(capture.expiresAt).getTime() > Date.now()) {
        status.textContent = capture.cardUid ? `Captured ${capture.cardUid}. Select the customer and enroll.` :
            'Waiting for a card. Tap after the bridge console shows “capture card”.';
    } else {
        status.textContent = `${reader.lastSeen ? 'Last seen ' + new Date(reader.lastSeen).toLocaleTimeString() : 'Bridge not connected yet.'}` +
            (scan.eventId ? ` · ${scan.cardUid}: ${scan.outcome} (+${scan.pointsAdded} point)` : '');
    }
    if (scan.eventId && scan.eventId !== rfidLastEvent) {
        rfidLastEvent = scan.eventId;
        fetchAdminRfidData();
    }
}

async function captureRfidCard() {
    const readerId = document.getElementById('rfidReaderSelect').value;
    if (!readerId) { showActionNotice('Pair and select a reader first.', 'error'); return; }
    try {
        await rfidRequest('/api/admin/rfid/capture', {readerId});
        await refreshRfidReaders();
    } catch (error) { showActionNotice(error.message, 'error'); }
}

async function searchRfidCustomers() {
    try {
        const query = document.getElementById('rfidCustomerSearch').value.trim();
        if (query.length < 2) throw new Error('Enter at least two characters.');
        const result = await rfidRequest('/api/admin/loyalty/customers?q=' + encodeURIComponent(query));
        document.getElementById('rfidCustomerSelect').replaceChildren(...result.customers.map(customer =>
            new Option(`${customer.name} (${customer.email})`, customer.uid)));
        if (!result.customers.length) showActionNotice('No matching eligible customer accounts.', 'error');
    } catch (error) { showActionNotice(error.message, 'error'); }
}

async function enrollRfidCard() {
    const button = document.getElementById('rfidEnrollButton');
    if (button.disabled) return;
    button.disabled = true;
    try {
        const reader = rfidReaders.find(item => item.readerId === document.getElementById('rfidReaderSelect').value);
        const uid = document.getElementById('rfidCustomerSelect').value;
        if (!reader?.capture?.cardUid || !uid) throw new Error('Capture a card and select the customer first.');
        const result = await rfidRequest('/api/admin/rfid/cards', {readerId: reader.readerId,
            captureId: reader.capture.id, uid, replaceExisting: document.getElementById('rfidReplaceCard').checked});
        showActionNotice(`Card ${result.cardUid} enrolled. Remove it and tap again after the bridge is ready.`);
        await refreshRfidReaders();
        fetchAdminRfidData();
    } catch (error) { showActionNotice(error.message, 'error'); }
    finally { button.disabled = false; }
}

setInterval(() => {
    if (!document.hidden && currentAdminTab === 'rfidLoyalty') refreshRfidReaders();
}, 5000);
