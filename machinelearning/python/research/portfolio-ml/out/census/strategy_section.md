## 8. Outcome census (PM0.11)

PRICE RETURN ONLY: dividends are sparse in the data, so every figure excludes them. Entry is the next session's open after the first session of each quarter; thresholds are tested on adjusted closes. A dead name's path is variant a (last close carried) or b (zero 60 sessions after its last bar; merged names always a). A live name with no bars to the endpoint, and every name in a cohort whose window runs past the data, counts as unknown. Market cap and sector breakdowns wait for the crawl.

Standard universe, variant (b). `out/census/` holds every cohort, both variants and the no-floor pass.

**Base rates, first touch on closes:**

| Horizon | Threshold | Cohorts | Pooled | Equal-cohort | Median cohort | Worst to best cohort | Held at end, pooled |
|---|---|---|---|---|---|---|---|
| 1y | 1.5× | 54 | 32.3% | 32.7% | 27.2% | 6.2% to 87.6% | 20.1% |
| 1y | 2× | 54 | 12.5% | 12.6% | 8.1% | 0.4% to 56.1% | 8.1% |
| 1y | 3× | 54 | 2.8% | 2.9% | 1.4% | 0.0% to 19.3% | 1.8% |
| 1y | 5× | 54 | 0.4% | 0.4% | 0.2% | 0.0% to 3.1% | 0.2% |
| 1y | 10× | 54 | 0.0% | 0.0% | 0.0% | 0.0% to 0.3% | 0.0% |
| 2y | 1.5× | 50 | 52.7% | 53.8% | 52.2% | 11.2% to 94.0% | 32.4% |
| 2y | 2× | 50 | 28.5% | 28.8% | 25.2% | 2.3% to 78.5% | 17.2% |
| 2y | 3× | 50 | 10.5% | 10.6% | 7.5% | 0.4% to 45.3% | 5.9% |
| 2y | 5× | 50 | 2.5% | 2.5% | 1.3% | 0.0% to 16.0% | 1.4% |
| 2y | 10× | 50 | 0.3% | 0.3% | 0.0% | 0.0% to 2.1% | 0.1% |
| 3y | 1.5× | 46 | 66.3% | 66.1% | 68.5% | 18.2% to 95.2% | 43.5% |
| 3y | 2× | 46 | 43.3% | 42.2% | 40.7% | 9.9% to 82.3% | 27.4% |
| 3y | 3× | 46 | 20.2% | 19.2% | 16.8% | 3.0% to 53.9% | 12.1% |
| 3y | 5× | 46 | 6.0% | 5.5% | 4.1% | 0.2% to 20.3% | 3.5% |
| 3y | 10× | 46 | 1.0% | 0.8% | 0.6% | 0.0% to 3.1% | 0.5% |
| 5y | 1.5× | 38 | 76.0% | 76.5% | 76.0% | 48.9% to 98.1% | 52.2% |
| 5y | 2× | 38 | 57.1% | 57.2% | 55.4% | 29.1% to 92.1% | 37.8% |
| 5y | 3× | 38 | 33.2% | 32.9% | 30.2% | 14.7% to 75.9% | 20.6% |
| 5y | 5× | 38 | 13.8% | 13.4% | 10.6% | 4.2% to 45.3% | 8.1% |
| 5y | 10× | 38 | 3.5% | 3.2% | 1.8% | 0.0% to 15.3% | 1.9% |

**Halved per doubled, 12-month horizon, by cohort year:**

| Year | Resolved | Halved at end | Doubled at end | Halved per doubled | Halved per 2× touch |
|---|---|---|---|---|---|
| 2012 | 534 | 76 | 5 | 15.20 | 9.50 |
| 2013 | 1052 | 54 | 88 | 0.61 | 0.43 |
| 2014 | 1638 | 75 | 187 | 0.40 | 0.27 |
| 2015 | 1620 | 75 | 49 | 1.53 | 0.95 |
| 2016 | 1855 | 47 | 108 | 0.44 | 0.29 |
| 2017 | 2136 | 145 | 116 | 1.25 | 0.59 |
| 2018 | 2307 | 325 | 8 | 40.62 | 11.21 |
| 2019 | 1843 | 344 | 30 | 11.47 | 7.17 |
| 2020 | 1996 | 15 | 597 | 0.03 | 0.02 |
| 2021 | 3162 | 68 | 330 | 0.21 | 0.12 |
| 2022 | 3147 | 85 | 233 | 0.36 | 0.28 |
| 2023 | 3682 | 14 | 751 | 0.02 | 0.01 |
| 2024 | 4514 | 109 | 156 | 0.70 | 0.27 |
| 2025 | 4441 | 151 | 101 | 1.50 | 0.77 |

**Concentration of touch winners:**

| Horizon | Threshold | Winning rows | Distinct companies | Top 20 companies' share | Top start year | Its share |
|---|---|---|---|---|---|---|
| 1y | 1.5× | 10944 | 1329 | 4.6% | 2023 | 19.2% |
| 1y | 2× | 4228 | 997 | 6.4% | 2023 | 24.1% |
| 1y | 3× | 964 | 424 | 12.7% | 2023 | 27.9% |
| 1y | 5× | 121 | 83 | 42.1% | 2020 | 34.7% |
| 1y | 10× | 7 | 6 | 100.0% | 2020 | 42.9% |
| 2y | 1.5× | 15529 | 1330 | 4.7% | 2023 | 17.0% |
| 2y | 2× | 8415 | 1124 | 5.6% | 2023 | 20.1% |
| 2y | 3× | 3105 | 729 | 8.8% | 2023 | 21.6% |
| 2y | 5× | 734 | 283 | 20.2% | 2020 | 24.0% |
| 2y | 10× | 78 | 44 | 69.2% | 2020 | 30.8% |
| 3y | 1.5× | 16562 | 1273 | 4.8% | 2023 | 16.8% |
| 3y | 2× | 10805 | 1124 | 5.9% | 2023 | 17.4% |
| 3y | 3× | 5043 | 831 | 7.8% | 2021 | 18.9% |
| 3y | 5× | 1490 | 388 | 15.4% | 2021 | 22.4% |
| 3y | 10× | 239 | 84 | 53.6% | 2022 | 26.4% |
| 5y | 1.5× | 13779 | 1084 | 5.5% | 2021 | 19.3% |
| 5y | 2× | 10351 | 1000 | 6.6% | 2021 | 20.6% |
| 5y | 3× | 6022 | 818 | 9.2% | 2021 | 22.4% |
| 5y | 5× | 2506 | 531 | 14.4% | 2020 | 27.6% |
| 5y | 10× | 629 | 195 | 30.4% | 2020 | 33.9% |

**Data caveat:** adjusted closes can still hold an unadjusted corporate action. Share of each group's windows with a one-session move beyond 60 %, 12-month horizon:

| Group | Rows | With such a move |
|---|---|---|
| all | 33927 | 0.3% |
| ended -25% | 6755 | 1.2% |
| ended -50% | 1583 | 4.9% |
| ended -75% | 338 | 13.9% |
| touched 1.5x | 10944 | 0.4% |
| touched 10x | 7 | 14.3% |
| touched 2x | 4228 | 0.6% |
| touched 3x | 964 | 1.7% |
| touched 5x | 121 | 5.0% |
