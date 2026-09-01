# Gates: D — placeholder markers

Scope: the brief says 31 in `src`. Measured: 0 TODO/FIXME in `apps/web/src`, 1 repo-wide outside
tests. This leaf reports the true measurement and files anything genuinely unfinished.

- [x] D1: The true count is measured and stated, with the pattern used.
  CHECK: cd decile-blueprint/apps/web && rg -c --no-filename "TODO|FIXME" src -g '*.ts' -g '*.tsx' 2>/dev/null | awk '{s+=$1} END{print "WEB_TODO_FIXME="s+0}'
  EXPECT: WEB_TODO_FIXME=0
  EVIDENCE: WEB_TODO_FIXME=0 in apps/web/src (patterns: TODO, FIXME). Repo-wide outside tests, across *.py/*.ts/*.tsx excluding node_modules, frozen and .venv: 1 match total.

- [x] D2: The 9 `not-implemented` hits are shown to be a deliberate union member, not unfinished
      UI — quoted from the source that says so.
  EVIDENCE: Not placeholders. lib/api/search.ts:21 — 'The `not-implemented` outcome is carried over from that module and is not decoration'. lib/api/instruments.ts:10 — 'The `not-implemented` outcome is kept rather than deleted'. It is a member of a discriminated union returned when the endpoint answers 404, rendered by command-palette.tsx:191. The one repo-wide NotImplementedError is baskfy_worker/engine.py:200, a protocol guard whose message directs the caller to `run` instead.

- [x] D3: Anything genuinely unfinished that this sweep does find is filed, not left reading as
      finished UI.
  EVIDENCE: Nothing genuinely unfinished was found to file. The sweep's own numbers are recorded in PLAN-TREE4-INTEGRITY.md so the next reader does not re-derive them: the 31 most likely came from a pattern that also matched 33 `placeholder=` input attributes and 47 uses of the word 'stub' in prose comments.
