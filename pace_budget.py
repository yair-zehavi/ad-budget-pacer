#!/usr/bin/env python3
"""Cross-channel monthly budget pacer (Meta + Google Ads).

Keeps an advertiser's month-end ad spend on the number that was actually agreed,
by re-deriving daily budgets every morning from what has ALREADY been spent:

    daily = (month_target - month_to_date_spend) / days_left_including_today

That self-corrects. A flat "month_total / days_in_month" set once on the 1st
silently misses the target
whenever anything real happens: an account goes dark for a week, a platform
underspends its cap, or someone pushes a mid-month change. This re-plans daily,
so any of those get absorbed over the remaining days instead of blowing the month.

Design notes
------------
* Schedule is expressed in the currency the ADVERTISER budgets in (e.g. EUR) and
  converted to the currency the AD ACCOUNTS bill in (e.g. USD) at live FX,
  cached daily with a config fallback so an FX outage can never wedge a run.
* Each channel is paced against its OWN slice of the month target, so one
  channel overspending never quietly eats the other's budget.
* Utilization boost: a channel that only spends 70% of its cap (typical for
  search campaigns that are keyword-inventory-limited) gets its cap raised so
  ACTUAL spend hits the target, not just the cap. Bounded, so it can't run away.
* Distribution across ad sets / campaigns is proportional to their CURRENT
  budgets, which preserves hand-tuning done in Ads Manager. Budgets only:
  this never touches statuses, creative, targeting, or shared budgets.
* Guardrails: per-entity floor + ceiling, max step per run, and a hard
  worst-case month-end re-check that scales the whole plan down if the plan
  could still overshoot.

Dry-run by default. Pass --apply to write.

Usage:
  pace_budget.py --config budget-plan.json [--month YYYY-MM] [--apply]
  pace_budget.py --config ... --explain      # show the math, change nothing
  pace_budget.py --config ... --fixture f.json   # offline run on fake platform data
"""
import argparse
import calendar
import datetime as dt
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH = "https://graph.facebook.com/v22.0"

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = ssl.create_default_context()


# ─── small helpers ────────────────────────────────────────────────────────────

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def http_json(url, data=None, timeout=45):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return json.load(r)


def _alert_target():
    """Alert destination from the environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    optional TELEGRAM_THREAD_ID (forum topic). Returns (token, chat, thread)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    thread = os.environ.get("TELEGRAM_THREAD_ID") or None
    return token, chat, thread


def telegram(text, loud=False):
    token, chat, thread = _alert_target()
    if not token or chat in (None, ""):
        print("[warn] telegram target missing, skipping alert", file=sys.stderr)
        return
    payload = {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_notification": "false" if loud else "true",
    }
    if thread is not None:
        payload["message_thread_id"] = thread
    data = urllib.parse.urlencode(payload).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15)
    except Exception as e:  # alerting must never kill the run
        print(f"[warn] telegram send failed: {e}", file=sys.stderr)


def fx_rate(base, quote, state_dir, fallback):
    """Live FX with a daily cache and two independent sources.

    Returns (rate, source). Never raises - a bad FX lookup must not stop pacing.
    """
    if base == quote:
        return 1.0, "identity"
    cache_f = state_dir / "fx-cache.json"
    today = dt.date.today().isoformat()
    cache = {}
    if cache_f.exists():
        try:
            cache = json.loads(cache_f.read_text())
        except Exception:
            cache = {}
    key = f"{base}{quote}"
    if cache.get("date") == today and key in cache.get("rates", {}):
        return cache["rates"][key], "cache"

    rate = None
    src = None
    for name, url, extract in (
        ("frankfurter", f"https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}",
         lambda d: d["rates"][quote]),
        ("er-api", f"https://open.er-api.com/v6/latest/{base}",
         lambda d: d["rates"][quote]),
    ):
        try:
            rate = float(extract(http_json(url, timeout=20)))
            src = name
            break
        except Exception as e:
            print(f"[warn] FX source {name} failed: {e}", file=sys.stderr)

    if rate is None:
        stale = cache.get("rates", {}).get(key)
        if stale:
            return float(stale), f"stale-cache({cache.get('date')})"
        return float(fallback), "config-fallback"

    cache.setdefault("rates", {})[key] = rate
    cache["date"] = today
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(cache, indent=2))
    return rate, src


def historical_cap(state_dir, channel, since, until):
    """Mean daily cap actually in force for `channel` over [since, until].

    Utilization must be spend-vs-the-cap-that-was-live-then. Measuring against
    TODAY's cap makes any raise look like throttling (a raise on day N shows the
    prior days' spend against the new, larger cap), which would make the pacer
    over-boost right after every increase. Returns None when we have no history
    for the window - callers then treat utilization as neutral rather than
    inventing a number.
    """
    f = state_dir / "history.jsonl"
    if not f.exists():
        return None
    caps = []
    for line in f.read_text().splitlines():
        try:
            rec = json.loads(line)
            d = dt.date.fromisoformat(rec["date"])
        except Exception:
            continue
        if since <= d <= until and channel in rec.get("channels", {}):
            cap = rec["channels"][channel].get("cap")
            if cap:
                caps.append(float(cap))
    return sum(caps) / len(caps) if caps else None


def allocate(entities, target, floor, ceiling, max_step):
    """Distribute `target` across entities proportional to current budgets.

    Each entity is bounded by [floor, ceiling] and by max_step relative to its
    current budget (so no single run can shock an ad set out of learning).
    Solved by bisecting a global scale factor, which always converges and
    respects every bound simultaneously - unlike scale-then-clamp, which
    silently misses the target whenever anything hits a bound.

    Entities with a "hold" value are pinned and excluded from the solve.
    """
    held = [e for e in entities if e.get("hold") is not None]
    free = [e for e in entities if e.get("hold") is None]
    for e in held:
        e["new"] = float(e["hold"])

    rest = target - sum(e["new"] for e in held)
    if not free:
        return

    for e in free:
        cur = e["current"]
        e["_lo"] = max(floor, cur * (1 - max_step)) if cur > 0 else floor
        e["_hi"] = min(ceiling, cur * (1 + max_step)) if cur > 0 else ceiling
        if e["_hi"] < e["_lo"]:          # floor above the step ceiling → floor wins
            e["_hi"] = e["_lo"]
        # Weight = current budget (hand-tuning survives) × performance multiplier
        # (>1 for entities with a better lead rate than the channel, <1 for worse;
        # 1.0 when performance weighting is off). Step caps still bound the move.
        pm = float(e.get("perf_mult", 1.0))
        e["_w"] = (cur if cur > 0 else 1.0) * pm
        # A below-average entity is never RAISED just because the winners hit their
        # step cap and money is left over; it can only hold or shrink. The winners
        # walk up over the next runs instead (the month self-corrects daily).
        if (pm < 1.0 or e.get("no_delivery")) and cur > 0:
            e["_hi"] = max(e["_lo"], min(e["_hi"], cur))

    lo_sum = sum(e["_lo"] for e in free)
    hi_sum = sum(e["_hi"] for e in free)
    if rest <= lo_sum:
        for e in free:
            e["new"] = e["_lo"]
    elif rest >= hi_sum:
        for e in free:
            e["new"] = e["_hi"]
    else:
        def total_at(s):
            return sum(clamp(e["_w"] * s, e["_lo"], e["_hi"]) for e in free)
        lo, hi = 0.0, 1.0
        while total_at(hi) < rest and hi < 1e6:
            hi *= 2
        for _ in range(80):
            mid = (lo + hi) / 2
            if total_at(mid) < rest:
                lo = mid
            else:
                hi = mid
        s = (lo + hi) / 2
        for e in free:
            e["new"] = clamp(e["_w"] * s, e["_lo"], e["_hi"])

    # Whole currency units, then push rounding drift onto the largest free entity
    # that can absorb it without breaking its own bounds.
    for e in free:
        e["new"] = float(round(e["new"]))
    drift = round(rest) - sum(e["new"] for e in free)
    if drift:
        for e in sorted(free, key=lambda x: -x["new"]):
            if e["_lo"] <= e["new"] + drift <= e["_hi"]:
                e["new"] += drift
                break
    for e in free:
        for k in ("_lo", "_hi", "_w"):
            e.pop(k, None)


# ─── Meta ─────────────────────────────────────────────────────────────────────

def meta_spend(account, token, since, until):
    url = (f"{GRAPH}/{account}/insights?"
           + urllib.parse.urlencode({
               "fields": "spend", "level": "account",
               "time_range": json.dumps({"since": str(since), "until": str(until)}),
               "access_token": token}))
    rows = http_json(url).get("data", [])
    return float(rows[0]["spend"]) if rows else 0.0


def meta_adsets(account, token, exclude):
    out, url = [], (f"{GRAPH}/{account}/adsets?"
                    + urllib.parse.urlencode({
                        "fields": "id,name,effective_status,daily_budget,lifetime_budget",
                        "limit": 200, "access_token": token}))
    while url:
        d = http_json(url)
        for a in d.get("data", []):
            if a.get("effective_status") != "ACTIVE":
                continue
            if any(x.lower() in a["name"].lower() for x in exclude):
                continue
            if int(a.get("lifetime_budget") or 0) > 0:
                continue          # lifetime-budget ad sets pace themselves
            out.append({"id": a["id"], "name": a["name"],
                        "current": int(a.get("daily_budget") or 0) / 100})
        url = d.get("paging", {}).get("next")
    return out


def meta_perf(account, token, since, until, action):
    """{adset_id: {spend, leads}} over the window; leads = count of `action`
    (the configured lead action type, e.g. a custom pixel conversion)."""
    url = (f"{GRAPH}/{account}/insights?"
           + urllib.parse.urlencode({
               "fields": "adset_id,spend,actions", "level": "adset", "limit": 500,
               "time_range": json.dumps({"since": str(since), "until": str(until)}),
               "access_token": token}))
    out = {}
    while url:
        d = http_json(url)
        for x in d.get("data", []):
            acts = {y["action_type"]: float(y["value"]) for y in x.get("actions", [])}
            out[x["adset_id"]] = {"spend": float(x.get("spend") or 0), "leads": acts.get(action, 0.0)}
        url = d.get("paging", {}).get("next")
    return out


def meta_apply(entities, token):
    results = []
    for e in entities:
        if round(e["current"]) == round(e["new"]):
            continue
        data = urllib.parse.urlencode({
            "daily_budget": str(int(round(e["new"] * 100))),
            "access_token": token}).encode()
        try:
            http_json(f"{GRAPH}/{e['id']}", data=data)
            results.append((e["name"], True, ""))
        except Exception as ex:
            results.append((e["name"], False, _meta_err(ex)))
    return results


def _meta_err(ex):
    """Graph always explains a 400 in the RESPONSE BODY; urllib's str(HTTPError) is
    only 'HTTP Error 400: Bad Request', which is useless for diagnosis. Read the body."""
    body = None
    try:
        body = ex.read().decode("utf-8", "replace")      # urllib.error.HTTPError
    except Exception:
        pass
    if body:
        try:
            err = json.loads(body).get("error", {})
            msg = (err.get("error_user_title") or "").strip()
            det = (err.get("error_user_msg") or err.get("message") or "").strip()
            code = err.get("code")
            sub = err.get("error_subcode")
            parts = [p for p in (msg, det) if p]
            label = " - ".join(parts) or body[:160]
            return f"{label} [code {code}/{sub}]"[:300]
        except Exception:
            return body[:200]
    return str(ex)[:160]


# ─── Google Ads ───────────────────────────────────────────────────────────────

_GC = {}


def google_client():
    """Cached. A fresh client per call lets gRPC collect the channel out from
    under an in-flight search_stream ("Channel deallocated!")."""
    if "c" in _GC:
        return _GC["c"]
    from google.ads.googleads.client import GoogleAdsClient
    _GC["c"] = GoogleAdsClient.load_from_dict({
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_OAUTH_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        "use_proto_plus": True,
    })
    _GC["ga"] = _GC["c"].get_service("GoogleAdsService")
    return _GC["c"]


def google_query(client, cid, gaql):
    ga = _GC.get("ga") or client.get_service("GoogleAdsService")
    _GC["ga"] = ga                       # keep the service (and its channel) alive
    rows = []
    for batch in ga.search_stream(customer_id=cid, query=gaql):
        rows.extend(batch.results)
    return rows


def google_spend(client, cid, since, until):
    rows = google_query(client, cid, f"""
        SELECT metrics.cost_micros FROM customer
        WHERE segments.date BETWEEN '{since}' AND '{until}'""")
    return sum(r.metrics.cost_micros for r in rows) / 1e6


def google_campaigns(client, cid, exclude):
    out = []
    for r in google_query(client, cid, """
        SELECT campaign.id, campaign.name, campaign_budget.resource_name,
               campaign_budget.amount_micros, campaign_budget.explicitly_shared
        FROM campaign WHERE campaign.status = 'ENABLED'"""):
        if r.campaign_budget.explicitly_shared:
            continue              # shared budgets fan out to campaigns we don't own here
        if any(x.lower() in r.campaign.name.lower() for x in exclude):
            continue
        out.append({"id": str(r.campaign.id), "name": r.campaign.name,
                    "budget_rn": r.campaign_budget.resource_name,
                    "current": r.campaign_budget.amount_micros / 1e6})
    return out


def google_perf(client, cid, since, until):
    """{campaign_id: {spend, leads}} over the window; leads = metrics.conversions
    (every conversion action the account counts - leads + calls for lead-gen)."""
    out = {}
    for r in google_query(client, cid, f"""
        SELECT campaign.id, metrics.cost_micros, metrics.conversions FROM campaign
        WHERE segments.date BETWEEN '{since}' AND '{until}' AND campaign.status = 'ENABLED'"""):
        p = out.setdefault(str(r.campaign.id), {"spend": 0.0, "leads": 0.0})
        p["spend"] += r.metrics.cost_micros / 1e6
        p["leads"] += float(r.metrics.conversions)
    return out


def apply_performance(ents, perf, pcfg):
    """Attach perf_mult / cpl / leads to each entity from a {id: {spend, leads}} map.
    Bayesian-smoothed toward the channel lead rate (prior_spend in currency) so a
    small ad set is not judged on two leads. Returns the channel CPL (or None)."""
    prior = float(pcfg.get("prior_spend", 300))
    lo, hi = pcfg.get("mult_bounds", [0.5, 2.0])
    tot_sp = sum(perf.get(e["id"], {}).get("spend", 0.0) for e in ents)
    tot_ld = sum(perf.get(e["id"], {}).get("leads", 0.0) for e in ents)
    ch_rate = tot_ld / tot_sp if tot_sp else 0.0
    for e in ents:
        p = perf.get(e["id"], {"spend": 0.0, "leads": 0.0})
        e["spend_lb"], e["leads_lb"] = p["spend"], p["leads"]
        e["cpl_lb"] = (p["spend"] / p["leads"]) if p["leads"] else None
        if p["spend"] <= 0 and e.get("hold") is None:
            e["no_delivery"] = True          # active but not spending: never raise it
        elif ch_rate > 0 and e.get("hold") is None:
            sm = (p["leads"] + prior * ch_rate) / (p["spend"] + prior)
            e["perf_mult"] = clamp(sm / ch_rate, lo, hi)
    return (tot_sp / tot_ld) if tot_ld else None


def google_apply(client, cid, entities):
    from google.api_core import protobuf_helpers
    changes = [e for e in entities if round(e["current"]) != round(e["new"])]
    if not changes:
        return []
    ops = []
    for e in changes:
        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name = e["budget_rn"]
        op.update.amount_micros = int(round(e["new"]) * 1_000_000)
        op.update_mask.CopyFrom(protobuf_helpers.field_mask(None, op.update._pb))
        ops.append(op)
    client.get_service("CampaignBudgetService").mutate_campaign_budgets(
        customer_id=cid, operations=ops)
    return [(e["name"], True, "") for e in changes]


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--month", default=None, help="YYYY-MM (default: today's month)")
    ap.add_argument("--apply", action="store_true", help="write budgets (default: dry-run)")
    ap.add_argument("--explain", action="store_true", help="print the math, never write")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--state-dir", default=None, help="history.jsonl + fx cache (default: ./state next to the config)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override for reproducible runs")
    ap.add_argument("--fixture", default=None,
                    help="JSON file with fake platform data; no network, never writes")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text())
    state_dir = Path(args.state_dir).resolve() if args.state_dir else cfg_path.parent / "state"
    g = cfg.get("guardrails", {})
    max_step = g.get("max_step", 0.30)
    util_floor = g.get("utilization_floor", 0.5)
    max_util_boost = g.get("max_util_boost", 1.5)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    fixture = json.loads(Path(args.fixture).read_text()) if args.fixture else None
    if fixture:
        args.apply, args.no_telegram = False, True
    month = args.month or today.strftime("%Y-%m")
    year, mon = (int(x) for x in month.split("-"))
    last_day = dt.date(year, mon, calendar.monthrange(year, mon)[1])
    month_start = dt.date(year, mon, 1)
    # Run is early morning: today is still fully spendable, so it counts.
    days_left = max((last_day - max(today, month_start)).days + 1, 1)

    sched = cfg["schedule"]
    if month not in sched:
        msg = (f"budget-pacer [{cfg['name']}]: no target for {month} - "
               f"schedule ends {max(sched)}. Nothing changed.")
        print(msg)
        if not args.no_telegram:
            telegram(msg, loud=True)
        return

    if fixture:
        rate, fx_src = float(fixture.get("fx", 1.0)), "fixture"
    else:
        rate, fx_src = fx_rate(cfg["currency"], cfg["account_currency"],
                               state_dir, cfg.get("fx_fallback", 1.0))
    target_client_ccy = float(sched[month])
    target = target_client_ccy * rate

    lines = [
        f"Budget pacer [{cfg['name']}] {month}  ({'APPLY' if args.apply and not args.explain else 'DRY-RUN'})",
        f"target {target_client_ccy:,.0f} {cfg['currency']} × {rate:.4f} ({fx_src}) "
        f"= {target:,.0f} {cfg['account_currency']} | day {today.day}/{last_day.day}, "
        f"{days_left} left",
    ]

    # MTD window: month start → today, clamped inside the month. Empty when
    # planning a month that hasn't started (--month is used for what-if runs).
    mtd_end = min(today, last_day)
    mtd_open = mtd_end >= month_start

    # Utilization window: trailing 7 complete days. Deliberately NOT clamped to
    # the month - how fully a channel spends its cap is a property of the
    # channel, not of the calendar, and clamping would blind the 1st-of-month run.
    win_end = min(today - dt.timedelta(days=1), last_day)
    win_start = win_end - dt.timedelta(days=6)
    win_open = win_end >= win_start

    plans, mtd_total, alerts = {}, 0.0, []

    def base_share(c):
        # share may be a flat number or {"default": x, "YYYY-MM": y} - the
        # Meta/Google split genuinely differs month to month (seasonality,
        # search-inventory ceilings), so it has to be schedulable too.
        share = c["share"]
        if isinstance(share, dict):
            share = share.get(month, share.get("default"))
            if share is None:
                raise SystemExit(f"no share for {month} and no default")
        return float(share)

    # ── pass 1: pull entities + (optional) performance for every channel ──────
    pulled, ch_cpl = {}, {}
    for ch, c in cfg["channels"].items():
        exclude = c.get("exclude", [])
        hold = c.get("hold", {})
        pcfg = c.get("performance") or {}
        lb_days = int(pcfg.get("lookback_days", 14))
        lb_start = win_end - dt.timedelta(days=lb_days - 1)
        if fixture:
            f = fixture["channels"][ch]
            mtd = f["mtd"] if mtd_open else 0.0
            recent = f["recent"] if win_open else 0.0
            ents = [dict(e) for e in f["entities"]
                    if not any(x.lower() in e["name"].lower() for x in exclude)]
            perf = f.get("perf") if pcfg.get("enabled") and win_open else None
        elif ch == "meta":
            token = os.environ.get("META_ACCESS_TOKEN")
            if not token:
                raise SystemExit("META_ACCESS_TOKEN not set")
            acct = c["ad_account_id"]
            mtd = meta_spend(acct, token, month_start, mtd_end) if mtd_open else 0.0
            recent = meta_spend(acct, token, win_start, win_end) if win_open else 0.0
            ents = meta_adsets(acct, token, exclude)
            perf = meta_perf(acct, token, lb_start, win_end, c.get("lead_action", "lead")) \
                if pcfg.get("enabled") and win_open else None
        elif ch == "google":
            gc = google_client()
            cid = c["customer_id"]
            mtd = google_spend(gc, cid, month_start, mtd_end) if mtd_open else 0.0
            recent = google_spend(gc, cid, win_start, win_end) if win_open else 0.0
            ents = google_campaigns(gc, cid, exclude)
            perf = google_perf(gc, cid, lb_start, win_end) if pcfg.get("enabled") and win_open else None
        else:
            raise SystemExit(f"unknown channel {ch!r}")
        for e in ents:
            for name, amt in hold.items():
                if name.lower() in e["name"].lower():
                    e["hold"] = amt
        if perf is not None:
            ch_cpl[ch] = apply_performance(ents, perf, pcfg)
        pulled[ch] = {"mtd": mtd, "recent": recent, "ents": ents, "share": base_share(c)}

    # ── cross-channel auto split (two-channel plans only) ─────────────────────
    # A channel with "share_auto": {min, max, elasticity} has its scheduled share
    # nudged by relative CPL vs the other channel, inside [min, max]; the other
    # channel takes the remainder. Money can move between channels here because
    # the target is a COMBINED monthly number, not per-channel caps.
    if len(pulled) == 2:
        names = list(pulled)
        for i, ch in enumerate(names):
            sa = cfg["channels"][ch].get("share_auto")
            other = names[1 - i]
            if not sa or not ch_cpl.get(ch) or not ch_cpl.get(other):
                continue
            base = pulled[ch]["share"]
            adj = base * (ch_cpl[other] / ch_cpl[ch]) ** float(sa.get("elasticity", 0.5))
            new = clamp(adj, float(sa["min"]), float(sa["max"]))
            pulled[ch]["share"], pulled[other]["share"] = new, 1.0 - new
            lines.append(f"  split: {ch} share {base:.0%} → {new:.0%} "
                         f"(CPL {ch} {ch_cpl[ch]:,.0f} vs {other} {ch_cpl[other]:,.0f})")
            break

    # ── pass 2: pace each channel against its slice ───────────────────────────
    for ch, c in cfg["channels"].items():
        ch_target = target * pulled[ch]["share"]
        hold = c.get("hold", {})
        mtd, recent, ents = pulled[ch]["mtd"], pulled[ch]["recent"], pulled[ch]["ents"]
        mtd_total += mtd

        if not ents:
            alerts.append(f"{ch}: no eligible active entities - channel may be paused/dark")
            plans[ch] = {"mtd": mtd, "entities": [], "cap": 0.0, "util": None,
                         "target": ch_target}
            lines.append(f"  {ch}: MTD {mtd:,.0f} - NO ACTIVE ENTITIES, skipped")
            continue

        cap_now = sum(e["current"] for e in ents)
        win_days = max((win_end - win_start).days + 1, 1)
        # Utilization = actual spend ÷ the cap that was live during the window.
        # Neutral (1.0) until history covers the window, so a fresh install
        # never boosts on a guess.
        hist_cap = historical_cap(state_dir, ch, win_start, win_end)
        util = clamp(recent / win_days / hist_cap, util_floor, 1.0) \
            if hist_cap and recent else 1.0

        need_daily = max(ch_target - mtd, 0.0) / days_left
        # Raise the CAP so ACTUAL spend lands on need_daily, bounded so a
        # temporarily-throttled channel can't spiral its own cap upward.
        ch_cap = min(need_daily / util, need_daily * max_util_boost)

        floor_d = c.get("floor_daily", 5)
        # Below-floor months are an ad-set-COUNT problem, not a budget problem:
        # you cannot spend 109/day across 11 ad sets without starving all of
        # them. Say so explicitly rather than silently landing over target.
        held_sum = sum(v for k, v in hold.items()
                       if any(k.lower() in e["name"].lower() for e in ents))
        min_possible = floor_d * len([e for e in ents if e.get("hold") is None]) + held_sum
        if min_possible > ch_cap:
            alerts.append(
                f"{ch}: FLOOR-BOUND - {len(ents)} entities at floor {floor_d}/d sum to "
                f"{min_possible:,.0f}/d but only {ch_cap:,.0f}/d is needed. Month will land "
                f"~{min_possible * days_left / max(ch_target - mtd, 1) * 100 - 100:+.0f}% over "
                f"this channel's target unless ad sets are consolidated or paused.")

        allocate(ents, ch_cap, floor_d, c.get("ceiling_daily", 1e6), max_step)

        # Overshoot guard. Pessimistic utilization, not 100%: a channel running
        # at 60% of cap will not jump to 100% overnight, and assuming it would
        # cancels out the utilization boost entirely (the plan then lands well
        # UNDER target, which is just as wrong as overshooting).
        planned = sum(e["new"] for e in ents)
        pess_util = min(util * 1.3, 1.0)
        worst = mtd + planned * pess_util * days_left
        overshoot = g.get("overshoot_alert", 1.05)
        if planned and worst > ch_target * overshoot:
            scale = max(ch_target - mtd, 0.0) / (planned * pess_util * days_left)
            for e in ents:
                if e.get("hold") is None:
                    e["new"] = float(max(round(e["new"] * scale), c.get("floor_daily", 5)))
            # Only alert if the plan STILL overshoots after scaling. Routine
            # step-capping (the 30%/run limit walking a big change down over a
            # few days) is the design working, not something to page about.
            residual = mtd + sum(e["new"] for e in ents) * pess_util * days_left
            if residual > ch_target * overshoot:
                alerts.append(
                    f"{ch}: cannot reach target - floors/step-caps hold spend at "
                    f"~{residual:,.0f} vs target {ch_target:,.0f} "
                    f"({residual / ch_target * 100 - 100:+.0f}%)")

        plans[ch] = {"mtd": mtd, "entities": ents, "cap": sum(e["new"] for e in ents),
                     "util": util, "target": ch_target, "recent_daily": recent / win_days}
        cpl_note = f" | CPL {ch_cpl[ch]:,.0f}" if ch_cpl.get(ch) else ""
        lines.append(
            f"  {ch}: target {ch_target:,.0f} | MTD {mtd:,.0f} | "
            f"need {need_daily:,.0f}/d | util {util:.0%} | "
            f"cap {cap_now:,.0f} → {sum(e['new'] for e in ents):,.0f}/d{cpl_note}")
        for e in sorted(ents, key=lambda x: -x["new"]):
            tag = " [held]" if e.get("hold") is not None else ""
            arrow = "" if round(e["current"]) == round(e["new"]) else \
                    f"  {e['current']:.0f} → {e['new']:.0f}"
            perf = ""
            if e.get("no_delivery"):
                perf = "  no delivery in window"
            elif "leads_lb" in e:
                perf = f"  {e['leads_lb']:.0f} leads" + (f" @{e['cpl_lb']:,.0f}" if e.get("cpl_lb") else "")
            lines.append(f"      {e['name'][:40]:<40} {e['new']:>6.0f}/d{arrow}{perf}{tag}")

    projected = mtd_total + sum(
        p["cap"] * (p["util"] or 1.0) * days_left for p in plans.values())
    pct = projected / target * 100 if target else 0
    lines.append(f"projected month-end ≈ {projected:,.0f} {cfg['account_currency']} "
                 f"({pct:.0f}% of target) | MTD {mtd_total:,.0f}")
    if mtd_total > target:
        alerts.append(f"MTD {mtd_total:,.0f} already exceeds the {month} target "
                      f"{target:,.0f} - budgets floored, month will land over.")
    for a in alerts:
        lines.append(f"  ! {a}")

    report = "\n".join(lines)
    print(report)

    if args.explain or not args.apply:
        print("\n[DRY-RUN] nothing written. Re-run with --apply to push.")
        return

    applied = []
    for ch, p in plans.items():
        if not p["entities"]:
            continue
        if ch == "meta":
            applied += meta_apply(p["entities"], os.environ["META_ACCESS_TOKEN"])
        elif ch == "google":
            applied += google_apply(google_client(), cfg["channels"][ch]["customer_id"],
                                    p["entities"])
    failed = [a for a in applied if not a[1]]
    lines.append(f"applied: {len(applied) - len(failed)} ok, {len(failed)} failed")
    for name, _, err in failed:
        lines.append(f"  FAIL {name}: {err}")

    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "history.jsonl", "a") as f:
        f.write(json.dumps({
            "date": str(today), "month": month, "fx": rate, "fx_source": fx_src,
            "target_client_ccy": target_client_ccy, "target": round(target),
            "mtd": round(mtd_total), "days_left": days_left,
            "projected": round(projected), "alerts": alerts,
            "channels": {ch: {
                "mtd": round(p["mtd"]), "cap": round(p["cap"]),
                "util": p["util"],
                "share": round(pulled[ch]["share"], 4), "cpl": ch_cpl.get(ch),
                "entities": [{"id": e["id"], "name": e["name"],
                              "old": round(e["current"]), "new": round(e["new"]),
                              "leads": e.get("leads_lb"), "cpl": e.get("cpl_lb"),
                              "perf_mult": e.get("perf_mult")}
                             for e in p["entities"]],
            } for ch, p in plans.items()},
        }, ensure_ascii=False) + "\n")

    report = "\n".join(lines)
    print("\n".join(lines[-(len(failed) + 2):]))
    if not args.no_telegram:
        telegram(f"<pre>{report}</pre>", loud=bool(alerts or failed))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"budget-pacer FAILED: {type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        if "--no-telegram" not in sys.argv:
            telegram(msg, loud=True)
        raise
