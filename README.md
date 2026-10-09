# Ad Budget Pacer

A small Python tool that keeps a monthly Meta Ads and Google Ads budget on target by recomputing every campaign's daily budget each morning from what has actually been spent so far.

## Why it matters

- No month-end surprises. A daily budget set once on the 1st drifts whenever an account pauses, a platform under-delivers, or someone edits a budget mid-month. Re-planning from actual spend absorbs all of that over the remaining days.
- One combined target across channels. Each channel is paced against its own share of the month, so one channel overspending does not quietly eat the other's budget.
- Money follows results, not just spend. Optionally, budgets are weighted by lead rate (Meta lead actions, Google conversions), with smoothing so a small ad set is not judged on two leads.
- Alerts on problems only. A summary is sent every run, but it is silent (no notification) unless the plan cannot reach the target, a write fails, or a channel has gone dark.

## How the pacing math works

Everything below is taken from `pace_budget.py`.

Per month, per channel:

```
target          = schedule[month] * fx_rate            (advertiser currency -> account currency)
channel_target  = target * share
days_left       = last_day_of_month - today + 1        (today still counts, the run is early morning)
need_daily      = max(channel_target - month_to_date_spend, 0) / days_left
```

A channel that does not spend its whole cap (common for search campaigns limited by keyword inventory) needs a higher cap to land the same actual spend:

```
util            = clamp( (spend_last_7_days / 7) / avg_cap_in_force_then , util_floor, 1.0 )
channel_cap     = min( need_daily / util , need_daily * max_util_boost )
```

`avg_cap_in_force_then` comes from the tool's own `history.jsonl`, not today's cap, so a raise is never mistaken for throttling. Until history covers the window, `util` is 1.0 (no boost on a guess).

The channel cap is split across active campaigns or ad sets by weight, solved by bisection on a single scale factor `s`:

```
new_i  = clamp( current_i * perf_mult_i * s ,
                max(floor, current_i * (1 - max_step)),
                min(ceiling, current_i * (1 + max_step)) )
sum(new_i) = channel_cap - sum(held budgets)
```

`perf_mult` is 1.0 unless performance weighting is on:

```
channel_rate = leads / spend over the lookback window
smoothed_i   = (leads_i + prior_spend * channel_rate) / (spend_i + prior_spend)
perf_mult_i  = clamp( smoothed_i / channel_rate , lo , hi )
```

Then a worst-case check. Assuming a channel will not jump to full cap overnight:

```
pess_util = min(util * 1.3, 1.0)
worst     = mtd + planned_daily_total * pess_util * days_left
if worst > channel_target * overshoot_alert:  scale unheld budgets down to fit
```

If floors or step limits still leave the plan over target, it alerts instead of hiding it. With two channels, an optional `share_auto` block moves share toward the cheaper channel: `share = base * (cpl_other / cpl_this) ^ elasticity`, clamped to `[min, max]`, with the other channel taking the remainder.

## Modes

- Proportional (default): performance weighting off. Budgets are redistributed in proportion to their current values, so manual tuning in the ad platforms survives.
- Performance: `performance.enabled: true` on a channel. Weights become `current * perf_mult`. An entity with `perf_mult < 1`, or one that is active but had no spend in the window, is never raised; it can only hold or shrink. Winners walk up over consecutive runs under the step cap.
- Dry-run (default) versus `--apply`. Without `--apply` nothing is written. `--explain` prints the math and never writes.
- `hold`: a name substring pinned to a fixed daily budget, excluded from the solve. `exclude`: name substrings never touched. Lifetime-budget ad sets and shared Google budgets are skipped.

Only daily budgets are changed. Statuses, creative, targeting and bids are never touched, and the pacer will not turn a paused channel back on.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # only needed for live Google Ads / GA4; the core uses the standard library
cp .env.example .env                 # fill in, then export the variables (never commit .env)
cp budget-plan.example.json budget-plan.json
```

Run it offline first, on fake data:

```bash
python3 pace_budget.py --config budget-plan.example.json --fixture tests/fixture.json \
  --month 2026-03 --today 2026-03-16 --state-dir /tmp/pacer-state
```

Live dry-run, then apply:

```bash
python3 pace_budget.py --config budget-plan.json
python3 pace_budget.py --config budget-plan.json --apply
```

Schedule the `--apply` run once a day, early morning, with cron or any scheduler. Scheduling is not included.

## Config

One JSON file per advertiser (see `budget-plan.example.json`).

| Key | Meaning |
|---|---|
| `currency`, `account_currency` | Budget currency and the currency the ad accounts bill in. FX comes from two public sources, cached daily, with `fx_fallback` if both fail. |
| `schedule` | `"YYYY-MM": amount` in `currency`. A missing month changes nothing and raises an alert. |
| `channels.meta` / `channels.google` | Account IDs, `share` (a number, or `{"default": x, "YYYY-MM": y}`), `floor_daily`, `ceiling_daily`, `exclude`, `hold`. |
| `lead_action` (Meta) | The action type counted as a lead when performance weighting is on. |
| `performance` | `enabled`, `lookback_days`, `prior_spend`, `mult_bounds`. |
| `share_auto` | `min`, `max`, `elasticity`; two-channel plans only. |
| `guardrails` | `max_step`, `utilization_floor`, `max_util_boost`, `overshoot_alert`. |
| `analytics.ga4` | Used only by `lead_snapshot.py`: `property_id`, `conversion_events`. |

Credentials are read from environment variables only (see `.env.example`). Alerts go to Telegram if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set; otherwise the run just prints.

State is written next to the config in `state/`: `history.jsonl` (one record per applied run, also the input to the utilization calculation) and `fx-cache.json`.

`lead_snapshot.py --config budget-plan.json` writes a GA4 snapshot (paid leads by campaign and term, device, landing page, day, hour, region, new versus returning, plus the last 14 pacer runs) for a weekly human or LLM review. It makes no ad-platform writes.

## Example output

An example run on the fake fixture (all numbers are invented):

```
Budget pacer [Example Advertiser] 2026-03  (DRY-RUN)
target 6,000 EUR × 1.0800 (fixture) = 6,480 USD | day 16/31, 16 left
  split: google share 30% → 28% (CPL google 32 vs meta 27)
  meta: target 4,694 | MTD 1,900 | need 175/d | util 100% | cap 150 → 175/d | CPL 27
      Prospecting A                                78/d  60 → 78  30 leads @20
      Prospecting B                                58/d  60 → 58  10 leads @60
      Retargeting                                  39/d  30 → 39  15 leads @20
  google: target 1,786 | MTD 900 | need 55/d | util 100% | cap 60 → 55/d | CPL 32
      Generic Search                               35/d  40 → 35  12 leads @47
      Brand                                        20/d  14 leads @20 [held]
projected month-end ≈ 6,480 USD (100% of target) | MTD 2,800
```

Alert lines, from the same fixture with a deliberately low target (invented):

```
  ! meta: FLOOR-BOUND - 3 entities at floor 10/d sum to 30/d but only 28/d is needed. Month will land ~+7% over this channel's target unless ad sets are consolidated or paused.
  ! google: cannot reach target - floors/step-caps hold spend at ~1,380 vs target 893 (+55%)
```

Alert types: `FLOOR-BOUND` (too many ad sets for the budget), `cannot reach target`, `no eligible active entities` (channel is dark; nothing is turned on), `no target for YYYY-MM`, and month-to-date already above target.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Eight offline tests cover the allocator (target hit, step cap, holds, never-raise-losers, floors), the lead-rate smoothing, and an end-to-end dry run on fake data. No network or credentials are used.

## Limitations

- Meta and Google Ads only. Two channels maximum for `share_auto`.
- The live Meta and Google code paths (API calls, writes) are not covered by the offline tests; they were used in production but are not exercised here.
- Lead counts come from platform-reported actions and conversions, not from a CRM or website form table. Platform attribution applies.
- Budget changes are limited by `max_step` per run, so a large correction takes several days.
- It does not manage lifetime budgets, shared Google budgets, bids or statuses.
- FX uses two free public APIs; if both fail it uses the cached or configured rate.
- Not a hosted service: you provide credentials, a scheduler, and review of the dry-run output before enabling `--apply`.
