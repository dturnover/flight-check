# Deploy and operate Flight Watch

## Current gates

No live search or real email/SMS send has succeeded. All tests are synthetic. The available GitHub integration denied `createRepository`, Actions permission inspection and Actions secret listing with “Resource not accessible by integration.” Git read and basic repository metadata access work. The unauthenticated live SerpApi endpoint returns HTTP 401. There are no configured flight/messaging credentials in this machine. No monitoring is active.

## One-time deployment

1. Create `dturnover/flight-watch-runtime` as a **private**, empty GitHub repository. Grant the existing GitHub connection access to it and to `dturnover/flight-check`, including Contents read/write and Workflows write. For the agent to configure and run Actions, the connection also needs Actions read/write, Secrets read/write, and Variables read/write. If the connection cannot grant those permissions, perform the following setup directly in GitHub; do not repeat unrelated account setup or paste a token into chat.
2. Push the tested source to the private repository's `main` branch. Public `dturnover/flight-check` holds source only. Do not publish, fork publicly, upload, or mirror the private runtime's `state` branch.
3. Ensure Actions is enabled and its workflow token can write repository contents (the workflow requests only Contents write). Set the account Actions spending limit to $0 and confirm enough included minutes remain. The private state branch is initialized automatically and requires no extra PAT or database.
4. Configure a dedicated SerpApi **free** account/key. In the **private repository → Settings → Secrets and variables → Actions**, enter the following secrets securely:

   | Secret | Purpose |
   | --- | --- |
   | `SERPAPI_KEY` | Live Google Flights API |
   | `RESEND_API_KEY` | Email send and retrieve-delivery access |
   | `ALERT_FROM` | Verified sending address; or provider testing sender under its restrictions |
   | `ALERT_EMAIL` | Your destination email |
   | `TWILIO_ACCOUNT_SID` | Messaging account |
   | `TWILIO_AUTH_TOKEN` | Messaging authentication |
   | `TWILIO_FROM` | Existing or approved, verified SMS sender, E.164 |
   | `ALERT_PHONE` | Destination mobile number, E.164 |

   Do not use public repository variables for recipients. Do not print secret values. Confirm which sender/recipient countries and Twilio registration rules apply before approving costs. Trial messaging may be limited to verified recipients and require additional setup.
5. Set Actions variables `ALLOW_UNKNOWN_PROTECTION=true` only if you explicitly agree to manually review candidates whose protection cannot be established. `false` or unset blocks activation with a visible explanation. The current adapter cannot satisfy a protected-only requirement. Set `MESSAGING_COST_APPROVED=true` only after approving the actual messaging charges. Leave `FLIGHT_WATCH_ENABLED` unset during setup.
6. In the private repo's Actions → Flight Watch → Run workflow, choose **observe**. Inspect the private `state` branch's `state.json`: `last_search` contains breadth/rejection counts and provider insights; `observations` holds complete-trip prices. Verify the selected route/time and whether filters remove all results. A successful empty response is a failed coverage check, not a successful launch. No automatic destination substitution is allowed.
7. Choose **test-delivery**, then **receipts**. “Accepted,” “queued,” or “sent” is not confirmation of delivery. Both channels must report **delivered**; email delivery means the receiving mail server accepted the message, not that a human read it. Confirm receipt with the user too. The workflow blocks monitoring without both provider delivery receipts. Resend must grant retrieval access; Twilio status can take time to settle.
8. Only after the live route and filter review, protection decision, approved costs, and both deliveries succeed, set `FLIGHT_WATCH_ENABLED=true`. Dispatch **monitor** once and verify its outcome. Then verify the next scheduled run starts and persists observations. The cron is daily 14:17 UTC, best effort. Monitoring is not deployed/verified merely because code was pushed.

## Health, controls, and recovery

* Private repository Actions shows successful and failed runs. Configure GitHub's own Actions failure notifications in your account. The **health** command prints last run, observation count, stop status and reserved API count; inspect the timestamp, not only a green job. No independent external heartbeat is installed, so a scheduler outage is not automatically detected.
* Use **stop** to persistently stop searches/alerts after purchase. Also set `FLIGHT_WATCH_ENABLED=false` or disable the workflow to stop scheduled execution. A stopped bot does not make flight API requests. Automatic stop is December 18, 2026 at 18:00 America/Los_Angeles. `receipts` and `health` remain available. Do not reset `stopped` without an intentional restart review.
* Edit `config.json` in the private runtime repository for quality and alert defaults. Changes to fixed trip details require review of adapter validation too. Do not silently substitute airports. Evaluate `last_search.rejections` before tightening filters. Two-hour connections are only a usability filter, not a guarantee of rebooking protection.
* HTTP 429/outage: the run fails visibly and keeps the quota reservation. The next scheduled run retries with fresh searches; no tight retry loop or automatic overage. Manual retries also use the same quota. A dedicated provider account avoids quota spent by other applications. SerpApi may count billing windows differently; rolling 31-day limits are conservative.
* Partial sends: each channel has an independent receipt. A channel never attempted can be sent on the next run; a channel already attempted is never blindly retried. Run **receipts** to reconcile known message IDs. `uncertain` or `attempting` without an ID means a crash/timeout could have happened after delivery. Inspect the private provider console before acting. Only after proving that no send occurred, remove that channel's receipt from the private `state.json` and commit on the `state` branch to permit a retry. If a message was accepted, record its ID and actual status instead. Do not delete accepted/delivered receipts or reset the whole state.
* A failed/bounced/undelivered message blocks further monitoring until the recipient/sender issue is resolved and the receipt is deliberately reconciled. No automatic repeat billing.
* Git push failure stops further side effects. Never use force pushes for state, never run another scheduler/writer, and never restore an old state copy without reconciling provider usage and sends. Concurrency serializes GitHub runs; local locks cannot coordinate independent cloud deployments.
* The free plan does not support instant monitoring. More frequent searches require a revised quota/cost estimate and explicit paid-plan approval. Scheduled jobs can be delayed/dropped by GitHub. Keep state private and back it up privately if desired.

## Cloud coding environment

Use the existing `/workspace/flight-check` checkout; do not create a worktree unless explicitly requested. Python 3.12 is sufficient; no dependency install or persistent local service is needed. Run `python -m unittest discover -s tests -v` in that directory. The actual scheduled bot belongs on GitHub Actions, not this interactive coding machine. Environment publication does not deploy GitHub Actions or set repository secrets.
