"""Jev editorial gate for the WPS Watch news rail.

Purpose: keep reporting that serves a Philippine reader's understanding of the
West Philippine Sea, and hold items that amplify PRC state framing.

Order of gates (static first, model second):
  1. source allowlist + deny list + relevance keywords   (ingest.py, no model)
  2. Jev scores the item on two axes: Philippine interest, PRC narrative
  3. enforce: keep only items that pass both thresholds

Fail-open: if Jev is disabled, capped, times out or errors, every allowlisted
item is kept and the API reports filter_state="degraded". A filter outage must
never empty the rail silently.

Every decision is logged through jev.write_log() with caller="wps_watch" so the
shared Jev log stays the single source of truth for cost and calibration.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

SCRIPTS = "/data/workspace/scripts"
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import jev  # noqa: E402

CACHE_PATH = "/data/state/wps_jev_cache.json"
CALLER = "wps_watch"
CACHE_VERSION = "v2|"   # bump when question wording changes: invalidates cached verdicts
PRESET = "wps_editorial"

# The wording matters more than the thresholds: vague questions collapse every
# item into the mid band. Each axis states who the reader is and what counts.
QUESTIONS = {
    "ph_interest": {
        "type": "noul",
        "instructions": (
            "Does this item report something a Philippine reader needs in order to understand "
            "their own maritime situation in the West Philippine Sea? Yes for Philippine fishing "
            "communities, coast guard or navy operations, resupply and rotation missions, "
            "harassment or water-cannon incidents, Filipino casualties or arrests, Philippine "
            "government positions, statements by Philippine church, civic or fisherfolk leaders "
            "on the dispute, foreign naval or coast guard activity inside the Philippine EEZ, "
            "surveys, or fuel and fish supply effects on Filipinos. No for South China Sea "
            "commentary, business or mining deal news, or policy essays where the Philippines "
            "is not the subject."
        ),
    },
    "prc_narrative": {
        "type": "noul",
        "instructions": (
            "Does this item carry the Chinese party-state's framing into the reader's head? Yes "
            "if it asserts the waters are China's inherent territory, describes Chinese coast "
            "guard or militia action as lawful, routine or humanitarian, casts Philippine or "
            "allied activity as the provocation, quotes only PRC officials or state media as "
            "fact, or repeats phrases lifted from PRC foreign ministry statements. No for "
            "independent reporting that quotes those claims while attributing them."
        ),
    },
    "attribution": {
        "type": "score",
        "instructions": (
            "How independent is the sourcing? Base the answer on who is quoted and whether "
            "claims are attributed, not on the outlet's nationality."
        ),
        "criteria": [
            "single unattributed claim or PRC state source only",
            "mixed, partly attributed",
            "named independent sources, Philippine officials, or direct observation",
        ],
    },
    "local_value": {
        "type": "score",
        "instructions": "How directly does this affect Filipinos, not just regional politics?",
        "criteria": [
            "no direct Philippine consequence",
            "indirect, policy or diplomatic level",
            "direct effect on Filipinos at sea, on the coast, or on national policy",
        ],
    },
}


def gate_config(conf):
    """Jev config for this caller: shared defaults, our own caps and log dir."""
    cfg = dict(jev.load_config())
    j = conf["news"]["jev"]
    cfg.update({
        "daily_cost_cap_usd": float(j.get("cost_cap_usd", 0.6)),
        "log_dir": "/data/logs/jev",
        "shadow_callers": sorted(set(list(cfg.get("shadow_callers") or []) + [CALLER])),
    })
    return cfg


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(cache, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cache = {k: v for k, v in cache.items() if (v.get("ts") or "") >= cutoff}
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, separators=(",", ":"))
    os.replace(tmp, CACHE_PATH)


def _state(item):
    return "SOURCE: %s\nTITLE: %s\nSUMMARY: %s" % (
        item.get("source", ""), item.get("title", ""), (item.get("summary") or "")[:600])


def _verdict(decision):
    answers = (decision or {}).get("answers") or {}
    def prob(name):
        a = answers.get(name) or {}
        return a.get("noul") if isinstance(a, dict) else None
    def score(name):
        a = answers.get(name) or {}
        return a.get("score") if isinstance(a, dict) else None
    return {"ph_interest": prob("ph_interest"), "prc_narrative": prob("prc_narrative"),
            "attribution": score("attribution"), "local_value": score("local_value")}


def gate(items, conf, budget_s=20.0):
    """Score unseen items. Returns (kept, held, stats)."""
    j = conf["news"]["jev"]
    cache = _load_cache()
    stats = {"scored": 0, "cached": 0, "errors": 0, "cost_usd": 0.0,
             "filter_state": "off", "model": None}
    kept, held = [], []

    if not j.get("enabled"):
        for it in items:
            it["verdict"] = None
            kept.append(it)
        stats["filter_state"] = "off"
        return kept, held, stats

    cfg = gate_config(conf)
    deadline = time.time() + budget_s
    budget_left = int(j.get("max_per_cycle", 30))
    degraded = False

    for it in items:
        ref = hashlib.sha256((CACHE_VERSION + it["id"]).encode()).hexdigest()[:16]
        v = cache.get(ref)
        if v:
            stats["cached"] += 1
        elif budget_left > 0 and time.time() < deadline:
            d = jev.ask(_state(it), QUESTIONS, cfg=cfg, caller=CALLER,
                        preset=PRESET, deadline=deadline)
            budget_left -= 1
            if not d.get("ok"):
                degraded = True
                stats["errors"] += 1
                v = None
            else:
                v = _verdict(d)
                v["ts"] = datetime.now(timezone.utc).isoformat()
                v["model"] = d.get("model_resolved")
                stats["model"] = d.get("model_resolved") or stats["model"]
                stats["cost_usd"] += float(d.get("cost_usd") or 0)
                stats["scored"] += 1
                cache[ref] = v
                entry = {"ts": v["ts"], "ok": True, "caller": CALLER, "preset": PRESET,
                         "model_requested": d.get("model_requested"),
                         "model_resolved": d.get("model_resolved"),
                         "answers": d.get("answers"), "cost_usd": d.get("cost_usd"),
                         "latency_ms": d.get("latency_ms"), "item_ref": ref,
                         "source": it.get("source"), "title": it.get("title", "")[:120],
                         "enforced": bool(j.get("enforce"))}
                try:
                    jev.write_log(entry, cfg)
                except Exception:
                    pass
        else:
            degraded = True
            v = None

        it["verdict"] = v
        if v and v.get("ph_interest") is not None and v.get("prc_narrative") is not None:
            ok = (v["ph_interest"] >= float(j.get("ph_min", 0.45))
                  and v["prc_narrative"] <= float(j.get("prc_max", 0.55)))
        else:
            ok = True  # fail-open per item

        if ok or not j.get("enforce"):
            kept.append(it)
        else:
            held.append(it)

    try:
        _save_cache(cache, int(j.get("cache_days", 7)))
    except Exception:
        pass

    if not stats["scored"] and not stats["cached"]:
        stats["filter_state"] = "degraded"
    elif degraded:
        stats["filter_state"] = "partial"
    else:
        stats["filter_state"] = "on"
    kept.sort(key=lambda i: (
        -((i.get("verdict") or {}).get("local_value") or 0),
        -(i.get("published").timestamp() if i.get("published") else 0)))
    return kept, held, stats
