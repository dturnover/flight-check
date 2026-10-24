"""Pure fare normalization and explainable comparison logic."""
import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


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


def leg(raw, origin, destination, day, config, outbound=False):
    flights = raw.get('flights', [])
    if not flights or unsafe(raw):
        raise ValueError('missing segments or known unprotected transfer')
    if flights[0]['departure_airport']['id'] != origin or flights[-1]['arrival_airport']['id'] != destination:
        raise ValueError('wrong route')
    departure = datetime.fromisoformat(flights[0]['departure_airport']['time'])
    if departure.date().isoformat() != day:
        raise ValueError('wrong departure date')
    if outbound and departure.strftime('%H:%M') < config['outbound_after']:
        raise ValueError('outbound before local cutoff')
    duration = raw.get('total_duration', 0)
    if not 0 < duration <= config['max_leg_hours'] * 60:
        raise ValueError('missing or excessive duration')
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
    cohort = digest({'stops': [len(x['layovers']) for x in [out, ret]],
                     'duration_6h_bands': [int(x['minutes'] // 360) for x in [out, ret]],
                     'overnights': [sum(l['overnight'] for l in x['layovers']) for x in [out, ret]],
                     'bags': bags, 'protection': 'unknown', 'currency': config['currency']})
    query = f"Round trip {config['origin']} to {config['destination']} {config['outbound_date']} after {config['outbound_after']} return {config['return_date']} 1 adult economy"
    return {'id': digest(identity), 'cohort': cohort, 'price': float(price), 'outbound': out, 'return': ret,
            'protection': 'unknown', 'baggage': bags, 'booking_token': inbound.get('booking_token'),
            'search_link': 'https://www.google.com/travel/flights?' + urlencode({'q': query, 'curr': config['currency'], 'hl': 'en'}),
            'seller': 'Not verified; compare airline-direct offers and check ticket terms before paying.'}


def reason(fare, observations, alerts, config, now):
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
