# Gates: leaf-2.6-runbook

Scope: RUN-AND-TEST.md documents SC publish / Friday apply / Track B flags

- [x] G1: SC section present
  CHECK: rg -n 'curated basket|SC publish|/explore|cb-eod-metrics' RUN-AND-TEST.md | head -3
  EXPECT: /
  EVIDENCE: yes — RUN-AND-TEST.md §8 (seed/managers, /explore, cb-eod-metrics, SC publish, Friday desk apply, Track B false, DRY_RUN)

<!-- integrity: security, performance, memory, accuracy required -->
