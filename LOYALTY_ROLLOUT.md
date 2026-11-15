# Current security update

Use the current loyalty epoch and balances for this update. The historical loyalty initialization instructions appear below.

## Verification on November 13, 2026

The local backend and Firestore Emulator suite passed all 159 tests. Flutter passed all 97 tests, including login error handling, debug backend configuration and action/logout feedback. The updated debug APK built and installed successfully during the rules rollout. Dart analysis reports zero errors, four existing unused declarations and 161 informational findings. Browser checks covered the login and recovery layouts at a 390-pixel viewport. The user confirmed that orders load correctly on the connected phone after the rules deployment. Guided checkout and cancellation testing is in progress.

The replacement Supabase project passed authenticated bucket access, upload, signed download, private-object public-access rejection and test-file cleanup checks. The public bucket is `kezcemzek-files`. The private bucket is `kezcemzek-orders-private`. Existing references to the previous Supabase project still require recovery and reviewed migration.

The user approved seven profile mappings and thirty legacy order mappings on November 13, 2026. These mappings were applied to live Firestore and verified with zero discrepancies. Six identity cases, two unmatched records and normalization metadata for six source profile documents still require review. The current readiness marker records this pending normalization review.

The user created the owner-filtered order index, and its READY state was verified through the Firestore API. The hardened rules were published and read back successfully, matching the tested local source. The deployment record is stored in the private `security-backups/security-rules-deployment-2026-11-13.json` file. Anonymous access checks rejected OTP records and private orders. USB forwarding connects the installed debug app to the running local backend.

Production hosting, HTTPS configuration and the final release build remain pending. The local session secret was rotated after its partial appearance in a screenshot, so existing website sessions require a fresh login. The user deferred recovery of the previous Supabase project's test attachments in favor of replacement uploads. Store inventory alerts now use an authenticated admin-only backend action with server-derived messages and duplicate protection. The shared website stock badge uses the existing store-products endpoint.

## Local verification

Install the tested backend dependencies with `pip install -r requirements.txt`. Set a private random `SESSION_SECRET` of at least 32 characters, the replacement Gmail `SMTP_PASSWORD`, Firebase credentials, and Supabase storage settings in the ignored `.env` file.

Run `python app.py` for local development. The debug APK uses `--dart-define=API_BASE_URL=http://127.0.0.1:5000` with USB port forwarding.

Debug runs default to `http://127.0.0.1:5000` when `API_BASE_URL` is omitted. An explicit `API_BASE_URL` overrides this default. Keep USB forwarding active for phone testing. Release builds require an explicit hosted HTTPS URL. Password-login profile failures display the backend error or a neutral sign-in message. Google sign-in retains its provider-specific guidance.

The login-fix debug APK was installed on the connected phone. The user confirmed that both email/password login and Continue with Google reach the dashboard successfully on November 13, 2026.

The mobile Cash on Pickup cancellation test used `PRNT-839770`. Live Firestore verification confirmed a canonical owner, matching profile email, cancellation by the owner, quantity one, `Cancelled` status, a server cancellation timestamp, `stockDeducted` set to false and an absent approval timestamp. Both inventory copies showed stock fifty. The account balance and reserved points were zero. The read-only verification record is stored in `security-backups/live-cancellation-PRNT-839770.json`. Confirmation of the web admin's visible Cancelled category and the approved-order cancellation lock remains part of guided testing.

The user confirmed that `PRNT-839770` appears in the web admin's Cancelled category. The next test order, `PRNT-654923`, was verified as Approved with a server approval timestamp and `stockDeducted` set to true. It fails the backend customer-cancellation eligibility conditions. The mobile cancellation action's visible state still requires user confirmation.

Order status actions now show upper-left feedback on web and mobile, and duplicate submissions are guarded while an action is pending. Logout shows a progress state on both platforms, disables repeated confirmation and restores retry controls on failure. The full Flutter suite passed 97 tests after these changes. Web behavior tests cover pending and successful approval notices, error messages, duplicate clicks and logout success/failure handling.

The user confirmed that the action notices and logout loading indicators work correctly on both web and mobile. The phone update uses its supported ARMv7 architecture. Generated Flutter test and app-build caches were cleaned after disk exhaustion interrupted packaging, and the rebuilt APK installed successfully.

The mobile printing order `PRNT-136091` was verified with quantity one, Cash on Pickup and a server-calculated total of three pesos. Its attachment uses the current Supabase project's private bucket and matching owner metadata. An initial direct download check timed out. The retry returned HTTP 200 with 354869 bytes and a matching upload SHA-256 hash. The anonymous public-object URL returned HTTP 400. Web download requests returned authenticated redirects. Visual confirmation that the PDF opens in the web admin remains part of guided testing.

The user confirmed that the PDF opens correctly in the web admin and that a Ready for Pickup notification arrives while the customer app is in the background.

The Processing tab and counter include Approved, Printing, Preparing and Processing orders on both platforms. Legacy Printing and Preparing statuses display as Processing in badges, timelines and order details. Production actions and new status notification messages use the Processing wording. Web admin history includes orders lacking `createdAt`, with the existing timestamp fallback handling sorting. The history cache schema was incremented to refresh existing clients. A live count snapshot is stored in `security-backups/processing-count-verification-2026-11-13.json`. The Flutter suite passed all 99 tests, and the backend suite passed 127 tests with 36 emulator-dependent cases skipped in this run.

## Reviewed migrations

Export a private report using `python tools/security_rollout.py --report security-backups/identity-review.json`. Review the UID, email, existing profile IDs, and proposed order ownership. Mark approved eligible rows in a reviewed copy, then run `python tools/security_rollout.py --apply-reviewed security-backups/identity-reviewed.json`.

The normalization stage adds indexed email metadata to approved identity paths or individually approved `records` rows. Review metadata-only rows for ambiguous identities separately. UID and RFID conflicts require separate resolution. Production startup stays blocked while normalization review is incomplete. Unmapped historical orders are displayed read-only through the authenticated history endpoint.

Export storage references with `python tools/security_rollout.py --file-report security-backups/files-review.json`. Verify the Supabase project is reachable and review each file object's ownership before marking it approved and using `--apply-files-reviewed`. The migration verifies a private copy before updating references and removing the public copy. Public catalog assets are identified separately in the manifest.

Apply the reviewed identity metadata, deploy the tested Firestore rules and required indexes, restart the backend, and update the debug APK together. Verify registration, password recovery, chat, ordinary and points checkouts, cancellation, approval, POS, private attachments, and notifications with test accounts.

## Production configuration

Production startup requires `SESSION_SECRET` and uses secure session cookies. On Linux, start `gunicorn --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT wsgi:app`. On Windows, use `waitress-serve --listen=0.0.0.0:5000 wsgi:app` behind HTTPS.

Build the release APK after setting the hosted HTTPS `API_BASE_URL`. Android release signing uses the ignored `android/key.properties` and private `android/app/kcz-release.jks`. Keep an encrypted backup of these signing materials. Public signing fingerprints are exported in the ignored `release-signing-fingerprints.txt`. Before replacing an installed debug build with the signed release, export local drafts.

Registration and recovery codes are stored as expiring hashes in `verification_challenges`, with server-only access. Configure the Firestore TTL policy on `expiresAt` for eventual cleanup. Expiry checks and one-time consumption run in the backend independently of TTL deletion.

---

# Loyalty setup and local testing

## Backend and database

Run the backend from this project directory with its configured Firebase service account. Mobile requests use verified Firebase ID tokens. Admin accounts must have `role` set to `admin` in `users/{firebaseUid}`.

The rollout command exports old testing balances before zeroing points. It preserves accounts, orders, RFID logs and existing ledger history. It enables the new loyalty epoch after the reset completes. A completed rollout cannot accidentally run again.

```powershell
python loyalty_rollout.py --backup loyalty-backups/initial-balances.json
python loyalty_rollout.py --backup loyalty-backups/initial-balances.json --apply
```

Review `unmatched` entries in the private backup. Resolve ambiguous profile identities through their actual Firebase Auth accounts. These records are excluded from account-linked redemptions until resolved. The backup directory is git-ignored because it contains customer identities.

Publish the mobile project's complete `firestore.rules`, update both clients and restart Flask together. The new rules require the backend for POS completion, order completion and reward-bearing order mutations. Keep the backend online while using these actions.

## Local Android testing

The requested local server URL is `http://127.0.0.1:5000`. Connect the Android device by USB, enable USB debugging and allow the computer. Run the following command after the device is connected.

```powershell
adb reverse tcp:5000 tcp:5000
```

Start Flask in the web project directory.

```powershell
python app.py
```

Build the local debug APK in the mobile project directory.

```powershell
flutter build apk --debug --dart-define=API_BASE_URL=http://127.0.0.1:5000
```

The debug manifest permits local HTTP testing. A hosted release should supply its reachable HTTPS `API_BASE_URL`. Reconnect USB forwarding after a device reboot or reconnection.

## Smoke test

1. Check zero balances on web and mobile. Create an admin reward linked to an existing product or service package.
2. Complete a paid customer order with the payment-received confirmation. Verify exactly one earned point on both clients.
3. Complete a linked POS sale and a guest sale. Verify one earned point for the linked account and zero for the guest.
4. Redeem points in printing and Store Supplies checkout. Verify the 50% limit, net Cash/GCash amount and pending reservation.
5. Cancel before approval and verify the reservation is released. Approve another order and verify final deduction and blocked customer cancellation.
6. Reject a reward-bearing order and verify one points return and correct stock restoration. Retry each request and verify no duplicate ledger entries.
7. Claim a free reward. Printing claims require an uploaded file and fixed package specifications. Verify zero money payment and zero earned points.
8. Disconnect the server during checkout. Reconnect and retry the saved request from Rewards or POS. Verify one order or receipt.

Discounts, reservations and earned points use the canonical loyalty account. Legacy profile point fields are retained as reset compatibility data. The RFID views display the canonical available balance. RFID identification does not award points.

## Historical loyalty rollout

Testing balances were backed up and reset for 16 profile records and 12 loyalty accounts. Four unmatched legacy profile records are listed in `loyalty-backups/initial-balances.json`.

The updated Firestore rules were published and the deployed ruleset was verified against the tested local file. Previous published rules are retained in `loyalty-backups/rules-before-loyalty.json`. The local Flask loyalty endpoint is active on port 5000.

The local debug APK includes `API_BASE_URL=http://127.0.0.1:5000`. The final APK was installed successfully on the connected Android phone. USB forwarding for port 5000 was configured. Full customer redemption transactions still require a live customer test.

The final archive/restore compatibility check preserves existing profile loyalty fields and initializes recreated mirrors with zero legacy points. The final verification passed 95 backend/emulator tests and 63 Flutter tests.
