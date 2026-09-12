# Gates: 7.1.3 The dead-column policy is wired, and what it hid is disclosed

Scope: `components/screens/results-panel.tsx` and its tests.

**Written 12 Sep 2026, after the work.** `gates/node-7.1.md` G1 has always named three leaves and
only two gate files existed; `leaf-7.1.3-dead-columns.md` was referenced and never written, so the
node's own check could not run. The work itself did land — under the id `screen-leaf-1.1.2`, whose
gate file covers the same `results-panel.tsx` wiring. This file is the missing third leaf stated in
this tree's numbering, with checks that can fail.

That last clause is the reason it is a new file rather than a pointer. Every check in
`gates/screen-leaf-1.1.2.md` is built on `rg`, which is not on `PATH` in the `/bin/sh` that
`gate-check` spawns, and its EXPECTs are substrings of their own evidence — `EXPECT: rows` against
a line containing the word `rows`, and an `|| echo NONE` whose `NONE` is produced by ripgrep being
missing rather than by the condition holding. Those gates are green and prove nothing.

7.1.2 built the policy as a pure function (`visibleResultColumns` / `suppressedResultColumns`).
This leaf is the other half: the panel has to actually *call* it with the rows, and has to tell the
reader what it removed. A column dropped silently is the same defect as a column of em dashes,
moved somewhere the reader cannot see it.

- [x] G1: `buildColumns` is called with the rows, so the policy runs at all. Called without them it
      falls back to the §2.2 diet and every dead column survives.
  CHECK: cd decile-blueprint/apps/web && node -e 'const s=require("fs").readFileSync("src/components/screens/results-panel.tsx","utf8");const m=s.match(/buildColumns\(([^;]*?)\)\s*,/s);console.log("buildColumns-receives-rows="+(m&&/\brows\b/.test(m[1])))'
  EXPECT: buildColumns-receives-rows=true
  EVIDENCE: buildColumns-receives-rows=true

- [x] G2: A Price column that is null in every row is dropped from the header, and the drop is
      disclosed rather than silent.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/results-panel.test.tsx -t "dead columns" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:29 | Duration  2.84s (transform 327ms, setup 118ms, collect 780ms, tests 552ms, environment 920ms, prepare 65ms)

- [x] G3: A column with data is kept — the policy drops what is empty, not what is unfamiliar.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/results-panel.test.tsx -t "keeps the Price header" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:32 | Duration  2.57s (transform 329ms, setup 189ms, collect 729ms, tests 301ms, environment 960ms, prepare 86ms)

- [x] G4: The disclosure names only columns dropped for lack of data, never the ones the §2.2 diet
      removed by design — otherwise the note reads as a data problem the reader cannot fix.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/results-panel.test.tsx -t "names every empty requested column" 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:12:36 | Duration  2.49s (transform 307ms, setup 124ms, collect 706ms, tests 346ms, environment 955ms, prepare 152ms)

- [x] G5: The disclosure is announced, not just painted: it carries `aria-live` so a reader who
      changed a filter hears which column went away.
  CHECK: cd decile-blueprint/apps/web && node -e 'const s=require("fs").readFileSync("src/components/screens/results-panel.tsx","utf8");const i=s.indexOf("suppressed-columns");const w=s.slice(i-200,i+200);console.log("suppression-is-announced="+/aria-live/.test(w))'
  EXPECT: suppression-is-announced=true
  EVIDENCE: suppression-is-announced=true

- [x] G6: The whole results-panel suite is green, because this wiring sits in the middle of it.
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/results-panel.test.tsx 2>&1 | tail -8
  EXPECT: /Tests +[0-9]+ passed/
  EVIDENCE: Start at  01:01:58 | Duration  950ms (transform 119ms, setup 51ms, collect 249ms, tests 196ms, environment 256ms, prepare 40ms)
