# Repo hygiene status

| Item | State | Commit |
|---|---|---|
| Preflight: 31 dirty entries, 13 worktree rows, 50 merged lines, rs.tag and pipeline.tag `indices` | done | — |
| RH1 root agreements; stale MTF test | done | 42d803c 0c78c3c |
| RH2 shared cargo target | done | d11eb5d |
| 2.2-2.5 worktrees and branches; disk 501 GB to 117 GB used | done | no commit |
| RH3 ignore list, editorconfig, cache untracked | done | 7292774 88d826d |
| RH4 root docs | done | 283a13e |
| 3.3 gate password moved; never committed | done | in RH4 |
| RH5 goldens; 2,419 files parsed, same as before (the 1,702 in the prompt is an older count) | done | e3caae0 |
| RH6 deploy/; stage byte-identical | done | dfc348b 2a0d619 |
| RH7 moves; run-pack depth | done | 65ac8df 51b35a3 |
| RH8 Makefile, fmt, namespace | done | 2efa151 d699b10 79fe153 |
| RH9 Dockerfile; probe builds, label and healthcheck present | done | 57aee1e |
| RH10 CI rust job, cargo deny | done | aaa5cc4 |
| RH11 pre-commit | done | 088fda4 |
| RH12 README, CONTRIBUTING | done | 8dd568d |
| Empty `go/` removal | blocked: a safety check refused it; `rmdir go` by hand | — |
| `make test`: 4,430 pass, 1 fail | blocked: `nothing_in_the_kite_adapter_can_reach_an_order` was already red at 0f31ee1 and is right (Law 2); fixing it changes a binary | — |
| `make test-py`: same 131 failures as baseline, none new | done | — |
| Branch count 9, not 7 | deviation RH-3 | — |

| Worktree | GB by du before removal |
|---|---|
| baskfy-rs8-int | 203 |
| baskfy-rs8-opps | 67 |
| baskfy-rs8-opps-setups | 20 |
| baskfy-rs8-opps-web | 2 |
| baskfy-rs8-r1 | 194 |
| baskfy-rs8-r2 | 196 |
| baskfy-rs8-r3 | 200 |
| baskfy-rs8-r4 | 0 |
| baskfy-rs8-r5 | 192 |
| baskfy-rs8-r6 | 196 |
| baskfy-rs8-r7 | 220 |
| baskfy-sc-run | 1 |
