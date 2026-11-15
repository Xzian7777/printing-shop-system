"""USB RC522 bridge with a durable, idempotent offline event queue."""
import argparse
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'rfid-local' / 'bridge.json'


class PersistentQueue:
    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT "pending", result TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def settings(self):
        with self.connection() as connection:
            row = connection.execute('SELECT value FROM settings WHERE key="reader_state"').fetchone()
        return json.loads(row[0]) if row else {'bindingsVersion': '', 'bindings': {}, 'captureId': ''}

    def update_settings(self, response):
        prior = self.settings()
        current = {**prior, 'bindingsVersion': response['bindingsVersion'], 'captureId': response.get('captureId') or ''}
        if 'bindings' in response:
            current['bindings'] = response['bindings']
        with self.connection() as connection:
            connection.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('reader_state', json.dumps(current)))

    def enqueue(self, packet):
        reader_id = packet.get('readerId', '')
        event_id = packet.get('eventId', '')
        card = packet.get('cardUid', '')
        if not re.fullmatch(r'[0-9A-F]{12}', reader_id) or not re.fullmatch(re.escape(reader_id) + r'-[1-9][0-9]{0,19}', event_id):
            raise ValueError('Malformed reader event')
        if not re.fullmatch(r'(?:[0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})', card):
            raise ValueError('Malformed card UID')
        state = self.settings()
        payload = {'readerId': reader_id, 'eventId': event_id, 'cardUid': card,
                   'bindingVersion': state['bindings'].get(card, ''), 'captureId': state.get('captureId', '')}
        with self.connection() as connection:
            prior = connection.execute('SELECT payload FROM events WHERE event_id=?', (event_id,)).fetchone()
            if prior:
                saved = json.loads(prior[0])
                if saved['cardUid'] != card or saved['readerId'] != reader_id:
                    raise ValueError('An existing event ID has different card data')
                return False
            connection.execute('INSERT INTO events(event_id,payload) VALUES (?,?)', (event_id, json.dumps(payload, sort_keys=True)))
        return True

    def next_event(self):
        with self.connection() as connection:
            row = connection.execute('SELECT payload FROM events WHERE state="pending" ORDER BY rowid LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def finish(self, event_id, result, state='done'):
        with self.connection() as connection:
            connection.execute('UPDATE events SET state=?,result=? WHERE event_id=?', (state, json.dumps(result), event_id))

    def pending_count(self):
        with self.connection() as connection:
            return connection.execute('SELECT COUNT(*) FROM events WHERE state="pending"').fetchone()[0]


def post(config, session, path, data):
    response = session.post(config['server'].rstrip('/') + path,
        headers={'X-RFID-Key': config['readerKey']}, json=data, timeout=(5, 20))
    if response.status_code >= 500 or response.status_code == 429:
        raise requests.ConnectionError('The shop server is temporarily unavailable')
    try:
        result = response.json()
    except ValueError:
        raise requests.ConnectionError('The shop server returned an invalid response') from None
    if not isinstance(result, dict):
        raise requests.ConnectionError('The shop server returned an invalid response')
    if response.status_code == 401:
        raise requests.ConnectionError('Reader authorization failed. Check the paired reader key')
    if response.status_code >= 400:
        return {'success': False, 'outcome': 'rejected', 'message': result.get('message', 'Request rejected')}
    if result.get('success') is not True:
        raise requests.ConnectionError('The shop server did not confirm the request')
    return result


def deliver_next(config, queue, session):
    payload = queue.next_event()
    if payload is None:
        return False
    result = post(config, session, '/api/iot/rfid/taps', payload)
    queue.finish(payload['eventId'], result, 'done' if result.get('success') else 'rejected')
    print(f"{payload['eventId']}: {result.get('outcome')} (+{result.get('pointsAdded', 0)} point), {queue.pending_count()} pending", flush=True)
    return True


def network_worker(config, queue, stop):
    session = requests.Session()
    next_sync = 0
    retry_delay = 1
    while not stop.is_set():
        try:
            if time.monotonic() >= next_sync:
                state = queue.settings()
                result = post(config, session, '/api/iot/rfid/state',
                    {'readerId': config['readerId'], 'bindingsVersion': state['bindingsVersion']})
                if not result.get('success'):
                    raise requests.ConnectionError(result.get('message'))
                queue.update_settings(result)
                if state.get('captureId') != result.get('captureId'):
                    print('Reader mode: capture card' if result.get('captureId') else 'Reader mode: ready for points', flush=True)
                next_sync = time.monotonic() + (2 if result.get('captureId') else 10)
            deliver_next(config, queue, session)
            retry_delay = 1
            stop.wait(.2)
        except (requests.RequestException, sqlite3.Error) as error:
            print(f'Backend unavailable ({type(error).__name__}). {queue.pending_count()} scans remain queued.', flush=True)
            stop.wait(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
    session.close()


def select_port(configured):
    from serial.tools import list_ports
    if configured != 'auto':
        return configured
    matches = [port.device for port in list_ports.comports() if port.vid in {0x10C4, 0x1A86, 0x303A}]
    if len(matches) > 1:
        raise ValueError('Multiple ESP32 ports found. Set --port to the reader COM port.')
    return matches[0] if matches else None


def run(config, queue):
    import serial
    stop = threading.Event()
    worker = threading.Thread(target=network_worker, args=(config, queue, stop), daemon=True)
    worker.start()
    try:
        while not stop.is_set():
            port = select_port(config['port'])
            if port is None:
                print('Waiting for the ESP32 USB port...', flush=True)
                stop.wait(5)
                continue
            try:
                with serial.Serial(port=None, baudrate=115200, timeout=1, write_timeout=2) as device:
                    device.dtr = False
                    device.rts = False
                    device.port = port
                    device.open()
                    device.write(b'PING\n')
                    print(f'Reader USB connected on {port}. Queue: {queue.pending_count()}', flush=True)
                    while not stop.is_set():
                        raw = device.readline(1024)
                        if not raw or not raw.endswith(b'\n'):
                            continue
                        try:
                            packet = json.loads(raw.decode('utf-8'))
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if packet.get('type') == 'error':
                            print(packet.get('message', 'Reader hardware error'), flush=True)
                        elif packet.get('readerId') != config['readerId']:
                            print('Connected board ID does not match the paired reader.', flush=True)
                        elif packet.get('type') == 'tap':
                            try:
                                added = queue.enqueue(packet)
                                device.write(('ACK ' + packet['eventId'] + '\n').encode())
                                if added:
                                    print(f"Scan saved: {packet['eventId']} ({queue.pending_count()} pending)", flush=True)
                            except (ValueError, sqlite3.Error) as error:
                                print(f'Scan could not be persisted: {type(error).__name__}', flush=True)
            except serial.SerialException:
                print('Reader disconnected or the COM port is busy. Retrying...', flush=True)
                stop.wait(3)
    except KeyboardInterrupt:
        print('Stopping bridge. Pending scans are saved.', flush=True)
    finally:
        stop.set()
        worker.join(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--reader-id')
    parser.add_argument('--port', default='auto')
    parser.add_argument('--server', default='http://127.0.0.1:5000')
    args = parser.parse_args()
    if args.setup:
        reader_id = (args.reader_id or input('Reader ID shown by ESP32: ')).upper().strip()
        if not re.fullmatch(r'[0-9A-F]{12}', reader_id):
            parser.error('Reader ID must contain twelve hexadecimal characters.')
        server = args.server.rstrip('/')
        from urllib.parse import urlparse
        parsed = urlparse(server)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ['127.0.0.1', 'localhost']):
            parser.error('Use localhost HTTP or a hosted HTTPS backend.')
        key = getpass.getpass('Paste the private reader key: ').strip()
        config = {'readerId': reader_id, 'readerKey': key, 'port': args.port, 'server': server}
        with requests.Session() as session:
            verified = post(config, session, '/api/iot/rfid/state', {'readerId': reader_id, 'bindingsVersion': ''})
        if not verified.get('success'):
            parser.error(verified.get('message', 'Reader pairing failed.'))
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(json.dumps(config, indent=2), encoding='utf-8')
        os.chmod(args.config, 0o600)
        queue = PersistentQueue(args.config.with_suffix('.sqlite3'))
        queue.update_settings(verified)
        print('Reader pairing saved privately. Start the bridge without --setup.')
        return
    if not args.config.exists():
        parser.error('Pair the reader with --setup first.')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.port != 'auto':
        config['port'] = args.port
    run(config, PersistentQueue(args.config.with_suffix('.sqlite3')))


if __name__ == '__main__':
    main()
