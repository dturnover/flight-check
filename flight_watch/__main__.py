import argparse
import fcntl
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .core import baseline_report, candidate, digest, leg, message, reason, rows
from .runtime import Delivery, Failure, Search, State, delivered, now_utc


def validate(c):
    expected = {'origin': 'LAX', 'destination': 'BKK', 'outbound_after': '18:00', 'adults': 1,
                'currency': 'USD', 'market': 'us', 'cabin': 1}
    if any(c.get(k) != v for k, v in expected.items()):
        raise Failure('Fixed trip or comparison settings changed; review adapter before running.')
    allowed = {('2026-12-18', '2027-01-02'), ('2026-12-18', '2027-01-03'), ('2026-12-19', '2027-01-02'), ('2026-12-19', '2027-01-03')}
    if not c['date_options'] or any(tuple(d) not in allowed for d in c['date_options']):
        raise Failure('Date options changed; review adapter before running.')
    if c['allow_unknown_protection'] not in (None, True, False):
        raise Failure('Invalid protection preference')
    if not 1 <= c['outbound_choices'] <= 4 or c['max_api_calls_per_run'] > 7 or c['max_api_calls_per_rolling_31_days'] > 240:
        raise Failure('Quota settings exceed reviewed free-plan limits')


def collect(config, state, search):
    initial = search.query()
    rejected = Counter()
    usable = []
    for raw in rows(initial):
        try:
            leg(raw, config['origin'], config['destination'], config['outbound_date'], config, True)
            if raw.get('departure_token'):
                usable.append(raw)
            else:
                rejected['missing departure token'] += 1
        except (ValueError, KeyError, TypeError) as e:
            rejected[str(e) if isinstance(e, ValueError) else 'invalid outbound schema'] += 1
    usable.sort(key=lambda r: r.get('price', float('inf')))
    # Retain cheapest two; rotate remaining slots through broader results daily.
    n = config['outbound_choices']
    selected = usable[:min(2, n)]
    rest = usable[len(selected):]
    if rest:
        offset = len(state.data['runs']) % len(rest)
        selected += (rest[offset:] + rest[:offset])[:n - len(selected)]
    results = []
    for outbound in selected:
        returns = search.query(departure_token=outbound['departure_token'])
        for inbound in rows(returns):
            try:
                fare = candidate(outbound, inbound, config)
                results.append((fare, outbound))
            except (ValueError, KeyError, TypeError) as e:
                rejected[str(e) if isinstance(e, ValueError) else 'invalid return schema'] += 1
    state.data['last_search'] = {'at': now_utc().isoformat(), 'dates': [config['outbound_date'], config['return_date']], 'outbounds_found': len(rows(initial)),
        'outbounds_usable': len(usable), 'outbounds_expanded': len(selected),
        'complete_candidates': len(results), 'rejections': dict(rejected),
        # Provider insights are contextual, not our baseline or exact-itinerary history.
        'provider_price_insights': initial.get('price_insights', {}), 'protection': 'unknown'}
    state.data['last_candidates'] = [dict(fare, observed_at=now_utc().isoformat()) for fare, _ in sorted(results, key=lambda pair: pair[0]['price'])[:20]]
    state.save()
    return sorted(results, key=lambda pair: pair[0]['price'])


def run(config, state, search, delivery, notify):
    now = now_utc()
    if state.data['stopped'] or now >= datetime(2026, 12, 18, 18, tzinfo=ZoneInfo('America/Los_Angeles')):
        return 'stopped'
    if notify:
        if not config['enabled']:
            return 'disabled'
        if config['allow_unknown_protection'] is not True:
            raise Failure('Activation blocked: provider cannot verify protection. Resolve manual-review preference or choose a provider with explicit protection evidence.')
        delivery.require()
        delivery.reconcile()
        if not any(a.get('kind') == 'test' and delivered(a) for a in state.data['alerts']):
            raise Failure('Activation blocked: both test delivery receipts must say delivered.')
        # Finish the untouched channel after a crash; never resend the uncertain one.
        for a in state.data['alerts']:
            if a.get('kind') != 'test':
                delivery.send(a)
        if any(r.get('status') in ('uncertain', 'attempting', 'failed', 'undelivered', 'bounced', 'complained') for a in state.data['alerts'] for r in a.get('receipts', {}).values()):
            raise Failure('A delivery needs operator attention; inspect private receipts.')
    # One date combination per run keeps each run within the per-run search budget.
    i = state.data.get('date_option_index', 0)
    config = dict(config, outbound_date=config['date_options'][i % len(config['date_options'])][0],
                  return_date=config['date_options'][i % len(config['date_options'])][1])
    search.config = config
    state.data['date_option_index'] = i + 1
    results = collect(config, state, search)
    prior = list(state.data['observations'])
    for fare, _ in results:
        state.data['observations'].append({k: fare[k] for k in ['id', 'cohort', 'price']} | {'at': now.isoformat()})
    state.data['observations'] = [o for o in state.data['observations'] if datetime.fromisoformat(o['at']) >= now - timedelta(days=45)]
    state.save()
    if not results:
        raise Failure('Search completed but found no usable complete round trips; inspect private rejection counts. Route coverage is unverified.')
    state.data['baseline_report'] = baseline_report([f for f, _ in results], state.data['observations'],
        state.data['last_search']['provider_price_insights'], now)
    state.save()
    if not notify:
        return f"observed {len(results)} complete candidates; no notifications requested"
    alerts = [a for a in state.data['alerts'] if datetime.fromisoformat(a['at']) >= now - timedelta(days=31)]
    if len(alerts) >= config['max_alerts_per_rolling_31_days']:
        raise Failure('Notification budget reached; observations saved but further alerts paused.')
    for fare, outbound in results:
        why = reason(fare, prior, state.data['alerts'], config, now, state.data['last_search']['provider_price_insights'])
        if not why:
            continue
        # Two fresh requests: regenerate the outbound token, then reselect this return.
        refreshed_out = search.query(no_cache='true')
        refreshed = None
        for o in rows(refreshed_out):
            try:
                if leg(o, config['origin'], config['destination'], config['outbound_date'], config, True)['segments'] == fare['outbound']['segments'] and o.get('departure_token'):
                    returning = search.query(departure_token=o['departure_token'], no_cache='true')
                    for r in rows(returning):
                        try:
                            new = candidate(o, r, config)
                            if new['id'] == fare['id'] and new['cohort'] == fare['cohort']:
                                refreshed = new
                                break
                        except (ValueError, KeyError, TypeError):
                            continue
                    break
            except (ValueError, KeyError, TypeError):
                continue
        if not refreshed:
            raise Failure('Promising fare disappeared during refresh; no alert sent.')
        why = reason(refreshed, prior, state.data['alerts'], config, now, refreshed_out.get('price_insights', {}))
        if not why:
            return 'price changed on refresh; no longer qualifies'
        fare = refreshed
        alert = {'kind': 'fare', 'key': digest([fare['id'], fare['price'], now.isoformat()]),
                 'id': fare['id'], 'cohort': fare['cohort'], 'price': fare['price'], 'at': now.isoformat(),
                 'subject': f"Flight Watch: ${fare['price']:.0f} round trip (manual review)",
                 'body': message(fare, why, config),
                 'sms': f"Flight Watch: USD {fare['price']:.0f} total round trip, 1 adult {config['origin']}-{config['destination']} {config['outbound_date']} / {config['return_date']}. Protection UNKNOWN: manual review. Full itinerary, baggage caveats and search link in your email. " + why,
                 'receipts': {}}
        # Force short GSM-compatible SMS to bound segment costs (max 3 segments).
        alert['sms'] = alert['sms'].encode('ascii', 'replace').decode()[:450]
        state.data['alerts'].append(alert)
        state.save()
        delivery.send(alert)
        return 'alert submitted; delivery confirmation pending'
    return 'observations saved; no qualifying deal'


def scheduled(config, state, search, delivery):
    """Three checks/day for the first 72 hours, then roughly daily; no messaging dependency."""
    now = now_utc()
    if not config.get('baseline_collection_enabled', False):
        return 'baseline collection disabled'
    started = state.data.get('baseline_started_at')
    collecting = started is None or now - datetime.fromisoformat(started) < timedelta(hours=72)
    last = state.data.get('last_scheduled_observation_at')
    interval = timedelta(hours=7 if collecting else 11)
    if last and now - datetime.fromisoformat(last) < interval:
        return 'next scheduled observation not due'
    # Baseline collection must work before messaging setup or protection consent.
    # Paid messaging still requires its independent gates when explicitly enabled.
    status = run(config, state, search, delivery, bool(config['enabled']))
    if status not in ('stopped', 'disabled'):
        state.data.setdefault('baseline_started_at', now.isoformat())
        state.data['last_scheduled_observation_at'] = now.isoformat()
        state.save()
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['observe', 'monitor', 'scheduled', 'baseline', 'test-delivery', 'receipts', 'health', 'stop', 'probe'])
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--state-dir', default='state')
    parser.add_argument('--persist-git', action='store_true')
    args = parser.parse_args()
    Path(args.state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(args.state_dir) / '.run.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another local run is active')
        state = State(args.state_dir, args.persist_git)
        config = json.loads(Path(args.config).read_text())
        validate(config)
        delivery = Delivery(state)
        at = now_utc().isoformat()
        try:
            if args.command == 'stop':
                state.data['stopped'] = True
                status = 'stopped'
            elif args.command == 'health':
                # Counts/status only, never delivery IDs, addresses, raw provider payloads.
                last = state.data['runs'][-1] if state.data['runs'] else {}
                print(json.dumps({'last_run': last, 'stopped': state.data['stopped'], 'observations': len(state.data['observations']), 'search_calls': len(state.data['calls'])}))
                return
            elif args.command == 'baseline':
                report = state.data.get('baseline_report')
                if not report:
                    raise Failure('No baseline yet: a successful live observe or scheduled run is required.')
                print(json.dumps(report, indent=2))
                return
            elif args.command == 'probe':
                # Diagnose empty route coverage: result counts only, through the same quota.
                search = Search(config, state)
                variants = {'LAX-URT': {'arrival_id': 'URT'}, 'BKK-URT': {'departure_id': 'BKK', 'arrival_id': 'URT'}}
                for name, extra in variants.items():
                    try:
                        r = search.query(**extra)
                        print(name, len(r.get('best_flights', [])) + len(r.get('other_flights', [])), 'results')
                    except Failure as exc:
                        print(name, exc)
                status = 'probed'
            elif args.command == 'scheduled':
                status = scheduled(config, state, Search(config, state), delivery)
            elif args.command == 'receipts':
                delivery.require()
                delivery.reconcile()
                status = 'all deliveries confirmed' if state.data['alerts'] and all(delivered(a) for a in state.data['alerts']) else 'delivery not yet confirmed'
                if status != 'all deliveries confirmed':
                    raise Failure(status)
            elif args.command == 'test-delivery':
                delivery.require()
                existing = next((a for a in state.data['alerts'] if a.get('kind') == 'test'), None)
                if not existing:
                    existing = {'kind': 'test', 'key': digest(['test', at]), 'at': at, 'subject': 'Flight Watch delivery test',
                                'body': 'Flight Watch test only. No flight price is being quoted; monitoring is not activated.',
                                'sms': 'Flight Watch delivery test. No fare is quoted. Monitoring is not activated.', 'receipts': {}}
                    state.data['alerts'].append(existing)
                    state.save()
                delivery.send(existing)
                delivery.reconcile()
                status = 'both tests delivered' if delivered(existing) else 'test accepted; delivery pending (run receipts)'
            else:
                status = run(config, state, Search(config, state), delivery, args.command == 'monitor')
            state.data['runs'].append({'at': at, 'command': args.command, 'status': status})
            state.save()
            print(status)
        except Failure as exc:
            state.data['runs'].append({'at': at, 'command': args.command, 'status': 'failed', 'reason': str(exc)})
            state.save()
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        except Exception:
            # Fail closed without leaking provider response or credentials in tracebacks.
            print('Unexpected failure; inspect code and private state. No raw provider data logged.', file=sys.stderr)
            raise SystemExit(1)


if __name__ == '__main__':
    main()
