# Gates: 1.1.2 Which desk source is authoritative

Scope: the in-repo subtree `kite-momentum-rebalancer/` and the external
`/Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer` differ in 58 source files.
Every port leaf needs to know which one is the live desk's truth. READ ONLY on both.

- [x] G1: The full 58-file divergence is enumerated, not sampled — count stated and matching
  CHECK: diff -rq /Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer/app /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer/app 2>/dev/null | grep -v __pycache__ | wc -l
  EXPECT: /[0-9]+/
  EVIDENCE: 58

- [x] G2: For every differing file, the direction of divergence is classified: external-ahead,
      subtree-ahead, or genuinely forked. Table in the report.
  CHECK: grep -c "external-ahead\|subtree-ahead\|forked" docs/DESK-SOURCE-RECONCILIATION.md
  EXPECT: /[1-9][0-9]*/
  EVIDENCE: 68

- [x] G3: The behavioural deltas that matter for execution are called out by name, with the
      commit that introduced them (at minimum: market-order protection band, daily loss cap
      measured against NAV)
  CHECK: grep -ci "protection band" docs/DESK-SOURCE-RECONCILIATION.md
  EXPECT: /[1-9]/
  EVIDENCE: 8

- [x] G4: A recommendation states which copy the port leaves must read from, with reasoning
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c "Structure from the subtree. The 25 Aug behaviours from external" docs/DESK-SOURCE-RECONCILIATION.md
  EXPECT: /[1-9]/
  EVIDENCE: 1

- [x] G5: Neither source tree was modified
  NOTE (criterion scoped, per CLAUDE.md Autonomy charter precedence rule 5): the subtree carries
  ONE uncommitted change that pre-dates this leaf — ` M kite-momentum-rebalancer/.gitignore`,
  mtime `Aug 26 17:06:32 2026`, five days before this session, and present in the git-status
  snapshot taken at session start. It adds `data/.kite_token.json.*` and `data/*.key` to the
  ignore list. This leaf did not make it and must not revert it (reverting would itself be a
  modification of a read-only tree). The check below therefore excludes exactly that one path and
  additionally asserts the EXTERNAL tree is clean, which the original check did not cover at all.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && { git status --porcelain kite-momentum-rebalancer | grep -v '^ M kite-momentum-rebalancer/\.gitignore$'; git -C /Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer status --porcelain; } | wc -l
  EXPECT: /^\s*0\s*$/
  EVIDENCE: 0
