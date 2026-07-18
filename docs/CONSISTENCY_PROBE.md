# Consistency Probe — Findings & Fixes

**Question probed:** *"Why are users from Texas underspending compared to California?
Dig into the drivers."* — run repeatedly in fresh threads (no shared conversation
state), `google/gemini-3.5-flash` via OpenRouter, temperature 0.1, live BigQuery.

Three batches on 2026-07-17: 5 runs (baseline) → prompt fixes → 5 runs → graph fixes →
2-run spot check.

## What was consistent (all 12 runs)

- **Methodology.** Every run's primary comparison used the golden-trio conventions:
  `LEFT JOIN` from `users` (never-purchasers count in the denominator) and
  Cancelled/Returned orders excluded. This was the biggest pre-registered risk — an
  INNER-vs-LEFT join flip changes revenue-per-customer enough to flip the verdict —
  and it never happened. Retrieval-injected examples effectively pin the methodology.
- **Numbers.** Within a batch, core metrics were identical across runs to the cent
  (e.g. revenue/customer CA $83.21 vs TX $81.09; 0.94 orders/customer and 1.46
  items/order in both states; conversion 66.1% both).
- **Verdict.** All runs agreed: the per-customer gap is small and driven by average
  item price / product mix (concentrated in female-shopper categories), not by
  engagement; the *total* revenue gap is customer-volume driven.
- Drill-down **emphasis** varied run-to-run (female-shopper gap vs category pricing vs
  acquisition-volume framing) — acceptable analyst-style variance over the same
  numbers, not contradiction.

## Defects found and fixed

| # | Defect | Evidence | Fix |
|---|---|---|---|
| 1 | **Answer-in-the-report-only.** One run's entire chat reply was "I have saved this report as …" — the analysis existed only inside the saved report body. | batch 1, run 5 | Scaffold rule: the chat reply must always contain the full analysis; never a bare pointer/confirmation. Not observed since. |
| 2 | **Unprompted saves.** 3 of 5 runs saved reports without being asked. | batch 1, runs 3–5 | Scaffold rule: save only on user request or accepted offer. Batch 2 + spot check: zero unprompted saves. |
| 3 | **Hallucinated analysis date.** After asking reports to carry an as-of date, the model invented "October 24, 2023" (it had no access to the real date). | batch 2, all runs | Real date injected into the system prompt at build time. Spot check: correct date rendered. |
| 4 | **Empty final reply.** One run returned a blank assistant message after five successful queries — the turn ended showing the user nothing (transient model failure mode). | batch 2, run 2 | Structural fix in the graph: blank replies get one automatic regeneration (blank turns are also filtered from model input); if still blank, a graceful "please ask again" message is substituted. Covered by 2 new offline tests (36 total). |

## Round 3 (automated eval) findings

Automating the probe as a live eval immediately caught two more issues:

| # | Finding | Fix |
|---|---|---|
| 5 | **Double-blank replies.** One run produced a blank reply, and the single identical retry was *also* blank — at low temperature, an unchanged input tends to reproduce the unchanged failure. The graceful fallback fired (correct last resort), but the answer was lost. | Retry budget raised to two, and each retry now appends a nudge message so the model input *differs* from the attempt that produced the blank. |
| 6 | **Methodology drift is real but subtler than feared.** One run's comparison used CASE-based status filtering and an `active_customers` denominator vocabulary instead of the trio's exact shape (verdict still agreed). Also, runs sometimes lead with a *diagnostic* query (`SELECT DISTINCT state …` to verify filter values) — good behavior that the eval initially misgraded as the primary comparison. | Cohort-comparison methodology (LEFT JOIN from `users`, Cancelled/Returned excluded) promoted from trio-influence to an explicit scaffold rule; eval now identifies the primary comparison as the first *join* query naming both cohorts, and derives revenue-per-customer from raw revenue/count columns rather than trusting the model's column names. |

## Caveat that is *not* a defect

`thelook_ecommerce` is a **continuously regenerated dataset**: on 2026-07-16 Texas led
revenue-per-customer ($80.34 vs $76.59); on 2026-07-17 California led ($83.21 vs
$81.09). Cross-day verdict changes are the ground truth changing, not model
inconsistency — which is why reports now carry their analysis date, and why the QA
design (docs/ARCHITECTURE.md §4.6) compares agent SQL against golden SQL *executed at
the same time* rather than against stored answers.

## Re-running this probe

The probe is automated as a live eval: `uv run pytest evals -v`
(`CONSISTENCY_RUNS=5` for a wider batch). It asserts every property in the findings
table above and writes full transcripts to `evals/output/`. It is deliberately outside
the default offline suite — it hits real BigQuery and the LLM provider.

## Residual variance, honestly stated

At temperature 0.1 the drill-down path still varies between runs (which secondary
breakdowns get queried, which driver leads the headline). The numbers and the verdict
do not. Options if tighter output consistency is ever required: temperature 0, a fixed
comparison playbook in the scaffold, or (production) caching answers per
question-cluster per data snapshot.
