// ============================================================
// push_notifications.js
// Firebase Cloud Messaging — client-side push notification setup
// Used by both admin.html and customer.html
// ============================================================

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------
let _kcz_firebase_app    = null;
let _kcz_messaging       = null;
let _kcz_current_token   = null;
let _kcz_push_enabled    = false;

// ---------------------------------------------------------------------------
// Bootstrap — call once on page load (DOMContentLoaded)
// ---------------------------------------------------------------------------
async function kczInitPushNotifications() {
    if (!('Notification' in window)) {
        console.warn('[Push] Browser does not support notifications.');
        return;
    }
    if (!('serviceWorker' in navigator)) {
        console.warn('[Push] Service workers not supported.');
        return;
    }

    try {
        // 1. Fetch public Firebase config + VAPID key from the backend
        const res  = await fetch('/api/notifications/public-config');
        const data = await res.json();

        if (!data.success || !data.firebaseConfig || !data.vapidKey) {
            console.warn('[Push] Could not load public notification config.');
            return;
        }

        // Skip if this is still using the placeholder vapid key
        if (data.vapidKey.includes('REPLACE_WITH') || data.vapidKey.includes('dummy')) {
            console.warn('[Push] VAPID key is a placeholder. Push notifications are disabled until a real key is set.');
            return;
        }

        const { firebaseConfig, vapidKey } = data;

        // 2. Dynamically import Firebase SDK (module syntax via compat CDN is handled globally)
        //    We expect firebase-app-compat + firebase-messaging-compat to be loaded as <script> tags
        if (typeof firebase === 'undefined') {
            console.warn('[Push] Firebase SDK not loaded. Make sure the compat scripts are included.');
            return;
        }

        // 3. Initialize Firebase app only once
        if (!_kcz_firebase_app) {
            const existingApps = firebase.apps || [];
            _kcz_firebase_app = existingApps.length > 0
                ? existingApps[0]
                : firebase.initializeApp(firebaseConfig);
        }

        // 4. Get messaging instance
        _kcz_messaging = firebase.messaging();

        // 5. Register the service worker at root scope
        const swReg = await navigator.serviceWorker.register('/firebase-messaging-sw.js', { scope: '/' });
        await navigator.serviceWorker.ready;
        console.log('[Push] Service worker registered:', swReg.scope);

        // 6. If permission is already granted, silently get the token and register it
        if (Notification.permission === 'granted') {
            await _kczGetAndRegisterToken(vapidKey);
            _kcz_push_enabled = true;
            _kczUpdateNotifButtons(true);
        } else if (Notification.permission === 'denied') {
            _kczUpdateNotifButtons(false, true /* denied */);
        }
        // If permission === 'default', wait for user to click the button

        // 7. Wire up all push-notif toggle buttons on the page
        document.querySelectorAll('.push-notif-toggle-btn').forEach(btn => {
            btn.addEventListener('click', () => kczRequestPushPermission(vapidKey));
        });

    } catch (err) {
        console.error('[Push] Initialization error:', err);
    }
}

// ---------------------------------------------------------------------------
// Request permission + register token (triggered by button click)
// ---------------------------------------------------------------------------
async function kczRequestPushPermission(vapidKey) {
    if (!_kcz_messaging) {
        console.warn('[Push] Messaging not initialized.');
        return;
    }

    if (_kcz_push_enabled && _kcz_current_token) {
        // Already enabled — give user option to disable
        const confirmDisable = confirm(
            'Push notifications are already enabled on this browser.\n\n' +
            'Click OK to disable them, or Cancel to keep them active.'
        );
        if (confirmDisable) {
            await _kczUnregisterToken();
        }
        return;
    }

    if (Notification.permission === 'denied') {
        alert(
            '🔔 Push notifications are blocked by your browser.\n\n' +
            'To enable them:\n' +
            '1. Click the 🔒 lock icon or ⚙ settings icon in your browser address bar.\n' +
            '2. Set Notifications to "Allow".\n' +
            '3. Refresh the page and try again.'
        );
        return;
    }

    try {
        const permission = await Notification.requestPermission();
        if (permission === 'granted') {
            await _kczGetAndRegisterToken(vapidKey);
            _kcz_push_enabled = true;
            _kczUpdateNotifButtons(true);
            _kczShowPushToast('🔔 Push notifications enabled! You will now receive alerts.', 'success');
        } else {
            _kczUpdateNotifButtons(false, true);
            _kczShowPushToast('Push notifications were blocked. You can enable them in browser settings.', 'warning');
        }
    } catch (err) {
        console.error('[Push] Permission request error:', err);
    }
}

// ---------------------------------------------------------------------------
// Internal: get FCM token and POST it to the backend
// ---------------------------------------------------------------------------
async function _kczGetAndRegisterToken(vapidKey) {
    try {
        const swReg = await navigator.serviceWorker.ready;
        const token = await _kcz_messaging.getToken({
            vapidKey:         vapidKey,
            serviceWorkerRegistration: swReg
        });

        if (!token) {
            console.warn('[Push] No registration token available.');
            return;
        }

        _kcz_current_token = token;

        // Register token with the Flask backend
        const res = await fetch('/api/notifications/fcm-token', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ token: token, device: navigator.userAgent.substring(0, 120) })
        });
        const data = await res.json();
        if (data.success) {
            console.log('[Push] FCM token registered with backend.');
            localStorage.setItem('kcz_fcm_token', token);
        } else {
            console.warn('[Push] Backend token registration failed:', data.message);
        }

        // Handle foreground messages (tab in focus)
        _kcz_messaging.onMessage(function(payload) {
            console.log('[Push] Foreground message:', payload);
            _kczHandleForegroundMessage(payload);
        });

        // Watch for token refresh
        _kcz_messaging.onTokenRefresh(async function() {
            console.log('[Push] Token refreshed — re-registering...');
            await _kczGetAndRegisterToken(vapidKey);
        });

    } catch (err) {
        console.error('[Push] Error getting FCM token:', err);
    }
}

// ---------------------------------------------------------------------------
// Internal: remove token from backend on opt-out
// ---------------------------------------------------------------------------
async function _kczUnregisterToken() {
    const token = _kcz_current_token || localStorage.getItem('kcz_fcm_token');
    if (!token) return;

    try {
        await fetch('/api/notifications/fcm-token', {
            method:  'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ token: token })
        });
        if (_kcz_messaging) {
            await _kcz_messaging.deleteToken();
        }
        _kcz_current_token = null;
        _kcz_push_enabled  = false;
        localStorage.removeItem('kcz_fcm_token');
        _kczUpdateNotifButtons(false);
        _kczShowPushToast('Push notifications disabled on this device.', 'info');
        console.log('[Push] Token unregistered.');
    } catch (err) {
        console.error('[Push] Error unregistering token:', err);
    }
}

// ---------------------------------------------------------------------------
// Foreground message handler — shows an in-page toast instead of OS notification
// ---------------------------------------------------------------------------
function _kczHandleForegroundMessage(payload) {
    const title   = (payload.notification && payload.notification.title)
                 || (payload.data && payload.data.title)
                 || 'Kez C-Em Zek Shop';
    const body    = (payload.notification && payload.notification.body)
                 || (payload.data && payload.data.body)
                 || 'You have a new message.';
    const clickUrl = (payload.data && payload.data.click_url) || null;

    _kczShowPushToast(`💬 ${title}: ${body}`, 'message', clickUrl);
}

// ---------------------------------------------------------------------------
// UI helpers: update toggle button appearance + show toast
// ---------------------------------------------------------------------------
function _kczUpdateNotifButtons(enabled, denied) {
    document.querySelectorAll('.push-notif-toggle-btn').forEach(btn => {
        const iconEl = btn.querySelector('i');
        const spanEl = btn.querySelector('span');

        if (denied) {
            if (iconEl) { iconEl.className = 'fas fa-bell-slash text-rose-400'; }
            if (spanEl) spanEl.textContent = 'Blocked';
            btn.title = 'Notifications blocked — enable in browser settings';
            btn.classList.remove('bg-blue-600');
            btn.classList.add('bg-slate-800');
        } else if (enabled) {
            if (iconEl) { iconEl.className = 'fas fa-bell text-emerald-400'; }
            if (spanEl) spanEl.textContent = 'Alerts ON';
            btn.title = 'Push notifications are enabled. Click to disable.';
            btn.classList.remove('bg-slate-800');
            btn.classList.add('bg-blue-600/30', 'border-blue-500/50');
        } else {
            if (iconEl) { iconEl.className = 'far fa-bell text-slate-400'; }
            if (spanEl) spanEl.textContent = 'Enable Alerts';
            btn.title = 'Enable browser push notifications for chat replies';
            btn.classList.remove('bg-blue-600/30', 'border-blue-500/50');
            btn.classList.add('bg-slate-800');
        }
    });
}

function _kczShowPushToast(message, type = 'info', clickUrl = null) {
    // Remove any existing push toast
    const old = document.getElementById('kczPushToast');
    if (old) old.remove();

    const colors = {
        success: 'border-emerald-500/40 bg-emerald-900/80',
        warning: 'border-amber-500/40 bg-amber-900/80',
        info:    'border-slate-500/40 bg-slate-900/80',
        message: 'border-blue-500/40 bg-blue-900/80'
    };
    const color = colors[type] || colors.info;

    const toast = document.createElement('div');
    toast.id = 'kczPushToast';
    toast.className = `fixed bottom-5 right-5 z-[200] max-w-sm text-white text-xs font-semibold px-4 py-3 rounded-2xl shadow-2xl border ${color} flex items-start gap-3 cursor-pointer transition-all animate-pulse`;
    toast.innerHTML = `
        <span class="flex-1 leading-relaxed">${message}</span>
        <button onclick="document.getElementById('kczPushToast')?.remove()" class="text-slate-300 hover:text-white ml-1 shrink-0 mt-0.5">✕</button>
    `;

    if (clickUrl) {
        toast.addEventListener('click', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            window.location.href = clickUrl;
        });
    }

    document.body.appendChild(toast);

    // Auto-dismiss after 6 seconds
    setTimeout(() => {
        if (document.getElementById('kczPushToast') === toast) toast.remove();
    }, 6000);
}
