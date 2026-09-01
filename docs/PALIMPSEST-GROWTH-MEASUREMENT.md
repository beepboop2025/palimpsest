# Palimpsest growth measurement

This plan separates discovery, browser activity and confirmed activation. The
layers must not be added together or described as unique people.

## Evidence ladder

| Layer | Source | What it establishes | What it does not establish |
| --- | --- | --- | --- |
| Entry and page load | Cloudflare Web Analytics | A browser loaded a public path | A unique human, qualified reader or customer |
| Deep read | `PALIMPSEST_GROWTH_EVENT` with `deep_read` | A production page remained visible for at least 20 seconds and reached 50% depth | Comprehension, identity or intent |
| CTA action | `brief_clicked`, `follow_clicked`, `feed_clicked`, `inquiry_clicked` | A named production-page control was activated | A completed subscription, inquiry or purchase |
| Confirmed Telegram start | Palimpsest bot lead record | Telegram delivered `/start` with an attributable tag | Long-term retention or willingness to pay |
| Institutional inquiry | Created GitHub issue | A public inquiry was submitted | A private lead, signed pilot or payer |

Cloudflare Web Analytics remains the page-level discovery source. As of
2026-09-01, [Cloudflare documents that Web Analytics does not support custom
events](https://developers.cloudflare.com/web-analytics/faq/#does-web-analytics-support-custom-events),
so the small first-party endpoint records only the missing action layer. The
event line contains a closed event name, a closed location, a bounded site path,
a coarse source bucket and the server receipt clock. It contains no cookie, user
identifier, raw referrer, query string or client-supplied timestamp.

The application suppresses its normal IP-bearing access line for `/events`.
Hosting and network providers may still retain connection metadata under their
own policies; this application-level record does not change that boundary.

## Attribution tags

- Homepage Telegram: `china_home_primary`
- Situation top panel: `china_situation_top`
- Situation bottom panel: `china_situation_bottom`

The Telegram bot, not the website click, is the source for confirmed starts.
Owner IDs are excluded in the existing funnel digest. Website events cannot
reliably distinguish the owner from another browser, so they remain raw activity
and should be reported with that label.

## Source buckets

Only `direct`, `same_site`, `search`, `ai`, `social` and `other` leave the
browser. The raw referring URL never does. Do Not Track, Global Privacy Control
and `localStorage.palimpsest_analytics_opt_out=1` disable the first-party event
layer. The collector runs only on `www.palimpsest.info`, not local previews.

## Weekly funnel report

Report these as separate counts for one UTC window:

1. Cloudflare entry events and page loads for `/` and `/news/china/situation/`.
2. Raw `deep_read` records by page and coarse source bucket.
3. Raw CTA records by event and location.
4. Confirmed non-owner bot starts by the three attribution tags.
5. Submitted institutional inquiries and whether they progressed to a written
   request, pilot or payment.

Never derive a person count from event rows. Repeated loads, owner activity,
automation and spoofed same-origin requests remain possible. Until a bot start,
inquiry or payment is independently recorded, describe the result as measured
browser activity.
