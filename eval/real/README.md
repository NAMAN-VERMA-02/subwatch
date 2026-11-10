# Real data (Naman's own, anonymised)

First run: 2026-11-10 (see the main README for results). The steps were:

1. Write the ground truth **before** any detector run, from sources subwatch never reads:
   UPI AutoPay/mandate lists, card standing instructions, Play Store subscriptions, memory.
2. `python eval/run.py --anonymise-truth path/to/private/truth.csv` writes `truth.anon.csv`
   here, dated. Well-known services keep their names; every other payee becomes a salted hash
   (the salt stays on the machine); private notes are dropped. Commit it before step 3.
3. `python eval/run.py --real path/to/exports --truth path/to/private/truth.csv` runs once and
   writes `results.json` (counts and anonymised names only, never message text).

Any change to the detector after that run is reported as "after tuning".
