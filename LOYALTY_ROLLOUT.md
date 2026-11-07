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

## Verified local rollout

Testing balances were backed up and reset for 16 profile records and 12 loyalty accounts. Four unmatched legacy profile records are listed in `loyalty-backups/initial-balances.json`.

The updated Firestore rules were published and the deployed ruleset was verified against the tested local file. Previous published rules are retained in `loyalty-backups/rules-before-loyalty.json`. The local Flask loyalty endpoint is active on port 5000.

The local debug APK includes `API_BASE_URL=http://127.0.0.1:5000`. No Android device was connected during verification. Install the APK and run the USB forwarding command before phone testing.
