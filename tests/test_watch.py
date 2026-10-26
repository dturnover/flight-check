import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from flight_watch.core import candidate, reason, message, rows
from flight_watch.runtime import State, Search, Delivery, Failure, delivered
from flight_watch.__main__ import run, validate

CONFIG = json.loads(Path('config.json').read_text())
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def segment(a, b, depart, arrive, number):
    return {'departure_airport': {'id': a, 'time': depart}, 'arrival_airport': {'id': b, 'time': arrive},
            'airline': 'SYNTHETIC TEST AIRLINE', 'flight_number': number, 'duration': 100}


def fixtures():
    # Deliberately synthetic; never used by runtime or presented as live quotes.
    out = {'flights': [segment('LAX', 'TPE', '2026-12-18 18:00', '2026-12-20 01:00', 'TEST1'),
                       segment('TPE', 'BKK', '2026-12-20 04:00', '2026-12-20 06:00', 'TEST2')],
           'total_duration': 2400, 'price': 999, 'type': 'Round trip', 'departure_token': 'TEST-out'}
    ret = {'flights': [segment('BKK', 'TPE', '2027-01-03 08:00', '2027-01-03 12:00', 'TEST3'),
                       segment('TPE', 'LAX', '2027-01-03 15:00', '2027-01-03 17:00', 'TEST4')],
           'total_duration': 2100, 'price': 1200, 'type': 'Round trip', 'booking_token': 'TEST-book'}
    return out, ret


def response(flights):
    return {'search_metadata': {'status': 'Success'}, 'best_flights': flights}


class FareTests(unittest.TestCase):
    def setUp(self):
        self.out, self.ret = fixtures()
        self.c = copy.deepcopy(CONFIG)

    def fare(self):
        return candidate(self.out, self.ret, self.c)

    def test_round_trip_not_sum(self):
        self.assertEqual(self.fare()['price'], 1200)

    def test_no_inferred_protection(self):
        self.assertEqual(self.fare()['protection'], 'unknown')

    def test_cutoff_boundary_and_timezone(self):
        self.fare()
        self.out['flights'][0]['departure_airport']['time'] = '2026-12-18 17:59'
        with self.assertRaises(ValueError): self.fare()

    def test_wrong_return_date(self):
        self.ret['flights'][0]['departure_airport']['time'] = '2027-01-04 08:00'
        with self.assertRaises(ValueError): self.fare()

    def test_no_substitute_destination(self):
        self.out['flights'][-1]['arrival_airport']['id'] = 'URT'
        with self.assertRaises(ValueError): self.fare()

    def test_known_self_transfer_nested(self):
        self.ret['flights'][1]['extensions'] = ['Self transfer']
        with self.assertRaises(ValueError): self.fare()

    def test_airport_change(self):
        self.out['flights'][1]['departure_airport']['id'] = 'DMK'
        with self.assertRaises(ValueError): self.fare()

    def test_tight_connection(self):
        self.ret['flights'][1]['departure_airport']['time'] = '2027-01-03 09:30'
        with self.assertRaises(ValueError): self.fare()

    def test_non_round_trip_rejected(self):
        self.ret['type'] = 'One way'
        with self.assertRaises(ValueError): self.fare()

    def test_bad_price(self):
        for p in [None, -1, True, '100', float('nan')]:
            self.ret['price'] = p
            with self.assertRaises(ValueError): self.fare()

    def test_baggage_cohort_distinct(self):
        old = self.fare()['cohort']
        self.ret['extensions'] = ['1 checked bag included']
        self.assertNotEqual(old, self.fare()['cohort'])

    def test_cold_start_honest(self):
        self.assertIn('NOT yet', reason(self.fare(), [], [], self.c, NOW))

    def test_baseline_daily_not_result_weighted(self):
        f = self.fare()
        obs = [dict(f, price=1600, at=(NOW-timedelta(days=i+1)).isoformat()) for i in range(7)]
        self.assertIn('25%', reason(f, obs, [], self.c, NOW))

    def test_dedupe_and_improvement(self):
        f = self.fare()
        alerts = [dict(f, at=(NOW-timedelta(days=3)).isoformat())]
        self.assertIsNone(reason(f, [], alerts, self.c, NOW))
        obs = [dict(f, at=(NOW-timedelta(days=1)).isoformat())]
        f['price'] = 1000
        self.assertIn('dropped', reason(f, obs, alerts, self.c, NOW))

    def test_message_full_details(self):
        text = message(self.fare(), 'synthetic test', self.c)
        for part in ['TOTAL round trip', 'UNKNOWN', '2027-01-03', 'Layover', 'Baggage', 'https://', 'Return:', 'Outbound:']:
            self.assertIn(part, text)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = State(self.tmp.name)
        self.config = copy.deepcopy(CONFIG)
        self.env = patch.dict(os.environ, {k: 'TEST' for k in ['SERPAPI_KEY','RESEND_API_KEY','ALERT_EMAIL','ALERT_FROM','TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_FROM','ALERT_PHONE']} | {'MESSAGING_COST_APPROVED': 'true'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_quota_reserved_before_failure(self):
        def fail(*a, **kw): raise Failure('rate limit')
        s = Search(self.config, self.state, fail)
        with self.assertRaises(Failure): s.query()
        self.assertEqual(len(State(self.tmp.name).data['calls']), 1)

    def test_quota_cap_no_call(self):
        self.config['max_api_calls_per_run'] = 0
        with self.assertRaises(Failure): Search(self.config, self.state, lambda *a: self.fail()).query()

    def test_state_failure_prevents_request(self):
        with patch.object(self.state, 'save', side_effect=Failure('disk')):
            with self.assertRaises(Failure): Search(self.config, self.state, lambda *a: self.fail()).query()

    def test_provider_error_not_empty_success(self):
        with self.assertRaises(Failure): Search(self.config, self.state, lambda *a: {'error': 'secret response'}).query()

    def test_disabled_no_search(self):
        self.assertEqual(run(self.config, self.state, None, None, True), 'disabled')

    def test_unresolved_protection_blocks_activation(self):
        self.config['enabled'] = True
        with self.assertRaisesRegex(Failure, 'protection'): run(self.config, self.state, None, None, True)

    def test_explicit_no_blocks_with_explanation(self):
        self.config.update(enabled=True, allow_unknown_protection=False)
        with self.assertRaisesRegex(Failure, 'provider'): run(self.config, self.state, None, None, True)

    def test_no_delivered_receipts_blocks(self):
        self.config.update(enabled=True, allow_unknown_protection=True)
        with self.assertRaisesRegex(Failure, 'test delivery'): run(self.config, self.state, None, Delivery(self.state), True)

    def test_stop_before_provider(self):
        self.state.data['stopped'] = True
        self.assertEqual(run(self.config, self.state, None, None, False), 'stopped')

    def test_automatic_departure_stop(self):
        with patch('flight_watch.__main__.now_utc', return_value=datetime(2026,12,19,2,tzinfo=timezone.utc)):
            self.assertEqual(run(self.config, self.state, None, None, False), 'stopped')

    def test_partial_delivery_no_duplicate(self):
        calls = []
        def send(url, *a, **kw):
            calls.append(url)
            if 'resend' in url: return {'id': 'TEST-email'}
            raise Failure('timeout')
        alert = {'key':'test','subject':'test','body':'test','sms':'test','receipts':{}}
        self.state.data['alerts'].append(alert)
        d = Delivery(self.state, send)
        with self.assertRaises(Failure): d.send(alert)
        d.send(alert)
        self.assertEqual(len(calls), 2)
        self.assertEqual(alert['receipts']['sms']['status'], 'uncertain')
        self.assertFalse(delivered(alert))

    def test_both_delivered_required(self):
        alert = {'receipts': {'email': {'id':'TEST-email','status':'accepted'}, 'sms': {'id':'TEST-sms','status':'sent'}}}
        self.state.data['alerts'].append(alert)
        def get(url, **kw): return {'last_event':'delivered'} if 'resend' in url else {'status':'delivered'}
        Delivery(self.state, get).reconcile()
        self.assertTrue(delivered(alert))

    def test_realistic_round_trip_flow_and_refresh(self):
        out, ret = fixtures()
        self.config.update(enabled=True, allow_unknown_protection=True)
        self.state.data['alerts'] = [{'kind':'test','at':NOW.isoformat(),'receipts':{'email':{'status':'delivered'},'sms':{'status':'delivered'}}}]
        payloads = iter([response([out]), response([ret]), response([out]), response([ret])])
        s = Search(self.config, self.state, lambda *a: next(payloads))
        sent = []
        def transport(url, *a, **kw):
            sent.append(url)
            return {'id':'TEST-email'} if 'resend' in url else {'sid':'TEST-sms','status':'queued'}
        result = run(self.config, self.state, s, Delivery(self.state, transport), True)
        self.assertIn('submitted', result)
        self.assertEqual(s.used, 4)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.state.data['alerts'][-1]['price'], 1200)

    def test_empty_results_visible(self):
        with self.assertRaisesRegex(Failure, 'no usable'):
            run(self.config, self.state, Search(self.config,self.state,lambda *a:response([])), None,False)

    def test_failed_refresh_sends_nothing(self):
        out, ret = fixtures()
        self.config.update(enabled=True, allow_unknown_protection=True)
        self.state.data['alerts'] = [{'kind':'test','at':NOW.isoformat(),'receipts':{'email':{'status':'delivered'},'sms':{'status':'delivered'}}}]
        payloads=iter([response([out]),response([ret]),response([])])
        with self.assertRaisesRegex(Failure,'disappeared'):
            run(self.config,self.state,Search(self.config,self.state,lambda *a:next(payloads)),Delivery(self.state,lambda *a:self.fail()),True)


class HistoryAndScheduleTests(unittest.TestCase):
    def setUp(self):
        self.c = copy.deepcopy(CONFIG)
        self.out, self.ret = fixtures()
        self.fare = candidate(self.out, self.ret, self.c)

    def history(self, n=7, price=1600):
        return {'price_history': [[int((NOW-timedelta(days=i+1)).timestamp()), price] for i in range(n)]}

    def test_historical_immediate_comparison(self):
        text = reason(self.fare, [], [], self.c, NOW, self.history())
        self.assertIn('25%', text)
        self.assertIn('provider search-history', text)
        self.assertIn('unverified', text)

    def test_history_does_not_become_own_observations(self):
        from flight_watch.core import baseline_report
        observations = []
        report = baseline_report([self.fare], observations, self.history(), NOW)
        self.assertEqual(observations, [])
        self.assertEqual(report['candidates'][0]['own_comparable_days'], 0)
        self.assertEqual(report['historical']['days'], 7)

    def test_historical_daily_deduplication(self):
        from flight_watch.core import historical_baseline
        points = self.history(3)
        points['price_history'] += points['price_history'] * 20
        summary = historical_baseline(points, NOW)
        self.assertEqual(summary['days'], 3)
        self.assertEqual(summary['median_usd'], 1600)

    def test_bad_future_and_stale_history_ignored(self):
        from flight_watch.core import historical_baseline
        points = self.history(1)
        points['price_history'] += [[NOW.timestamp()+86400, 500], [NOW.timestamp()-86400*90, 500], [True, 1500], [NOW.timestamp(), float('nan')], ['bad', 500], [NOW.timestamp(), -1], None]
        result = historical_baseline(points, NOW)
        self.assertEqual(result['days'], 1)
        self.assertEqual(result['median_usd'], 1600)

    def test_insufficient_history_no_false_bargain(self):
        self.assertIn('NOT yet', reason(self.fare, [], [], self.c, NOW, self.history(2)))

    def test_old_history_no_bargain_alert(self):
        points = {'price_history': [[int((NOW-timedelta(days=i+10)).timestamp()),1600] for i in range(7)]}
        self.assertIn('NOT yet',reason(self.fare,[],[],self.c,NOW,points))

    def test_schedule_baseline_needs_no_messaging(self):
        from flight_watch.__main__ import scheduled
        with tempfile.TemporaryDirectory() as d:
            state=State(d)
            with patch('flight_watch.__main__.now_utc',return_value=NOW), patch('flight_watch.__main__.run',return_value='observed') as runner:
                scheduled(self.c,state,None,None)
                self.assertFalse(runner.call_args.args[-1])
                self.assertEqual(state.data['baseline_started_at'],NOW.isoformat())

    def test_schedule_transitions_to_daily(self):
        from flight_watch.__main__ import scheduled
        with tempfile.TemporaryDirectory() as d:
            state=State(d)
            state.data.update(baseline_started_at=(NOW-timedelta(days=4)).isoformat(),last_scheduled_observation_at=(NOW-timedelta(hours=8)).isoformat())
            with patch('flight_watch.__main__.now_utc',return_value=NOW), patch('flight_watch.__main__.run') as runner:
                self.assertIn('not due',scheduled(self.c,state,None,None))
                runner.assert_not_called()

    def test_initial_three_daily_checks(self):
        from flight_watch.__main__ import scheduled
        with tempfile.TemporaryDirectory() as d:
            state=State(d)
            state.data.update(baseline_started_at=(NOW-timedelta(days=1)).isoformat(),last_scheduled_observation_at=(NOW-timedelta(hours=8)).isoformat())
            with patch('flight_watch.__main__.now_utc',return_value=NOW), patch('flight_watch.__main__.run',return_value='observed') as runner:
                scheduled(self.c,state,None,None)
                runner.assert_called_once()

    def test_schedule_failure_does_not_start_baseline_clock(self):
        from flight_watch.__main__ import scheduled
        with tempfile.TemporaryDirectory() as d:
            state=State(d)
            with patch('flight_watch.__main__.run',side_effect=Failure('no key')):
                with self.assertRaises(Failure): scheduled(self.c,state,None,None)
            self.assertNotIn('baseline_started_at',state.data)


if __name__ == '__main__': unittest.main()
