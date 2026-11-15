# Vercel deployment

Vercel loads the Flask instance from `app.py` using Python 3.12 and installs dependencies from `requirements.txt`. The build copies existing static assets into `public/static`, preserving `/static` URLs and the current UI. Local development continues through `python app.py`. The existing `wsgi.py` remains available for conventional WSGI hosting.

## Private project environment variables

Set these in Vercel Project Settings for the deployment environment. Keep credentials in private environment variables.

| Variable | Value |
| --- | --- |
| `APP_ENV` | `production` |
| `PUSH_WORKER_MODE` | `external` |
| `SESSION_SECRET` | A private random value containing at least 32 characters |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Complete service-account JSON from the private local credential file |
| `FIREBASE_API_KEY` | Existing Firebase web API key |
| `SUPABASE_URL` | Existing Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Existing private Supabase service-role key |
| `BUCKET_NAME` | Existing public catalog bucket |
| `PRIVATE_BUCKET_NAME` | Existing private customer-document bucket |
| `SMTP_EMAIL` | Existing sender email |
| `SMTP_PASSWORD` | Rotated Gmail app password |
| `SMTP_SERVER` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |

Use the same production credentials for the web app and notification daemon. The ignore files exclude local secrets, service-account files, backups, firmware, and virtual environments from deployment uploads and container build context.

## Uploads

Web and updated mobile clients request signed staging URLs from `POST /api/uploads/prepare`. Files upload directly to private Supabase staging. `POST /api/uploads/finalize` verifies ownership, expiry, declared size, and existing file-format rules, then publishes the verified bytes to a separate final path. Private ownership metadata and completion state commit atomically. Retries reuse the same file identifier and final object path.

Limits remain 10 MB per file, five files, and 25 MB combined per preparation request. Preparation and finalization payloads contain metadata, keeping file bodies outside Vercel Functions' 4.5 MB request limit. The existing multipart endpoint remains available for compatible local callers. Older mobile builds using multipart uploads require updating for large production files.

Staging objects remain private until the signed upload tokens have expired. The background worker removes expired staging objects every five minutes. Signed download links expire after fifteen minutes and refresh through the existing authorized order-file endpoint.

## Notifications

`vercel.json` declares one notification daemon with 1 GiB of provisioned memory in Singapore (`sin1`). `Dockerfile.worker` runs `tools/push_daemon.py`, which uses the existing Firestore job queue and delivery leases. The worker handles shutdown signals and finishes its current delivery before exiting. Web request handlers use external-worker mode on Vercel.

Vercel Daemons is a public-beta service available within plan quotas. Confirm its availability and quota in the Vercel project. Daemons run in production deployments. Live notification delivery must be tested after production deployment.

## Deployment and verification

1. Review the remaining identity-normalization report. Production startup currently requires `settings/security_config.normalizedIdentityVersion` to equal `1`. Apply only reviewed metadata rows through the existing migration flow.
2. Sign in to Vercel and import the `printing-shop-system` repository with the Flask project as its root directory. Ensure the current reviewed changes are included in the uploaded source.
3. Configure the private environment variables above. Keep the committed Vercel entrypoint and build settings.
4. Deploy and inspect web build logs, Flask runtime logs, and notification-daemon logs.
5. Add the production hostname to Firebase Authentication's authorized domains. On the existing Google OAuth web client, add `https://printing-shop-system.vercel.app` as an authorized JavaScript origin and `https://printing-shop-system.vercel.app/__/auth/handler` as an authorized redirect URI. The Vercel rewrite proxies Firebase auth helpers under the same origin so popup-blocked redirect sign-in can resume with browser storage partitioning enabled. Verify web Google sign-in.
6. Test email registration/reset, password and Google login, customer/admin roles, checkout, points, cancellation, POS, archive/restore, chat, private files, and background notifications on the HTTPS URL. Include an upload larger than 4.5 MB.
7. Build the Android app with `--dart-define=API_BASE_URL=https://YOUR-PRODUCTION-HOST` and verify cross-platform order and notification flows.

Production acceptance requires a successful hosted build and the live checks above. Local tests and storage tests verify preparation changes. Vercel runtime behavior, account quotas, SMTP connectivity, and production Google sign-in require the deployed environment.

## References

- [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask)
- [Vercel Functions limits](https://vercel.com/docs/functions/limitations)
- [Vercel Daemons](https://vercel.com/docs/daemons)
- [Supabase signed upload URLs](https://supabase.com/docs/reference/python/storage-from-createsigneduploadurl)

## Verified deployment on November 14, 2026

- Production is served at `https://printing-shop-system.vercel.app` using the existing Vercel project.
- Private project variables are configured for production and preview. The six reviewed normalized-email metadata rows were applied atomically with a backup and readback verification.
- Public login and static assets return HTTP 200. Anonymous account/order/upload requests and forged Google/upload/order credentials return HTTP 401.
- The notification daemon reports one ready instance in Singapore with 1 GiB of provisioned memory.
- A synthetic 5 MB PDF passed direct Supabase staging, backend finalization, digest verification, and repeat finalization. Test storage objects were cleaned up.
- The backend/Firestore Emulator suite passed 180 tests. The final backend/browser suite passed 183 tests with 39 emulator-only tests skipped. Flutter passed 100 tests. Dart analysis reported zero errors and four existing unused-declaration warnings, plus informational lint findings.
- The signed ARMv7 Android release APK was built with the production HTTPS URL and its signature verified. Its release SHA1 and SHA256 fingerprints are registered in Firebase. Installation and live phone verification remain pending.
- The owner saved the production origin and auth-handler redirect URI on the existing Google OAuth web client. Google accepts the redirect URI and routes to its sign-in page. The same-origin auth helper scripts return HTTP 200 through the deployed proxy. The user confirmed successful Google sign-in to the hosted dashboard.
- Hosted printing checkout produced PRNT-334996. Canonical customer ownership, pending status, private attachment ownership, successful download, size, and digest were verified. The user confirmed successful admin attachment viewing and Cancelled status in both customer and admin categories. Stored cancellation owner, server timestamp, and zero stock deduction were verified. A cancelled-status omission in the push sender was fixed and deployed, with nine push-worker regressions passing. The original undelivered test notification was retried once and processed to sent status with zero device deliveries recorded. Actual-phone push delivery remains pending. Hosted OTP email delivery and end-to-end phone push delivery require the remaining live-account checks.
