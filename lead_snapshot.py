#!/usr/bin/env python3
"""Deterministic GA4 lead-gen snapshot for a weekly budget review.

Usage: lead_snapshot.py --config budget-plan.json [--days 14]

Reads the "analytics.ga4" block of the plan (property_id + conversion_events) and the
pacer's state/history.jsonl, and writes state/snapshot-YYYY-MM-DD.json with leads by
source/medium x campaign x term (utm_term = ad set id where tagged), device, landing
page, day of week, hour, region, new vs returning, all channels, and the last 14 pacer
runs. No model, no ad-platform writes. Needs GOOGLE_APPLICATION_CREDENTIALS pointing
at a service account with read access to the GA4 property.
"""
import argparse
import datetime as dt
import json
import os
import ssl
import urllib.request
from pathlib import Path

PAID = {"filter": {"fieldName": "sessionMedium",
                   "inListFilter": {"values": ["cpc", "paid_social", "paid", "ppc", "CPC", "Facebook"]}}}

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = ssl.create_default_context()


def token():
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    c = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    c.refresh(Request())
    return c.token


def pivot(rows, events, limit):
    """Rows of (dims..., eventName, eventCount) -> list of {dims, sessions, leads, per-event}."""
    agg = {}
    for row in rows:
        vals = [x["value"] for x in row["dimensionValues"]]
        agg.setdefault(tuple(vals[:-1]), {})[vals[-1]] = float(row["metricValues"][0]["value"])
    out = []
    for k, v in agg.items():
        leads = sum(v.get(e, 0) for e in events)
        out.append({"dims": list(k), "sessions": v.get("session_start", 0), "leads": leads,
                    **{e: v.get(e, 0) for e in events}})
    out.sort(key=lambda r: (-r["leads"], -r["sessions"]))
    return out[:limit]


def report(prop, tok, since, until, dims, events, flt=None, limit=60):
    """GA4 runReport of dims + eventName -> eventCount, pivoted incl. session_start."""
    exprs = [{"filter": {"fieldName": "eventName", "inListFilter": {"values": events + ["session_start"]}}}]
    if flt:
        exprs.append(flt)
    body = {"dateRanges": [{"startDate": str(since), "endDate": str(until)}],
            "dimensions": [{"name": d} for d in dims] + [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}], "limit": 5000,
            "dimensionFilter": {"andGroup": {"expressions": exprs}}}
    req = urllib.request.Request(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90, context=_SSL) as r:
        d = json.load(r)
    return pivot(d.get("rows", []), events, limit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--state-dir", default=None)
    a = ap.parse_args()
    cfg_path = Path(a.config).resolve()
    cfg = json.loads(cfg_path.read_text())
    ga4 = cfg.get("analytics", {}).get("ga4", {})
    prop, events = ga4.get("property_id"), ga4.get("conversion_events", [])
    if not prop or not events:
        raise SystemExit("plan needs analytics.ga4.property_id and conversion_events")
    state = Path(a.state_dir).resolve() if a.state_dir else cfg_path.parent / "state"
    today = dt.date.today()
    until, since = today - dt.timedelta(days=1), today - dt.timedelta(days=a.days)
    tok = token()

    def R(dims, flt=None, limit=60):
        return report(prop, tok, since, until, dims, events, flt, limit)

    snap = {
        "name": cfg.get("name"), "window": [str(since), str(until)], "lead_events": events,
        "all_channels": R(["sessionSourceMedium"], None, 30),
        "paid_campaign_term": R(["sessionSourceMedium", "sessionCampaignName", "sessionManualTerm"], PAID),
        "paid_device": R(["sessionSourceMedium", "deviceCategory"], PAID),
        "paid_landing": R(["sessionSourceMedium", "landingPagePlusQueryString"], PAID, 40),
        "paid_day_of_week": R(["sessionSourceMedium", "dayOfWeekName"], PAID),
        "paid_hour": R(["sessionSourceMedium", "hour"], PAID, 48),
        "paid_new_vs_returning": R(["sessionSourceMedium", "newVsReturning"], PAID),
        "paid_region": R(["sessionSourceMedium", "region"], PAID, 30),
    }
    hist = state / "history.jsonl"
    snap["pacer_history"] = [json.loads(l) for l in hist.read_text(encoding="utf-8").splitlines()[-14:]] \
        if hist.exists() else []
    out = state / f"snapshot-{today}.json"
    state.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
