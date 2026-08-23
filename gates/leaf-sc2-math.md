# Gates: SC2 exact catalog math

Scope: Implement every money/return/volatility rule in docs/smallcase/04 §§2–4 as pure exact code.

- [x] G1: Minimum amount and whole-share allocation satisfy the documented property
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA -k 'minimum or allocation or property'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — 5 selected tests passed, including 150 Hypothesis examples, exact
  integer-ratio ceil/floor beyond the Decimal context precision, and no residual redistribution.

- [x] G2: A golden fixture pins point-in-time chain-linked returns across a version change
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA -k 'golden or chain'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — 6 selected tests passed; the golden series pins 102.00 before the
  effective-date switch and 104.550000 after it, with future versions and pre-inception prices
  proven unable to leak backwards.

- [x] G3: Window returns, CAGR, annualized volatility, young-basket fallback, and bucket thresholds match docs/04
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA -k 'window or cagr or volatility or bucket or young'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — 19 selected tests passed: calendar windows, 365.25-day CAGR, age labels,
  ddof=1/sqrt(252), the 59/60-return fallback boundary, trailing 252 cap, exact 15%/25% edges,
  fixed buckets below 12, and stable-id terciles at 12.

- [x] G4: Inputs and outputs use Decimal and storage rounding is explicit and half-up
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q -rA -k 'decimal or rounding or float'
  EXPECT: /PASSED /
  EVIDENCE: 2026-08-23 — 5 selected tests passed; float injection is rejected, money rounds
  half-up to 0.01, and return/volatility values round half-up to the model's 10-decimal scale.

- [x] G5: The core module has no I/O, network, database, ambient clock, or unbounded quadratic loop
  CHECK: cd decile-blueprint && ! rg -n 'sqlalchemy|httpx|requests|Path\(|open\(|datetime\.now|date\.today|socket|for .* in .*for .* in' packages/core/src/baskfy_core/curated_metrics.py && echo pure-bounded
  EXPECT: pure-bounded
  EVIDENCE: 2026-08-23 — the prescribed source scan printed `pure-bounded`; chaining streams
  daily close maps and retains only the previous map plus the required output series.

- [x] G6: Expert reread, defect hunt, performance/memory pass, and free polish find no remaining math defect
  EVIDENCE: 2026-08-23 — four passes complete. Expert reread replaced context-sensitive
  affordability division with exact integer ratios and stopped old baskets falling through to a
  younger headline. Adversarial coverage rejects missing closes, duplicate/non-increasing data,
  datetimes, floats, and incomplete windows. The 8,000-day/4-name chain ran in 0.18s and stayed
  below its 12 MiB auxiliary-memory ceiling. Final results: 31/31 math tests and 39/39 math plus
  no-escape-hatch tests; targeted ruff, format-check, strict mypy, and diff-check all pass.
