# Gates: F3-0 — design, decisions, plan

- [x] D1: DECISIONS-FO carries M.5 (Maulik's three answers, verbatim), the F3 sections exist in 01/03/04/06, QUESTIONS.md lists every F3 number with its default
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c '^## M.5' docs/fno/DECISIONS-FO.md; grep -c 'F3' docs/fno/01-method.md; grep -c '§11' docs/fno/04-business-rules.md; grep -c 'F3' docs/fno/QUESTIONS.md
  EXPECT: /^1\n[1-9]/m
  EVIDENCE: 1 | 3

- [x] D2: the plan's contract names the sleeves, flags, structure, kinds, exit reasons and data, and every leaf has a gates file
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && ls gates/f3-*.md | wc -l | tr -d ' '
  EXPECT: 7
  EVIDENCE: 7
