# Provider research and cost estimate

Official documentation retrieved during implementation on October 24, 2026. Published prices may change. No live fare has been obtained; no price from documentation or tests is a quote for this trip.

## Flight data

[SerpApi Google Flights API](https://serpapi.com/google-flights-api) documents a round-trip search followed by a `departure_token` request to obtain return choices; `booking_token` can retrieve booking options. We use the return-selection total and refresh by repeating searches without cache. The route must be verified with a real API key. Airport-code support is not evidence of available LAX–URT itineraries. Google Flights may omit airlines/offers. Coverage of the required route and single-ticket options is unconfirmed.

[SerpApi pricing](https://serpapi.com/pricing): free 250 searches/month; Starter $25/month for 1,000. Default schedule uses at most 7 calls/day × 31 = 217: initial search, up to four outbound expansions, two refreshes. A rolling 31-day cap of 240 leaves a small reserve, but manual observations also consume it. Use a dedicated free key/account; other consumers are not counted locally. All attempted calls count locally even if the provider does not bill them. No auto-upgrade or extra credits.

No contractual missed-connection protection guarantee was found in the documented schema. All recommendations require explicit user acceptance of unknown protection and manual ticket review. Known separate tickets/self-transfers are rejected. A booking link alone would not establish protection.

Duffel's pricing page returned HTTP 403 in this environment, so current Duffel pricing and route coverage were not verified. It is not implemented. We have not established that an airline-distribution API covers URT better; provider switching needs live evidence.

## Hosting and private state

GitHub Actions in a private companion repository; approximately 31 scheduled jobs/month, bounded by a 20-minute timeout (620 minutes maximum scheduled allocation, usually much less). [GitHub billing documentation](https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions) was not reachable from this environment. GitHub Free normally includes 2,000 private-repository minutes/month, shared with the account's other jobs. Verify remaining allowance and set the account Actions spending limit to $0. A workflow timeout is a maximum, not a runtime measurement. Small private Git state replaces ephemeral caches/artifacts as durable history.

Schedule: **14:17 UTC daily** (07:17 PDT / 06:17 PST). Up to 24 hours between scheduled checks, plus provider and GitHub queue delay. GitHub schedules are best effort and can be delayed or dropped; there is no instant-alert or uptime guarantee. Inspect Actions failures and last successful observation. An external heartbeat service would be required to detect a scheduler that never launches; none is configured.

## Email and SMS

[Resend pricing](https://resend.com/pricing): free 3,000 emails/month, 100/day. This workload is well below that. A verified sending domain is needed for arbitrary recipients; an existing domain has no new registration cost. Resend's testing sender is restricted to the account owner's address. Domain purchase is not included or authorized. Disable any paid overage features.

[Twilio US SMS pricing](https://www.twilio.com/en-us/sms/pricing/us): $0.0083/outbound segment, plus carrier fees (up to $0.005 in the published US table). A toll-free sender is $2.15/month; a local number is $1.15/month but US A2P 10DLC registration/campaign fees may apply. Toll-free verification is required for US messaging. No number has been purchased and no registration submitted.

**Illustrative US toll-free budget:** 20 notifications × up to 3 SMS segments × ($0.0083 + $0.005) = **$0.80/month** messaging, plus **$2.15/month** number = **$2.95/month before taxes**, with flight data, hosting and email at $0 under the limits above. Trial prefixes/terms may affect segment counts. Reserve roughly $4/month plus applicable taxes; this is an estimate, not a spending guarantee. Sending-number availability, destination country, account eligibility and any verification fees must be confirmed before approval. Reusing an existing sender avoids a new rental. Non-US recipients require a new estimate. No paid service is activated by this code.

Confirm actual Twilio charges and approve messaging costs before setting `MESSAGING_COST_APPROVED=true`. Do not enable paid renewal, buy a number, or subscribe as part of deployment without that approval.

## Privacy

Searches originate on GitHub-hosted runners, use fixed US market/USD/English settings, and use no personal browser session or airline login. This avoids the home IP for scheduled searches; it does not promise anonymity or lower fares. Recipients and credentials belong in private repository Actions secrets. History, message bodies, receipt IDs and delivery status belong only in the private state branch. Logs contain sanitized health summaries and counts; no artifacts are uploaded.
