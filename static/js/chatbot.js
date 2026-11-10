// AI Chatbot Assistant Widget Logic
function toggleChatbotWindow() {
    const win = document.getElementById('chatbotWindow');
    if (win) win.classList.toggle('hidden');
}

function sendQuickPrompt(text) {
    const input = document.getElementById('chatbotInput');
    if (input) {
        input.value = text;
        sendChatbotMessage();
    }
}

function sendChatbotMessage() {
    const input = document.getElementById('chatbotInput');
    const msg = input.value.trim();
    if (!msg) return;

    const chatBox = document.getElementById('chatbotMessages');
    chatBox.innerHTML += `
        <div class="flex flex-col items-end">
            <div class="bg-blue-600 text-white p-2.5 rounded-xl max-w-[80%]">${msg}</div>
        </div>
    `;
    input.value = '';

    setTimeout(() => {
        let reply = "Thank you for your message! You can ask about prices, order status, or your RFID points balance.";
        const q = msg.toLowerCase();
        if (q.includes('magkano') || q.includes('price')) reply = "💰 Business Cards start at ₱250, Tarpaulins ₱15/sqft, Standard A4 ₱3.00/pg.";
        if (q.includes('order') || q.includes('status')) reply = "📦 Order #PRNT-8942 is currently [Printing 🖨️]. Estimated pickup: 3:30 PM.";

        chatBox.innerHTML += `
            <div class="flex flex-col items-start">
                <div class="bg-white border p-2.5 rounded-xl max-w-[80%] text-slate-800">${reply}</div>
            </div>
        `;
        chatBox.scrollTop = chatBox.scrollHeight;
    }, 600);
}
