# AF run status

Rollback tag: **e795a71** (verify-pc-deploy.sh 12 Sep 2026).
Box: pins=3 running=10 release=e795a71 twt_execution_true=0.
verify-stack.sh local: FAIL (local compose not up) — box verified via verify-pc-deploy.

Notes: `4.11` = brokers `to_thread` (A); live_prices `to_thread` tracked under `4.12` (C). `1.23` = kitchen-sink + openapi (F); orphan `/discover/{featured,plan}` under G `1.24` ownership list.

| item | lane | commit | state |
|---|---|---|---|
| 0.1 | A | — | pending |
| 0.8 | A | — | pending |
| 2.1 | A | — | pending |
| 2.2 | A | — | pending |
| 2.4 | A | — | pending |
| 2.5 | A | — | pending |
| 2.6 | A | — | pending |
| 2.10 | A | — | pending |
| 2.11 | A | — | pending |
| 2.12 | A | — | pending |
| 2.13 | A | — | pending |
| 2.14 | A | — | pending |
| 2.15 | A | — | pending |
| 4.11 | A | — | pending |
| 4.16 | A | — | pending |
| 0.2 | B | — | pending |
| 2.3 | B | — | pending |
| 2.7 | B | — | pending |
| 2.8 | B | — | pending |
| 2.9 | B | — | pending |
| 4.6 | B | — | pending |
| 0.4 | C | — | pending |
| 0.9 | C | — | pending |
| 1.3 | C | — | pending |
| 1.6 | C | — | pending |
| 3.12 | C | — | pending |
| 3.13 | C | — | pending |
| 4.1 | C | — | pending |
| 4.12 | C | — | pending |
| 4.13 | C | — | pending |
| 4.14 | C | — | pending |
| 4.15 | C | — | pending |
| 0.5 | D | — | pending |
| 0.6 | D | — | pending |
| 1.4 | D | — | pending |
| 1.8 | D | — | pending |
| 3.1 | D | — | pending |
| 3.2 | D | — | pending |
| 3.3 | D | — | pending |
| 3.11 | D | — | pending |
| D2 | D | — | pending |
| 0.7 | E | 8cbe278 | done |
| 3.4 | E | 1051e43 | done |
| 3.5 | E | ddbcfbb | done |
| 3.6 | E | 3d5e89c | done |
| 3.7 | E | 9448850 | done |
| 3.8 | E | 89f9964 | done |
| 3.9 | E | 2e91d73 | done |
| 3.10 | E | e1ab53e | done |
| 0.10 | F | — | pending |
| 1.7 | F | — | pending |
| 1.23 | F | — | pending |
| 4.2 | F | — | pending |
| 4.3 | F | — | pending |
| 4.4 | F | — | pending |
| 4.5 | F | — | pending |
| 4.7 | F | — | pending |
| 4.8 | F | — | pending |
| 4.9 | F | — | pending |
| 4.10 | F | — | pending |
| 0.3 | G | — | pending |
| 1.1 | G | — | pending |
| 1.2 | G | — | pending |
| 1.5 | G | — | pending |
| 1.9 | G | — | pending |
| 1.10 | G | — | pending |
| 1.11 | G | — | pending |
| 1.12 | G | — | pending |
| 1.13 | G | — | pending |
| 1.14 | G | — | pending |
| 1.15 | G | — | pending |
| 1.16 | G | — | pending |
| 1.17 | G | — | pending |
| 1.18 | G | — | pending |
| 1.19 | G | — | pending |
| 1.20 | G | — | pending |
| 1.21 | G | — | pending |
| 1.22 | G | — | pending |
| 1.24 | G | — | pending |
| 7 | G | — | pending |
| 5.1 | H | — | pending |
| 5.2 | H | — | pending |
| 5.3 | H | — | pending |
| 5.4 | H | — | pending |
| 5.5 | H | — | pending |
| 5.6 | H | — | pending |
| 5.7 | H | — | pending |
| 5.8 | H | — | pending |
| 5.9 | H | — | pending |
| 5.10 | H | — | pending |
| 5.11 | H | — | pending |
| 5.12 | H | — | pending |
| 5.13 | H | — | pending |
| I.1 | I | — | pending |
| I.2 | I | — | pending |
| I.3 | I | — | pending |
| I.4 | I | — | pending |
| I.5 | I | — | pending |
| I.6 | I | — | pending |
| I.BACKLOG | I | — | pending |

needs services/api/.../api_keys.py,admin.py: is_active(*, now=)
needs services/api/seed.py: build_calendar(..., holidays=)
needs packages/providers/.../fixture_builder.py: build_calendar(..., holidays=)
needs services/worker/.../reconcile_cli.py: read_export(path); drop default_fixture_path
needs services/api/tests/test_csv_export.py: read_export(path); drop default_fixture_path
