"""Precision and recall of subscription detection on a labelled sample.

  python eval/run.py --synthetic [--gate] [--write]      synthetic data (a regression gate)
  python eval/run.py --bench 100000                      scan throughput on a large mbox
  python eval/run.py --anonymise-truth TRUTH.csv         freeze your ground truth, anonymised
  python eval/run.py --real FOLDER --truth TRUTH.csv     score on your own exports

Unit: one subscription. Precision = correct detections / active subscriptions detected.
Recall = labelled active subscriptions found / labelled active subscriptions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from subwatch.detect import detect
from subwatch.parse import AGGREGATORS, known_merchant, newest_backup, normalise, scan_file
from subwatch.synthetic import MBOX_NAME, generate, write_labels

HERE = Path(__file__).parent
SYNTHETIC_TODAY = date(2026, 11, 9)
STOP = {"ach", "upi", "bank", "ltd", "rent", "the", "and", "for", "pvt", "india", "emi"}
METRICS = ("precision", "recall", "trial_precision", "trial_recall")


def tokens(s: str) -> set[str]:
    return {w for w in normalise(s).split() if len(w) >= 3 and w not in STOP}


def same_payee(label: str, sub) -> bool:
    if key := known_merchant(label):
        return sub.merchant == key or (sub.merchant in AGGREGATORS and key not in AGGREGATORS)
    a, b = normalise(label).replace(" ", ""), sub.merchant.replace(" ", "")
    return bool(a and b) and (a in b or b in a or bool(tokens(label) & tokens(sub.merchant)))


def amount_ok(row: dict, sub) -> bool:
    want = int(float(row["amount_rupees"]) * 100)
    if not want or not sub.amount:
        return not want  # a trial with no price yet still matches by name
    slack = 0.4 if row.get("category") == "bill" else 0.15
    return abs(sub.amount - want) <= want * slack


def _match(pos: list[dict], det: list) -> tuple[list, list, list]:
    used: set[int] = set()
    tp, fn = [], []
    for row in pos:
        cands = [
            s
            for s in det
            if id(s) not in used and same_payee(row["merchant"], s) and amount_ok(row, s)
        ]
        if not cands:
            fn.append(row)
            continue
        want = float(row["amount_rupees"]) * 100
        best = min(cands, key=lambda s: abs(s.amount - want))
        used.add(id(best))
        tp.append((row, best))
    return tp, [s for s in det if id(s) not in used], fn


def score(report, truth: list[dict]) -> dict:
    subs = [s for s in report.subs if s.kind == "sub" and s.status in ("active", "cancelling")]
    trials = [s for s in report.subs if s.kind != "sub" and s.status == "trial"]
    pos = [r for r in truth if r["kind"] == "sub" and r["active"] == "yes"]
    pos_trials = [r for r in truth if r["kind"] == "trial" and r["active"] == "yes"]
    tp, fp, fn = _match(pos, subs)
    ttp, tfp, tfn = _match(pos_trials, trials)

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else 1.0

    by_cat = {}
    for cat in ("service", "bill", "commitment"):
        n = sum(1 for r in pos if r["category"] == cat)
        found = sum(1 for r, _ in tp if r["category"] == cat)
        by_cat[cat] = {"found": found, "labelled": n}
    return {
        "precision": ratio(len(tp), len(subs)),
        "recall": ratio(len(tp), len(pos)),
        "found": len(tp),
        "labelled": len(pos),
        "detected": len(subs),
        "false_alarms": [s.merchant for s in fp],
        "missed": [r["merchant"] for r in fn],
        "cadence_right": sum(1 for r, s in tp if r["period"] == s.period),
        "by_category": by_cat,
        "trial_precision": ratio(len(ttp), len(trials)),
        "trial_recall": ratio(len(ttp), len(pos_trials)),
        "trials_found": len(ttp),
        "trials_labelled": len(pos_trials),
        "trials_detected": len(trials),
        "trial_false_alarms": [s.merchant for s in tfp],
        "trials_missed": [r["merchant"] for r in tfn],
    }


def scan_folder(folder: Path) -> tuple[list, int, float]:
    started = time.perf_counter()
    events, read = [], 0
    files = sorted(folder.glob("*.mbox"))
    if sms := newest_backup(folder):
        files.append(sms)
    for f in files:
        evs, n = scan_file(f)
        events += evs
        read += n
    return events, read, time.perf_counter() - started


def show(name: str, r: dict) -> None:
    print(f"\n{name}")
    print(
        f"  subscriptions: {r['found']} of {r['labelled']} found, "
        f"{len(r['false_alarms'])} false alarm(s)  ->  precision {r['precision']:.0%}, "
        f"recall {r['recall']:.0%}"
    )
    print(f"  cadence right on {r['cadence_right']} of {r['found']} found")
    for cat, c in r["by_category"].items():
        print(f"    {cat:<11} {c['found']} of {c['labelled']}")
    print(
        f"  trials: {r['trials_found']} of {r['trials_labelled']} found, "
        f"{len(r['trial_false_alarms'])} false alarm(s)"
    )
    if r["missed"] or r["trials_missed"]:
        print(f"  missed: {', '.join(r['missed'] + r['trials_missed'])}")
    if r["false_alarms"] or r["trial_false_alarms"]:
        print(f"  false alarms: {', '.join(r['false_alarms'] + r['trial_false_alarms'])}")


def synthetic(args) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        labels = generate(Path(tmp), SYNTHETIC_TODAY, seed=args.seed)
        events, read, secs = scan_folder(Path(tmp))
    truth = [dict(row, merchant=row["key"]) for row in labels]
    result = score(detect(events, SYNTHETIC_TODAY), truth)
    result.update(messages=read, seed=args.seed, today=SYNTHETIC_TODAY.isoformat())
    show(
        f"SYNTHETIC data (seed {args.seed}, {read} messages): a regression gate, not real accuracy",
        result,
    )
    if args.write:
        write_labels(labels, HERE / "synthetic" / "labels.csv")
        (HERE / "synthetic" / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    baseline_path = HERE / "synthetic" / "baseline.json"
    if args.update_baseline:
        baseline_path.write_text(json.dumps({m: result[m] for m in METRICS}, indent=2) + "\n")
    if args.gate:
        base = json.loads(baseline_path.read_text())
        worse = [m for m in METRICS if result[m] < base[m]]
        if worse:
            print(f"\nGATE FAILED: {', '.join(f'{m} {result[m]} < {base[m]}' for m in worse)}")
            return 1
        print("\ngate passed (no metric below eval/synthetic/baseline.json)")
    return 0


def bench(args) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp), SYNTHETIC_TODAY, seed=args.seed, noise=args.bench)
        mbox = Path(tmp) / MBOX_NAME
        size = mbox.stat().st_size
        started = time.perf_counter()
        _, read = scan_file(mbox)
        secs = time.perf_counter() - started
    print(
        f"scanned {read} synthetic emails ({size / 2**20:.0f} MB) in {secs:.1f} s: "
        f"{read / secs:,.0f} messages/s"
    )
    return 0


def _salt() -> bytes:
    home = Path(os.environ.get("SUBWATCH_HOME") or Path.home() / ".subwatch")
    path = home / "salt"
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(16))
    return bytes.fromhex(path.read_text().strip())


def anon(name: str) -> str:
    """Well-known services keep their name; anything else (UPI payees, shops) becomes a salted
    hash. The salt never leaves this machine, so the hash can't be reversed with a name list."""
    if key := known_merchant(name):
        return key
    digest = hmac.new(_salt(), normalise(name).encode(), hashlib.sha256).hexdigest()
    return f"merchant_{digest[:8]}"


def read_truth(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    for r in rows:
        r["active"] = r.get("active", "").lower()
        r["kind"] = r.get("kind", "").lower() or "sub"
    return [r for r in rows if r.get("merchant")]


def anonymise_truth(args) -> int:
    rows = read_truth(Path(args.anonymise_truth))
    out = HERE / "real" / "truth.anon.csv"
    cols = ["merchant", "amount_rupees", "period", "category", "active", "kind", "frozen_on"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    **{c: r.get(c, "") for c in cols},
                    "merchant": anon(r["merchant"]),
                    "frozen_on": date.today().isoformat(),
                }
            )
    print(f"wrote {out} ({len(rows)} rows, notes dropped, unknown payees hashed)")
    return 0


def real(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    events, read, secs = scan_folder(Path(args.real))
    truth = read_truth(Path(args.truth))
    result = score(detect(events, today), truth)
    for k in ("false_alarms", "missed", "trial_false_alarms", "trials_missed"):
        result[k] = [anon(m) for m in result[k]]
    result.update(messages=read, scan_seconds=round(secs, 1), today=today.isoformat())
    show(f"NAMAN'S OWN DATA, anonymised ({read} messages, as of {today})", result)
    out = HERE / "real" / "results.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nwrote {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--gate", action="store_true", help="fail if below eval/synthetic/baseline.json"
    )
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--write", action="store_true", help="write labels.csv and results.json")
    ap.add_argument("--bench", type=int, metavar="N", help="time a scan of N extra emails")
    ap.add_argument("--anonymise-truth", metavar="CSV")
    ap.add_argument("--real", metavar="FOLDER")
    ap.add_argument("--truth", metavar="CSV")
    ap.add_argument("--today", metavar="YYYY-MM-DD")
    args = ap.parse_args()
    if args.bench:
        return bench(args)
    if args.anonymise_truth:
        return anonymise_truth(args)
    if args.real:
        if not args.truth:
            ap.error("--real needs --truth (your private truth.csv)")
        return real(args)
    if args.synthetic:
        return synthetic(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
