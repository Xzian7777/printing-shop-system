// ============================================================
// firebase-messaging-sw.js
// Firebase Cloud Messaging Service Worker
// Handles background push notifications for Kez C-Em Zek Shop
// ============================================================

importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js');

// ---------------------------------------------------------------------------
// Firebase config for the service worker (public web config only — no secrets)
// ---------------------------------------------------------------------------
firebase.initializeApp({
    apiKey:            "AIzaSyBDk0kto8_VpvtzkQBdjrVP2YHOuUZ17rQ",
    authDomain:        "printingshop-7fd66.firebaseapp.com",
    projectId:         "printingshop-7fd66",
    storageBucket:     "printingshop-7fd66.firebasestorage.app",
    messagingSenderId: "800500466642",
    appId:             "1:800500466642:web:762350381a60650b16fe0b"
});

const messaging = firebase.messaging();

// ---------------------------------------------------------------------------
// Background message handler — fired when the app tab is NOT in focus
// ---------------------------------------------------------------------------
messaging.onBackgroundMessage(function(payload) {
    console.log('[SW] Background message received:', payload);

    const notifTitle = (payload.notification && payload.notification.title)
        || (payload.data && payload.data.title)
        || 'Kez C-Em Zek Shop';

    const notifBody = (payload.notification && payload.notification.body)
        || (payload.data && payload.data.body)
        || 'You have a new message.';

    const clickUrl  = (payload.data && payload.data.click_url) || '/';
    const convId    = (payload.data && payload.data.conversation_id) || '';
    const tag       = convId ? `kcz_chat_${convId}` : 'kcz_chat_general';

    const notifOptions = {
        body:    notifBody,
        icon:    '/static/icons/icon-192.png',
        badge:   '/static/icons/icon-192.png',
        tag:     tag,
        renotify: true,
        data: {
            click_url:       clickUrl,
            conversation_id: convId
        }
    };

    return self.registration.showNotification(notifTitle, notifOptions);
});

// ---------------------------------------------------------------------------
// Notification click handler — opens / focuses the shop tab
// ---------------------------------------------------------------------------
self.addEventListener('notificationclick', function(event) {
    event.notification.close();

    const clickUrl = (event.notification.data && event.notification.data.click_url) || '/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(windowClients) {
            // Try to focus an existing tab on the same origin
            for (let i = 0; i < windowClients.length; i++) {
                const client = windowClients[i];
                const clientUrl = new URL(client.url);
                const targetUrl = new URL(clickUrl, self.location.origin);

                if (clientUrl.origin === targetUrl.origin) {
                    // Navigate the existing tab to the target URL
                    client.navigate(targetUrl.href);
                    return client.focus();
                }
            }
            // No existing tab — open a new one
            return clients.openWindow(clickUrl);
        })
    );
});
