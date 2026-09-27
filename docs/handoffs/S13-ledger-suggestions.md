# S13 — Optional Jev ledger suggestions

Status: Local implementation complete with the feature off; continued to S14 by the developer's explicit S18 instruction.

`0003_ledger_suggestions.sql` adds per-owner UTC-day attempt budgets and decision audit events. `cloudflare_ledger_suggestions.py` validates draft, category and target ownership, caps candidate reads, validates typed Jev output, returns only optional hints and records decisions without writing an expense. Amount/date/currency context limits duplicate candidates to the same amount/currency within seven days. Budget and decision writes recheck the saved Cookie/key in their D1 batch. `cloudflare_jev_gateway.py` uses a fixed endpoint, 3-second timeout and a 64 KiB streamed response cap. Preview and production flags are both off; the provider secret belongs in Cloudflare Secrets.

`tests/test_ledger_suggestions.py` and `test_ledger_jev_gateway.py` passed locally (6 tests). The target Server regression set passed 84 tests after S17 export work. `scripts/evaluate-ledger-suggestions.py` ran on the four synthetic fixture rows: `eligibleForReview=false`; no real anonymized en/zh labeled sample exists. Keep both enable/evaluated flags off until at least 15 samples in each language meet the documented accuracy and false-prompt thresholds. Real Jev and Cloudflare calls were not run; CI status unavailable locally.

Next: S14 Web review UI, then S17 must verify the off/failure path and the Worker FFI gateway in local workerd. Do not enable suggestions from synthetic evaluation alone.
