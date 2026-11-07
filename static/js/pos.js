'use strict';
window.Pos = {
    catalog: [], items: [], pending: null, busy: false,
    id: () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join(''),
    money: value => new Intl.NumberFormat('en-PH', {style: 'currency', currency: 'PHP'}).format(value),
    cents: value => Math.floor(Number(value) * 100 + 0.50000001),
    validPrice(value) {
        if (!['string', 'number'].includes(typeof value) || String(value).trim() === '') return false;
        const price = Number(value);
        return Number.isFinite(price) && price >= 0 && price <= 5000000;
    },
    escape: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    error(message = '') { document.getElementById('posError').textContent = message; },
    async load() {
        try {
            const responses = await Promise.all([fetch('/api/store/products'), fetch('/api/services')]);
            if (responses.some(r => !r.ok)) throw Error('Unable to load the POS catalog.');
            const [products, services] = await Promise.all(responses.map(r => r.json()));
            this.catalog = [
                ...(Array.isArray(products) ? products : products.products || []).map(p => ({...p, kind: 'product'})),
                ...(Array.isArray(services) ? services : services.services || []).map(s => ({...s, kind: 'service', price: s.basePrice || 0}))
            ].filter(x => !x.archived && !x.isArchived);
            if (!this.pending && !this.busy) {
                for (const line of this.items.filter(x => x.kind === 'product')) {
                    const current = this.catalog.find(x => x.kind === 'product' && x.id === line.id);
                    if (current) line.unitPrice = Number(current.price);
                }
                this.renderItems();
            }
            this.renderCatalog();
            await this.history();
        } catch (error) { this.error(error.message); }
    },
    renderCatalog() {
        const search = document.getElementById('posSearch').value.toLowerCase();
        const category = document.getElementById('posCategory').value;
        const html = this.catalog.map((item, index) => {
            if (!String(item.name).toLowerCase().includes(search) || (category !== 'all' && item.kind !== category)) return '';
            const validPrice = this.validPrice(item.price);
            const unavailable = item.kind === 'product' && (Number(item.stock) <= 0 || !validPrice);
            return `<button type="button" class="pos-catalog-item" data-pos-add="${index}" ${unavailable || this.pending || this.busy ? 'disabled' : ''}><strong>${this.escape(item.name)}</strong><span>${validPrice ? this.money(item.price) : 'Price unavailable'}</span><small>${item.kind === 'product' ? `${Number(item.stock) || 0} available` : 'Service · set unit price'}</small></button>`;
        }).join('');
        document.getElementById('posCatalog').innerHTML = html || '<p>No matching items.</p>';
    },
    add(index) {
        if (this.pending || this.busy) return;
        const item = this.catalog[index];
        if (!item) return;
        if (item.kind === 'product' && !this.validPrice(item.price)) return this.error('Update the product price in Store Catalog before selling it.');
        const existing = this.items.find(x => x.kind === item.kind && x.id === item.id);
        const limit = item.kind === 'product' ? Number(item.stock) : 100000;
        if ((existing?.qty || 0) >= limit) return this.error('The available stock is already in this sale.');
        if (!existing && this.items.length >= 50) return this.error('A sale can contain up to 50 items.');
        if (existing) existing.qty++;
        else this.items.push({id: item.id, kind: item.kind, name: item.name, unitPrice: Number(item.price), qty: 1, note: ''});
        this.error(); this.renderItems();
    },
    renderItems() {
        document.getElementById('posItems').innerHTML = this.items.map((item, index) => `<div class="pos-line"><div class="pos-line-heading"><strong>${this.escape(item.name)}</strong><button type="button" data-pos-remove="${index}" aria-label="Remove ${this.escape(item.name)}">Remove</button></div><div class="pos-line-inputs"><label>Quantity<input data-pos-qty="${index}" type="number" min="1" max="100000" step="1" value="${item.qty}" inputmode="numeric"></label>${item.kind === 'service' ? `<label>Service unit price<input data-pos-price="${index}" type="number" min="0.01" max="5000000" step="0.01" value="${item.unitPrice}" inputmode="decimal"></label>` : `<span>${this.money(item.unitPrice)} each</span>`}<strong id="posLineTotal-${index}">${this.money(this.cents(item.unitPrice) * item.qty / 100)}</strong></div>${item.kind === 'service' ? `<label>Job details<input data-pos-note="${index}" maxlength="500" value="${this.escape(item.note)}" placeholder="e.g. A4 black and white, 10 pages"></label>` : ''}</div>`).join('') || '<p class="pos-help">Select an item to start a sale.</p>';
        this.renderTotal();
    },
    total() { return this.items.reduce((sum, x) => sum + this.cents(x.unitPrice) * (Number(x.qty) || 0), 0); },
    renderTotal() {
        const total = this.total();
        document.getElementById('posTotal').textContent = this.money(total / 100);
        this.pointsSelector?.update();
        const cash = this.cents(document.getElementById('posCash').value || 0);
        document.getElementById('posChange').textContent = this.money(Math.max(0, cash - total + Math.round((this.pointsSelector?.discount || 0)*100)) / 100);
        this.items.forEach((x, i) => { const el = document.getElementById(`posLineTotal-${i}`); if (el) el.textContent = this.money(this.cents(x.unitPrice) * (Number(x.qty) || 0) / 100); });
    },
    paymentChanged() {
        const cash = document.getElementById('posPayment').value === 'Cash';
        document.getElementById('posCashFields').hidden = !cash;
        document.getElementById('posGcashFields').hidden = cash;
    },
    async submit() {
        if (this.busy) return;
        this.error();
        if (!this.pending && this.reward) {
            this.pending = {submissionId:this.id(), buyerName:document.getElementById('posBuyer').value.trim(),
                customerUid:this.customer.uid, rewardId:this.reward.id, rewardVersion:this.reward.updatedAt, items:[], pointsUsed:0,
                expectedTotal:0, payment:'Points', cashReceived:0, paymentConfirmed:true};
        }
        if (!this.pending) {
            this.pointsSelector?.update();
            if (!this.items.length) return this.error('Add an item first.');
            if (this.items.some(x => !Number.isInteger(Number(x.qty)) || x.qty < 1 || x.qty > 100000 || !Number.isFinite(x.unitPrice) || x.unitPrice <= 0)) return this.error('Check item quantities and prices.');
            const total = this.total();
            if (!Number.isFinite(total) || total <= 0 || total > 500000000) return this.error('Total must be between 0.01 and 5,000,000.');
            const payment = document.getElementById('posPayment').value;
            const cash = document.getElementById('posCash').value;
            const reference = document.getElementById('posReference').value.trim();
            const confirmed = document.getElementById('posConfirmed').checked;
            if (payment === 'Cash' && this.cents(cash || 0) < total - Math.round((this.pointsSelector?.discount || 0)*100)) return this.error('Cash received must cover the total.');
            if (payment === 'GCash' && (!/^[0-9]{8,16}$/.test(reference) || !confirmed)) return this.error('Enter the GCash reference and confirm payment was received.');
            this.pending = {customerUid:this.customer?.uid, pointsUsed:this.pointsSelector?.points || 0, expectedDiscount:this.pointsSelector?.discount || 0, submissionId: this.id(), buyerName: document.getElementById('posBuyer').value.trim(), items: this.items.map(x => ({...x, qty: Number(x.qty)})), payment, cashReceived: cash || 0, gcashRef: reference, paymentConfirmed: confirmed, expectedTotal: total / 100};
        }
        try { sessionStorage.setItem('kcz-pos-pending', JSON.stringify(this.pending)); }
        catch (_) { return this.error('Unable to preserve this sale for safe retries. Enable browser storage.'); }
        this.busy = true; document.getElementById('posFields').disabled = true;
        document.getElementById('posComplete').disabled = true; this.renderCatalog();
        try {
            const response = await fetch('/api/pos/sales', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(this.pending)});
            const data = await response.json();
            if (!response.ok || !data.success) {
                if ([400, 409].includes(response.status)) { this.pending = null; sessionStorage.removeItem('kcz-pos-pending'); document.getElementById('posFields').disabled = false; }
                throw Error(data.message || 'Retry this same sale when the connection is restored.');
            }
            this.pending = null; sessionStorage.removeItem('kcz-pos-pending'); this.items = [];
            document.getElementById('posSaleForm').hidden = true;
            this.receipt(data.sale, true); await this.load();
        } catch (error) { this.error(error.message); }
        finally { this.busy = false; document.getElementById('posComplete').disabled = false; document.getElementById('posComplete').textContent = this.pending ? 'Retry same sale' : 'Complete sale'; this.renderCatalog(); }
    },
    receipt(sale, completed = false) {
        const panel = document.getElementById('posReceipt');
        completed = completed || document.getElementById('posSaleForm').hidden;
        panel.hidden = false;
        panel.innerHTML = `<div class="pos-receipt"><h3>KCZ Printing Shop</h3><p>Counter sale · ${this.escape(sale.id)}</p><p>Buyer · ${this.escape(sale.buyerName)}</p><p>${this.escape(new Date(sale.createdAt).toLocaleString())}</p>${sale.items.map(x => `<div class="pos-receipt-line"><span>${this.escape(x.name)} × ${x.qty}<small>${this.money(x.unitPrice)} each${x.note ? ' · ' + this.escape(x.note) : ''}</small></span><strong>${this.money(x.lineTotal)}</strong></div>`).join('')}<p>Subtotal · ${this.money(sale.subtotal ?? sale.total)} · Points used ${sale.pointsUsed || 0} · Discount ${this.money(sale.pointsDiscount || 0)} · Points earned ${sale.pointsEarned || 0}</p><div class="pos-total"><span>Total</span><strong>${this.money(sale.total)}</strong></div><p>Payment · ${this.escape(sale.payment)}${sale.gcashRef ? ' · ' + this.escape(sale.gcashRef) : ''}</p><p>Received · ${this.money(sale.cashReceived)} · Change · ${this.money(sale.change)}</p></div><div class="pos-receipt-actions"><button type="button" onclick="Pos.print()">Print receipt</button><button type="button" onclick="Pos.${completed ? 'newSale' : 'closeReceipt'}()">${completed ? 'New sale' : 'Close receipt'}</button></div>`;
    },
    closeReceipt() { document.getElementById('posReceipt').hidden = true; },
    newSale() {
        this.items = []; this.pending = null; this.reward = null; this.customer = null; Loyalty.account = null; this.error();
        document.getElementById('posSaleForm').reset(); document.getElementById('posFields').disabled = false;
        document.getElementById('posSaleForm').hidden = false; this.closeReceipt(); this.paymentChanged(); this.renderItems(); this.renderCatalog();
    },
    print() {
        const receipt = document.querySelector('#posReceipt .pos-receipt');
        if (!receipt) return;
        const win = window.open('', '_blank', 'width=420,height=650');
        if (!win) return this.error('Allow pop-ups to print the receipt.');
        win.document.write('<!doctype html><title>POS receipt</title><style>body{font:14px Arial;margin:24px;color:#111}.pos-receipt-line,.pos-total{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid #ddd}small{display:block;margin-top:4px}</style>' + receipt.outerHTML);
        win.document.close(); win.focus(); win.print();
    },
    async history() {
        const response = await fetch('/api/pos/sales');
        const data = await response.json();
        if (!response.ok) throw Error(data.message || 'Unable to load receipts.');
        this.sales = data.sales || [];
        document.getElementById('posHistory').innerHTML = this.sales.map((sale, i) => `<button type="button" data-pos-receipt="${i}"><strong>${this.escape(sale.id)}</strong><span>${this.escape(sale.buyerName)}</span><span>${this.money(sale.total)}</span></button>`).join('') || '<p class="pos-help">No counter sales yet.</p>';
    }
};
document.addEventListener('DOMContentLoaded', () => {
    const screen = document.getElementById('adminTabSection-pos');
    if (!screen) return;
    Pos.pointsSelector = Loyalty.selector(document.getElementById('posPoints'), () => Pos.total()/100);
    document.getElementById('posPoints').addEventListener('input', () => Pos.renderTotal());
    Loyalty.api('/api/loyalty/rewards').then(result => {
        Pos.rewards = result.rewards;
        for (const reward of Pos.rewards) document.getElementById('posReward').add(new Option(`${reward.name} · ${reward.pointsCost} points`, reward.id));
    }).catch(error => Pos.error(error.message));
    try {
        Pos.pending = JSON.parse(sessionStorage.getItem('kcz-pos-pending') || 'null');
        if (Pos.pending) {
            Pos.items = Pos.pending.items;
            document.getElementById('posBuyer').value = Pos.pending.buyerName;
            document.getElementById('posPayment').value = Pos.pending.payment;
            document.getElementById('posCash').value = Pos.pending.cashReceived;
            document.getElementById('posReference').value = Pos.pending.gcashRef;
            document.getElementById('posConfirmed').checked = Pos.pending.paymentConfirmed;
            document.getElementById('posFields').disabled = true;
            document.getElementById('posComplete').textContent = 'Retry same sale';
            Pos.paymentChanged(); Pos.renderItems();
            Pos.error('A previous sale needs confirmation. Retry it to retrieve the receipt.');
        }
    } catch (_) { sessionStorage.removeItem('kcz-pos-pending'); }

    screen.addEventListener('click', event => {
        const add = event.target.closest('[data-pos-add]');
        if (add) Pos.add(Number(add.dataset.posAdd));
        const remove = event.target.closest('[data-pos-remove]');
        if (remove && !Pos.pending && !Pos.busy) { Pos.items.splice(Number(remove.dataset.posRemove), 1); Pos.renderItems(); }
        const receipt = event.target.closest('[data-pos-receipt]');
        if (receipt && !Pos.pending && !Pos.busy) Pos.receipt(Pos.sales[Number(receipt.dataset.posReceipt)]);
    });
    screen.addEventListener('input', event => {
        if (Pos.pending || Pos.busy) return;
        const input = event.target;
        if (input.dataset.posQty !== undefined) Pos.items[Number(input.dataset.posQty)].qty = input.value;
        if (input.dataset.posPrice !== undefined) Pos.items[Number(input.dataset.posPrice)].unitPrice = Number(input.value);
        if (input.dataset.posNote !== undefined) Pos.items[Number(input.dataset.posNote)].note = input.value;
        Pos.renderTotal();
    });
});
