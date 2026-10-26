# Flight Watch

Fixed-trip monitoring for **LAX ↔ BKK**, one adult, economy, departing **December 18, 2026 at or after 18:00 Los Angeles time**, returning from BKK **January 3, 2027**. Google Flights sells no single ticket to URT, so the BKK ↔ URT hop is booked separately; `probe` checks both. Never books or pays for tickets.

**Status:** implementation tested with explicitly synthetic fixtures. Monitoring is disabled. No live fares, route coverage, or actual email/SMS delivery have yet been verified. The repository visibility and a flight API key still block automatic collection. See [OPERATIONS.md](OPERATIONS.md) for deployment and activation gates and [RESEARCH.md](RESEARCH.md) for costs and provider limitations.

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
* Exclude explicitly separate tickets/self-transfers, airport changes, connections under two hours, legs over 60 hours, and departures outside the exact dates/time. Count every rejection in private state to diagnose filter effects. These quality defaults are configurable; check `last_search.rejections` after live runs.
* Protection is **unknown** for this adapter, never inferred. Activation requires an explicit decision to accept candidates for manual review. If the user declines, a different provider with verifiable ticket terms is required before activation.
* Compare daily cheapest observations within cohorts defined by stop count, six-hour duration bands, overnight waits, baggage disclosures and protection. Unknown baggage fees remain unknown; prices are not falsely normalized to include bags.
* Provider historical search prices, when returned, are summarized immediately as a separate provisional baseline. With at least three recent historical days, a 15%/$75 discount can qualify for manual review, with an explicit warning that baggage, itinerary quality and protection may differ. Missing history is reported honestly; no past fares are invented.
* After seven observed days in a cohort, alert for at least 15% and $75 below its 30-day median daily minimum; also recognize a 10%/$75 drop for the same itinerary. A first candidate provides a clearly labeled baseline-starting update, not a claimed bargain. Historical insights remain separate from this bot's measurements.
* Refresh both selected legs before sending. Re-alert only for an 8%/$75 improvement in that quality cohort, with a global 48-hour cooldown. At most one new alert per run, at most 20 alerts (including tests) per rolling 31 days.
* Email contains full itinerary, local times, layovers, baggage caveats, unknown protection, seller caveat, and a trip search link. SMS is a short summary pointing to the detailed email. Search links require reselecting and checking the exact itinerary; no seller or ticket terms are certified.

## Cloud deployment

Use the existing **`dturnover/flight-check`** repository; no second repository is required. It must be private before the runtime job starts because a private `state` branch holds observations and, once alerts are configured, delivery records. The workflow refuses to run in a public repository. GitHub rejected the agent's attempt to change visibility; the owner needs to change it in Settings.

Once private and supplied with `SERPAPI_KEY`, scheduled baseline collection runs without any email/SMS credentials or protection decision. It checks at 06:17, 14:17 and 22:17 UTC for the first 72 hours after its first successful scheduled observation, then roughly daily. A rolling 31-day cap of 240 attempted search requests applies to manual and scheduled work combined. Alerts remain disabled until their separate requirements are met. The **baseline** workflow command shows provider historical figures and the bot's own cohort statistics separately; latest full itineraries are in private `state.json` under `last_candidates`.

Every side-effect reservation is committed and pushed before its request. A failed push stops the run. GitHub Actions concurrency prevents overlap; a local lock protects local runs. Do not run a second scheduler against the same state. API failures stop the run with a failed Actions status; they are never reported as “no deal.” Uncertain SMS sends are not blindly retried. See the operations guide for recovery.
