"""Pure fare normalization and explainable comparison logic."""
import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

WORK_TZ = ZoneInfo('America/Los_Angeles')
AIRPORT_TZ = {'LAX': 'America/Los_Angeles', 'BKK': 'Asia/Bangkok'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def rows(response):
    return response.get('best_flights', []) + response.get('other_flights', [])


def unsafe(value):
    if isinstance(value, dict):
        if any(value.get(k) for k in ('self_transfer', 'separate_tickets', 'airport_change')):
            return True
        return any(unsafe(v) for v in value.values())
    if isinstance(value, list):
        return any(unsafe(v) for v in value)
    if isinstance(value, str):
        return any(s in value.lower() for s in ('self-transfer', 'self transfer', 'separate tickets', 'separate ticket', 'airport change'))
    return False


def off_work(local, airport, config):
    """Outside the Mon-Fri Los Angeles workweek; Friday ends at outbound_after."""
    if airport not in AIRPORT_TZ:
        raise ValueError('unknown airport timezone')
    t = local.replace(tzinfo=ZoneInfo(AIRPORT_TZ[airport])).astimezone(WORK_TZ)
    return t.weekday() >= 5 or (t.weekday() == 4 and t.strftime('%H:%M') >= config['outbound_after'])


def leg(raw, origin, destination, day, config, outbound=False):
    flights = raw.get('flights', [])
    if not flights or unsafe(raw):
        raise ValueError('missing segments or known unprotected transfer')
    if flights[0]['departure_airport']['id'] != origin or flights[-1]['arrival_airport']['id'] != destination:
        raise ValueError('wrong route')
    departure = datetime.fromisoformat(flights[0]['departure_airport']['time'])
    if departure.date().isoformat() != day:
        raise ValueError('wrong departure date')
    duration = raw.get('total_duration', 0)
    if not 0 < duration <= config['max_leg_hours'] * 60:
        raise ValueError('missing or excessive duration')
    # Weekend windows are 114 hours apart, so two off-work ends of a <=60h leg share one weekend.
    arrival = datetime.fromisoformat(flights[-1]['arrival_airport']['time'])
    if not (off_work(departure, origin, config) and off_work(arrival, destination, config)):
        raise ValueError('travel during the workweek')
    layovers = []
    for prev, nxt in zip(flights, flights[1:]):
        a, b = prev['arrival_airport'], nxt['departure_airport']
        if a['id'] != b['id']:
            raise ValueError('airport change')
        # The two timestamps are at the same airport, hence same local timezone.
        minutes = (datetime.fromisoformat(b['time']) - datetime.fromisoformat(a['time'])).total_seconds() / 60
        if minutes < config['minimum_connection_minutes']:
            raise ValueError('connection below configured minimum')
        layovers.append({'airport': a['id'], 'minutes': minutes,
                         'overnight': a['time'][:10] != b['time'][:10]})
    segments = [{k: f.get(k) for k in ('departure_airport', 'arrival_airport', 'airline', 'flight_number', 'duration')} for f in flights]
    notes = [str(x) for obj in [raw] + flights for x in obj.get('extensions', []) if 'bag' in str(x).lower()]
    return {'segments': segments, 'minutes': duration, 'layovers': layovers, 'baggage_notes': sorted(set(notes))}


def candidate(outbound, inbound, config):
    # Returning selection price is the TOTAL round trip, never add outbound price.
    if inbound.get('type', '').lower() != 'round trip':
        raise ValueError('provider did not label price round trip')
    price = inbound.get('price')
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not 0 < price < 100000:
        raise ValueError('invalid price')
    out = leg(outbound, config['origin'], config['destination'], config['outbound_date'], config, True)
    ret = leg(inbound, config['destination'], config['origin'], config['return_date'], config)
    identity = [[(s['flight_number'], s['departure_airport']['id'], s['departure_airport']['time'], s['arrival_airport']['id'], s['arrival_airport']['time']) for s in x['segments']] for x in [out, ret]]
    bags = [out['baggage_notes'], ret['baggage_notes']]
    # SerpApi has no documented contractual protection evidence: always unknown.
    # No inference from airlines, a single result, or absence of a warning.
    cohort = digest({'dates': [config['outbound_date'], config['return_date']], 'stops': [len(x['layovers']) for x in [out, ret]],
                     'duration_6h_bands': [int(x['minutes'] // 360) for x in [out, ret]],
                     'overnights': [sum(l['overnight'] for l in x['layovers']) for x in [out, ret]],
                     'bags': bags, 'protection': 'unknown', 'currency': config['currency']})
    query = f"Round trip {config['origin']} to {config['destination']} {config['outbound_date']} return {config['return_date']} 1 adult economy"
    return {'id': digest(identity), 'cohort': cohort, 'price': float(price), 'outbound': out, 'return': ret,
            'protection': 'unknown', 'baggage': bags, 'booking_token': inbound.get('booking_token'),
            'search_link': 'https://www.google.com/travel/flights?' + urlencode({'q': query, 'curr': config['currency'], 'hl': 'en'}),
            'seller': 'Not verified; compare airline-direct offers and check ticket terms before paying.'}


def historical_baseline(insights, now):
    """Provider search-level history, never claimed as comparable-ticket history."""
    daily = {}
    if not isinstance(insights, dict):
        insights = {}
    for point in insights.get('price_history', []) or []:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        stamp, price = point
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (stamp, price)) or price <= 0:
            continue
        try:
            date = datetime.fromtimestamp(stamp, timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if not now - timedelta(days=30) <= date <= now:
            continue
        day = date.date().isoformat()
        daily[day] = min(daily.get(day, float('inf')), price)
    typical = insights.get('typical_price_range')
    if not (isinstance(typical, list) and len(typical) == 2
            and all(isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and p > 0 for p in typical)
            and typical[0] <= typical[1]):
        typical = None
    return {'source': 'Google Flights via SerpApi', 'scope': 'provider search-level history; ticket quality, baggage and protection comparability unverified',
            'days': len(daily), 'median_usd': statistics.median(daily.values()) if daily else None,
            'lowest_usd': min(daily.values()) if daily else None,
            'first_date': min(daily) if daily else None, 'last_date': max(daily) if daily else None,
            'typical_price_range_usd': typical}


def baseline_report(candidates, observations, insights, now):
    history = historical_baseline(insights, now)
    report = {'generated_at': now.isoformat(), 'historical': history, 'candidates': []}
    for fare in candidates[:20]:
        daily = {}
        for o in observations:
            if o['cohort'] == fare['cohort'] and now - timedelta(days=30) <= datetime.fromisoformat(o['at']) <= now:
                day = o['at'][:10]
                daily[day] = min(daily.get(day, float('inf')), o['price'])
        report['candidates'].append({'id': fare['id'], 'total_round_trip_usd': fare['price'],
            'own_comparable_days': len(daily),
            'own_comparable_median_usd': statistics.median(daily.values()) if daily else None,
            'percent_below_provider_history': round(100 * (1 - fare['price'] / history['median_usd']), 1) if history['median_usd'] else None,
            'protection': fare['protection']})
    return report


def reason(fare, observations, alerts, config, now, insights=None):
    relevant = [o for o in observations if o['cohort'] == fare['cohort'] and datetime.fromisoformat(o['at']) >= now - timedelta(days=30)]
    prior_alerts = [a for a in alerts if a.get('kind') != 'test']
    same = [a for a in prior_alerts if a['cohort'] == fare['cohort']]
    if same:
        last = same[-1]
        saving = last['price'] - fare['price']
        if saving < max(config['minimum_saving_usd'], last['price'] * config['realert_discount']):
            return None
    if prior_alerts and now - datetime.fromisoformat(prior_alerts[-1]['at']) < timedelta(hours=config['cooldown_hours']):
        return None
    daily = {}
    for o in relevant:
        day = o['at'][:10]
        daily[day] = min(daily.get(day, float('inf')), o['price'])
    if len(daily) >= config['baseline_min_days']:
        baseline = statistics.median(daily.values())
        if baseline - fare['price'] >= max(config['minimum_saving_usd'], baseline * config['baseline_discount']):
            return f"{(1-fare['price']/baseline)*100:.0f}% below the median of {len(daily)} comparable daily minima (${baseline:.0f})."
    previous = [o for o in relevant if o['id'] == fare['id']]
    if previous:
        p = previous[-1]['price']
        if p - fare['price'] >= max(config['minimum_saving_usd'], p * config['drop_discount']):
            return f"Same itinerary dropped from ${p:.0f} to ${fare['price']:.0f}."
    history = historical_baseline(insights, now)
    if history['days'] >= 3 and history['last_date'] >= (now - timedelta(days=7)).date().isoformat():
        baseline = history['median_usd']
        if baseline - fare['price'] >= max(config['minimum_saving_usd'], baseline * config['baseline_discount']):
            return (f"{(1-fare['price']/baseline)*100:.0f}% below provider search-history median (${baseline:.0f}, {history['days']} days). "
                    'Provisional historical comparison: matching baggage, itinerary quality and ticket protection are unverified; this is not the bot\'s own baseline.')
    if not prior_alerts:
        return 'First observed usable candidate for manual review; baseline is collecting. This is NOT yet evidence of an unusually cheap fare.'
    return None


def message(fare, why, config):
    lines = [f"Flight Watch: USD {fare['price']:.2f} TOTAL round trip, 1 adult, economy.", why,
             'MANUAL REVIEW: missed-connection protection UNKNOWN. Confirm a single ticket and airline rebooking terms.',
             'Times below are local to each airport. Fare and availability may change.']
    for key in ('outbound', 'return'):
        item = fare[key]
        lines.append(f"{key.title()}: {item['minutes']/60:.1f} hours")
        for s in item['segments']:
            a, b = s['departure_airport'], s['arrival_airport']
            lines.append(f"{s['airline']} {s['flight_number']}: {a['id']} {a['time']} -> {b['id']} {b['time']}")
        for l in item['layovers']:
            lines.append(f"Layover {l['airport']}: {l['minutes']/60:.1f} hours" + (' (overnight)' if l['overnight'] else ''))
        lines.append('Baggage: ' + ('; '.join(item['baggage_notes']) or 'Unknown; checked/carry-on allowance and fees not verified.'))
    lines += [fare['seller'], 'Search (reselect exact itinerary and confirm baggage/price): ' + fare['search_link'],
              'No booking or payment is performed by Flight Watch.']
    return '\n'.join(lines)
