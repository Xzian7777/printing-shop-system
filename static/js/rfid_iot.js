// Arduino RFID & IoT Stock Sensor Logic
let currentPoints = 150;

function simulateRfidTapAction() {
    fetch('/api/rfid/tap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rfidCardUid: "CARD-8842-PH" })
    })
    .then(res => res.json())
    .then(data => {
        currentPoints += 20;
        const ptsElem = document.getElementById('rfidPointsText');
        if (ptsElem) ptsElem.textContent = `${currentPoints} PTS`;
        showToast("💳 Arduino RFID Card Tapped! +20 Points Earned.");
    });
}

function triggerLowStockAlert() {
    const bar = document.getElementById('tray1Bar');
    if (bar) {
        bar.style.width = '10%';
        bar.className = 'bg-rose-500 h-3 rounded-full transition-all';
    }
    showToast("⚠️ ALERT: Arduino Ultrasonic Sensor detected Low Paper Stock!");
}
