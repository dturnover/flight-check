# Flight Watch

Fixed-trip monitoring for **LAX → URT**, one adult, economy, departing **December 18, 2026 at or after 18:00 Los Angeles time**, returning from URT **January 3, 2027**. Never books or pays for tickets.

**Status:** implementation tested with explicitly synthetic fixtures. Monitoring is disabled. No live fares, route coverage, or actual email/SMS delivery have yet been verified. See [OPERATIONS.md](OPERATIONS.md) for deployment and activation gates and [RESEARCH.md](RESEARCH.md) for costs and provider limitations.

## Run locally

Python 3.12+, standard library only; no package installation required.

```sh
python -m unittest discover -s tests -v
python -m flight_watch health --state-dir /tmp/flight-watch-state
# Requires SERPAPI_KEY in the environment; makes real API requests:
python -m flight_watch observe --state-dir /tmp/flight-watch-state
```

Do not put keys in command arguments or source files. Store operational state only in a private location. Local state is not a cloud deployment.

## Behavior

* Search complete round trips using SerpApi's documented outbound-selection/return-selection flow. The selected return's price is the total round-trip price; outbound and return totals are never added.
* Examine all returned inbound choices for up to four outbound choices. Keep the cheapest two outbounds and rotate two other choices over successive runs. This is bounded sampling, not exhaustive coverage.
* Exclude explicitly separate tickets/self-transfers, airport changes, connections under two hours, legs over 60 hours, and departures outside the exact dates/time. Count every rejection in private state to diagnose filter effects. These quality defaults are configurable; their impact on URT coverage needs a live search.
* Protection is **unknown** for this adapter, never inferred. Activation requires an explicit decision to accept candidates for manual review. If the user declines, a different provider with verifiable ticket terms is required before activation.
* Compare daily cheapest observations within cohorts defined by stop count, six-hour duration bands, overnight waits, baggage disclosures and protection. Unknown baggage fees remain unknown; prices are not falsely normalized to include bags.
* After seven observed days in a cohort, alert for at least 15% and $75 below its 30-day median daily minimum; also recognize a 10%/$75 drop for the same itinerary. A first candidate provides a clearly labeled baseline-starting update, not a claimed bargain. Provider historical insights are stored separately and are not treated as this bot's measurements.
* Refresh both selected legs before sending. Re-alert only for an 8%/$75 improvement in that quality cohort, with a global 48-hour cooldown. At most one new alert per run, at most 20 alerts (including tests) per rolling 31 days.
* Email contains full itinerary, local times, layovers, baggage caveats, unknown protection, seller caveat, and a trip search link. SMS is a short summary pointing to the detailed email. Search links require reselecting and checking the exact itinerary; no seller or ticket terms are certified.

## Cloud deployment

Public source: `dturnover/flight-check`. Deploy the same code to a **private companion repository**, proposed name `dturnover/flight-watch-runtime`. The workflow deliberately refuses to execute in a public repository. A private `state` branch persists observations, API reservations, health, and notification receipts. No public Actions artifacts contain state. No separate database subscription or cross-repository state token is required.

Every side-effect reservation is committed and pushed before its request. A failed push stops the run. GitHub Actions concurrency prevents overlap; a local lock protects local runs. Do not run a second scheduler against the same state. API failures stop the run with a failed Actions status; they are never reported as “no deal.” Uncertain SMS sends are not blindly retried. See the operations guide for recovery.
