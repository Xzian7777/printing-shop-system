# Phone notifications

The Flask backend sends FCM notifications for admin chat replies and order approval, printing/preparation, ready-for-pickup, completion, and rejection. Web and mobile actions save a `push_jobs` record atomically with the source action. The backend reads the saved order or chat to determine the recipient.

## Enable the feature

1. In Firebase Console → Firestore Database → Rules, add the `match /push_jobs/{jobId}` block from the mobile project's `firestore.rules` inside `/databases/{database}/documents`, then Publish. Merge this block into the existing deployed rules.
2. Restart the updated Flask backend and keep it running with network access. The development server starts the sender in its reloader child. WSGI starts it on the first request. A separate always-running process can use `python push_worker.py` with the same server credentials. Concurrent workers coordinate through persisted leases.
3. Install the updated APK, sign in, and allow Android notifications. Device registration uses the authenticated email, UID, and profile role. Reopen the app after changing notification permission in Android Settings.
4. On the website, open a customer conversation and send a test reply. With the phone app closed normally and the phone online, verify the notification arrives and tapping it opens that customer's chat.
5. Submit a test printing or Store Supplies order. Approve it and move it through production to Ready for Pickup. Verify each update arrives and tapping it opens My Orders. Repeat the admin action from the mobile admin account.

The sender requires the backend to remain online. Closing the browser tab is fine. Stopping the backend pauses delivery until it runs again. Jobs expire after 24 hours. Delivery retries preserve successful device deliveries and use a stable event tag. FCM delivery is at-least-once, so a crash immediately after sending can still produce another alert.

Android Settings → Apps → Force stop suspends FCM until the app is opened again. Google Play services, notification permission, and a network connection are required. This differs from closing the app normally or returning to the home screen.

## Verification

`python -m unittest discover -s tests -q` runs offline delivery tests. The push rules tests run only when `FIRESTORE_EMULATOR_HOST` points to localhost and always use the isolated `demo-printshop-push` project. No production order or message is created by these tests.

Rules and live phone delivery must be checked during rollout. Source changes and an APK build alone do not publish Firestore rules or verify the phone's permission and FCM registration.

[Firebase Flutter message handling](https://firebase.google.com/docs/cloud-messaging/flutter/receive-messages)
