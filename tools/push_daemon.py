"""Run the existing Firestore-backed push sender as a managed background process."""
import signal
import sys
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    import app

    if app.db is None:
        raise SystemExit('Firebase credentials are unavailable.')
    shutdown = Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    app.run(app.db, app.send_fcm_push_notification, shutdown)
