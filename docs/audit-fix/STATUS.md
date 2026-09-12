# AF run status

Rollback tag: **e795a71**.
Wave 1: A–G merged into developer (`a4ee64f`).
Post-merge: Law-1 callers (`12c1e1f`), house-rule Any + mypy (`1e1fe08`), `make lint` green (`9c24751`).
Wave 2: H+I merged; D2 after ship / suites green.

| item | lane | commit | state |
|---|---|---|---|
| 0.1 | A | merged | done |
| 0.8 | A | merged | done |
| 2.1 | A | merged | done |
| 2.2 | A | merged | done |
| 2.4 | A | merged | done |
| 2.5 | A | merged | done |
| 2.6 | A | merged | done |
| 2.10 | A | merged | done |
| 2.11 | A | merged | done |
| 2.12 | A | merged | done |
| 2.13 | A | merged | done |
| 2.14 | A | merged | done |
| 2.15 | A | merged | done |
| 4.11 | A | merged | done |
| 4.16 | A | merged | done (owned; other routers deferred) |
| 0.2 | B | merged | done |
| 2.3 | B | merged | done |
| 2.7 | B | merged | done |
| 2.8 | B | merged | done |
| 2.9 | B | merged | done |
| 4.6 | B | merged | done |
| 0.4 | C | merged | done |
| 0.9 | C | merged | done |
| 1.3 | C | merged | done |
| 1.6 | C | merged | done |
| 3.12 | C | merged | done |
| 3.13 | C | merged | done |
| 4.1 | C | merged | done |
| 4.12 | C | merged | done |
| 4.13 | C | merged | done |
| 4.14 | C | merged | done |
| 4.15 | C | merged | done |
| 0.5 | D | merged | done |
| 0.6 | D | merged | done |
| 1.4 | D | merged | done |
| 1.8 | D | merged | done |
| 3.1 | D | merged | done |
| 3.2 | D | merged | done |
| 3.3 | D | merged | done |
| 3.11 | D | merged | done |
| D2 | D | — | wave2 |
| 0.7 | E | merged | done |
| 3.4 | E | merged | done |
| 3.5 | E | merged | done |
| 3.6 | E | merged | done |
| 3.7 | E | merged | done |
| 3.8 | E | merged | done |
| 3.9 | E | merged | done |
| 3.10 | E | merged | done |
| 0.10 | F | merged | done |
| 1.7 | F | merged | done |
| 1.23 | F | merged | done |
| 4.2 | F | merged | done |
| 4.3 | F | merged | done |
| 4.4 | F | merged | done |
| 4.5 | F | merged | done |
| 4.7 | F | merged | done |
| 4.8 | F | merged | done |
| 4.9 | F | merged | done |
| 4.10 | F | merged | done |
| 0.3 | G | merged | done |
| 1.1 | G | merged | done |
| 1.2 | G | merged | done |
| 1.5 | G | merged | done |
| 1.9 | G | merged | done |
| 1.10 | G | merged | done |
| 1.11 | G | merged | done |
| 1.12 | G | merged | done |
| 1.13 | G | merged | done |
| 1.14 | G | merged | done |
| 1.15 | G | merged | done |
| 1.16 | G | merged | done |
| 1.17 | G | merged | done |
| 1.18 | G | merged | done |
| 1.19 | G | merged | done |
| 1.20 | G | merged | done |
| 1.21 | G | merged | done |
| 1.22 | G | merged | done |
| 1.24 | G | merged | done |
| 7 | G | merged | done |
| 5.1 | H | merged | done |
| 5.2 | H | merged | done |
| 5.3 | H | merged | done |
| 5.4 | H | merged | done |
| 5.5 | H | merged | done |
| 5.6 | H | merged | done |
| 5.7 | H | merged | done |
| 5.8 | H | merged | done — explore limit/offset + categories; listings `total` (AF 5.8b) |
| 5.9 | H | merged | done |
| 5.10 | H | merged | done |
| 5.11 | H | merged | done — Versions tab via lane I |
| 5.12 | H | merged | done |
| 5.13 | H | merged | done |
| I.1 | I | merged | done |
| I.2 | I | merged | done; box migration 0045 at D2 |
| I.3 | I | merged | done |
| I.4 | I | merged | done |
| I.5 | I | merged | done |
| I.6 | I | merged | done |
| I.BACKLOG | I | merged | done |
| W.1 | W | merged | done — `f2177c7`; web lint now runs `next typegen` first so it agrees with the build |
| V.1 | V | in flight | desk suite + image build-arg preflight |
| D2 | orch | pending | box data job with backup id + alembic 0045 |
| X.1 | X | merged | done — `83e40ef`: web lint 0 errors, 3268 tests green, openapi/client regenerated |
| O.1 | orch | noted | database suites are serialized; a result taken during an overlap is void (AF O.1) |
| V.1 | V | merged | done — `b02b2ff`/`8b1a132`: desk suite 2030 green; truncated web `docker build` and missing `REVALIDATE_SECRET` fixed |
| T.1 | T | merged | done — `46a9156`: test-db 1848 passed; `make test` and `make lint` green |
| I.7 | orch | merged | done — `77f765f`: version routes filtered on a visibility no row can hold (404 for every basket) |
| SHIP | orch | in flight | `tools/deploy/ship.sh` from HEAD |
