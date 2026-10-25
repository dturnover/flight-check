"""Private durable state, bounded HTTP, and provider adapters. No response bodies in logs."""
import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Failure(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def http(url, method='GET', data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        # Never include URL/query (API key), response (recipient), or provider error text.
        raise Failure(f'Provider HTTP {e.code}; check credentials, rate limits, or provider console privately.') from None
    except (OSError, ValueError):
        raise Failure('Provider transport/JSON failure; request may have been processed.') from None


class State:
    def __init__(self, directory, persistent=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'state.json'
        self.persistent = persistent
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'version': 1, 'observations': [], 'calls': [], 'alerts': [], 'runs': [], 'stopped': False}
        if self.data.get('version') != 1:
            raise Failure('Unknown state schema')

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, sort_keys=True, indent=2))
        temp.chmod(0o600)
        os.replace(temp, self.path)
        if self.persistent:
            for cmd in (['git', 'add', 'state.json'], ['git', 'commit', '--allow-empty', '-m', 'Checkpoint Flight Watch'], ['git', 'push', 'origin', 'HEAD:state']):
                r = subprocess.run(cmd, cwd=self.directory, capture_output=True)
                if r.returncode:
                    raise Failure('Private state checkpoint failed; stopping before further external side effects.')


class Search:
    def __init__(self, config, state, transport=http):
        self.config, self.state, self.transport = config, state, transport
        self.used = 0

    def query(self, **extra):
        now = now_utc()
        cutoff = now - timedelta(days=31)
        self.state.data['calls'] = [t for t in self.state.data['calls'] if datetime.fromisoformat(t) >= cutoff]
        if self.used >= self.config['max_api_calls_per_run'] or len(self.state.data['calls']) >= self.config['max_api_calls_per_rolling_31_days']:
            raise Failure('Search quota limit reached; no further requests made.')
        key = os.environ.get('SERPAPI_KEY')
        if not key:
            raise Failure('Missing SERPAPI_KEY')
        self.state.data['calls'].append(now.isoformat())
        # Reserve quota durably BEFORE making each request, including failed requests.
        self.state.save()
        self.used += 1
        params = {'engine': 'google_flights', 'api_key': key, 'type': 1,
                  'departure_id': self.config['origin'], 'arrival_id': self.config['destination'],
                  'outbound_date': self.config['outbound_date'], 'return_date': self.config['return_date'],
                  'adults': 1, 'travel_class': 1, 'currency': 'USD', 'gl': 'us', 'hl': 'en',
                  'show_hidden': 'true', 'deep_search': 'true', **extra}
        result = self.transport('https://serpapi.com/search.json?' + urllib.parse.urlencode(params))
        if result.get('error') or result.get('search_metadata', {}).get('status') != 'Success':
            # SerpApi's error text describes the query, not the key, so it is safe to log.
            detail = str(result.get('error') or result.get('search_metadata', {}).get('status'))[:200]
            raise Failure(f'Flight search failed or did not complete: {detail}')
        return result


class Delivery:
    def __init__(self, state, transport=http):
        self.state, self.transport = state, transport

    @staticmethod
    def require():
        keys = ['RESEND_API_KEY', 'ALERT_EMAIL', 'ALERT_FROM', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM', 'ALERT_PHONE']
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            raise Failure('Missing secret names: ' + ', '.join(missing))
        if os.environ.get('MESSAGING_COST_APPROVED') != 'true':
            raise Failure('Messaging costs must be explicitly approved before sending, including test messages.')

    def email_headers(self):
        return {'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'], 'Content-Type': 'application/json'}

    def sms_headers(self):
        auth = os.environ['TWILIO_ACCOUNT_SID'] + ':' + os.environ['TWILIO_AUTH_TOKEN']
        return {'Authorization': 'Basic ' + base64.b64encode(auth.encode()).decode(), 'Content-Type': 'application/x-www-form-urlencoded'}

    def sms_url(self):
        return 'https://api.twilio.com/2010-04-01/Accounts/' + os.environ['TWILIO_ACCOUNT_SID'] + '/Messages'

    def send(self, alert):
        self.require()
        errors = []
        for channel in ['email', 'sms']:
            receipt = alert.setdefault('receipts', {}).setdefault(channel, {})
            if receipt.get('status'):
                continue  # Never automatically resend an ambiguous or accepted request.
            receipt['status'] = 'attempting'
            self.state.save()
            try:
                if channel == 'email':
                    headers = self.email_headers()
                    headers['Idempotency-Key'] = 'flight-watch/' + alert['key']
                    result = self.transport('https://api.resend.com/emails', 'POST', json.dumps({
                        'from': os.environ['ALERT_FROM'], 'to': [os.environ['ALERT_EMAIL']],
                        'subject': alert['subject'], 'text': alert['body']}).encode(), headers)
                    receipt.update(id=result['id'], status='accepted')
                else:
                    result = self.transport(self.sms_url() + '.json', 'POST', urllib.parse.urlencode({
                        'From': os.environ['TWILIO_FROM'], 'To': os.environ['ALERT_PHONE'],
                        'Body': alert['sms']}).encode(), self.sms_headers())
                    receipt.update(id=result['sid'], status=result.get('status', 'accepted'))
            except (Failure, KeyError):
                receipt['status'] = 'uncertain'
                errors.append(channel)
            self.state.save()
        if errors:
            raise Failure('Delivery uncertain for ' + ', '.join(errors) + '; inspect private provider records before retrying.')

    def reconcile(self):
        errors = []
        for a in self.state.data['alerts']:
            for channel, receipt in a.get('receipts', {}).items():
                if receipt.get('status') in ('delivered', 'failed', 'undelivered', 'bounced', 'complained'):
                    continue
                if not receipt.get('id'):
                    continue
                try:
                    if channel == 'email':
                        r = self.transport('https://api.resend.com/emails/' + receipt['id'], headers=self.email_headers())
                        receipt['status'] = r.get('last_event', 'accepted').removeprefix('email.')
                    else:
                        r = self.transport(self.sms_url() + '/' + receipt['id'] + '.json', headers=self.sms_headers())
                        receipt['status'] = r['status']
                    self.state.save()
                except (Failure, KeyError):
                    errors.append(channel)
        if errors:
            raise Failure('Could not retrieve all delivery receipts.')


def delivered(alert):
    return all(alert.get('receipts', {}).get(c, {}).get('status') == 'delivered' for c in ['email', 'sms'])
