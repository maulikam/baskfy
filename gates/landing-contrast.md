# Gates: the landing page's twenty colour-contrast failures

Scope: `decile-blueprint/apps/web/src/app/globals.css` (the token layer), and the three marketing
components that draw on it — `page-sections.tsx`, `site-footer.tsx`, `landing-band.tsx` — plus the
flow stage's eyebrow in `how-it-works-flow.tsx`.

`gates/expired-abandonments.md` X3 voided the "Playwright cannot run here" abandonment on
12 Sep 2026, ran `e2e/accessibility.spec.ts` for real, and found what the abandonment had been
hiding: **`/` fails axe `color-contrast` on 20 elements, in BOTH themes.** Every other surface in
the sweep passed in both themes, so this is one page's palette. This file fixes it and measures it.

```
python3 tools/gates/rerun.py gates/landing-contrast.md
```

## What was actually wrong — one root cause, three faces

Not twenty problems. **Three surfaces that paint their own background and then let the reader's
theme supply the ink.**

| Face | Elements | Why |
|---|---|---|
| The dark bands (`page-sections.tsx`, `site-footer.tsx`) | 12 | Each re-pointed **two** custom properties inline — `--border`, `--muted-foreground` — and left everything else to a theme that is not the one on screen. The quiet ink tier had no token at all, so it was retyped as `#6f6f6f` on `#141414`/`#0a0a0a`. `--muted` was never re-pointed, so the footer's block `<Disclaimer/>` painted the LIGHT `--muted` at half alpha over near-black |
| The ticker rail (`landing-band.tsx`) | 3 (dark only) | The exact mirror: painted `#f8fafc` in both themes, so in dark its `text-muted-foreground` resolved to `#a6a6a6` and landed on a near-white rail |
| `.flow-ack` (`globals.css`) | 7 light / 5 dark | The eyebrow was muted **twice** — `text-muted-foreground` *and* `opacity: 0.7`. `--muted-foreground` is only 5.33:1 to begin with, so there is no dim that survives it |

## The large-text question, answered before assuming (WCAG 1.4.3)

Large text is ≥ 18pt (24px), or ≥ 14pt (18.66px) at bold (700). Measured at their rendered size:

| Element | Rendered | Large text? | Bar |
|---|---|---|---|
| `.flow-ack` caps labels | 12px, weight 500 | no | 4.5:1 |
| footer nav `<h2>` | 11px mono, weight 400 | no | 4.5:1 |
| card subtexts | 13px, weight 400 | no | 4.5:1 |
| `aside > p` (Disclaimer) | 12px (`text-xs`) | no | 4.5:1 |

**None qualifies**, and axe agrees independently: it applies the 3:1 bar itself when an element is
large, and it flagged the footer `<h2>`s at 3.94:1 — which would have passed at 3:1. So the honest
fix was not smaller than it looked. Nothing was changed on the strength of a guess about size.

## Measured ratios, before and after

Ratios are WCAG 2.x arithmetic on the two composited sRGB colours — exact, not sampled. The
*verdict* at each end is axe's, in a real Chromium against a production build: **20 violations in
each theme before, 0 in each theme after.**

| Offender | n | Surface | Before | After | What changed |
|---|---|---|---|---|---|
| card subtexts (`.mt-1.text-[13px]`) | 8 | `#141414` | `#6f6f6f` **3.67** | `#8a8a8a` **5.34** | `text-quiet-foreground` |
| card chip counts (11px mono) | 8 | `#141414` | `#6f6f6f` 3.67 | `#8a8a8a` **5.34** | same token (axe had not flagged these; same colour, same ground — fixed with them) |
| footer nav `<h2>` | 3 | `#0a0a0a` | `#6f6f6f` **3.94** | `#8a8a8a` **5.73** | `text-quiet-foreground` |
| footer copyright `<p>` | 1 | `#0a0a0a` | `#6f6f6f` **3.94** | `#8a8a8a` **5.73** | `text-quiet-foreground` |
| `aside > p` (Disclaimer, light) | 1 | `--muted`/50 over `#0a0a0a` = `#7d7d7d` | `#a6a6a6` **1.69** | `#121212` ground, `#a6a6a6` **7.70** | `.band-dark` re-points `--muted` |
| ticker rows (dark) | 3 | `#f8fafc` | `#a6a6a6` **2.05** | `#686868` **5.33** | `.band-light` re-points `--muted-foreground` |
| `.flow-ack`, on `--background` | — | light `#f8fafc` | `#939494` **2.91** | `#696a6b` **5.18** | rest opacity mutes `--foreground`, not a muted token |
| `.flow-ack`, on `--card` | — | light `#ffffff` | `#959595` **3.00** | `#6c6c6c` **5.25** | ditto |
| `.flow-ack`, on `--background` | — | dark `#0a0a0a` | `#777777` **4.42** | `#989898` **6.86** | ditto |
| `.flow-ack`, on `--card` | — | dark `#141414` | `#7a7a7a` **4.29** | `#9c9c9c` **6.71** | ditto |

**The token changed** is `--quiet-foreground`, new: `#6c6c6c` in `:root` (background 5.02, card
5.25, muted 4.61) and `#8a8a8a` in `.dark` (5.73 / 5.34 / 5.04). It is the fourth ink tier, and it
existed already — as a hex on eight components. Nothing that was legible got darker: the change is
a quarter-step, and the muted tier is untouched.

`.flow-ack`'s rest opacity went 0.7 → 0.6 and its label went `text-muted-foreground` →
`text-foreground`, so the mute is **one** mechanism instead of two. The rendered rest colour is
within a hair of the muted tier it replaces (`#696a6b` vs `#686868` in light), and the
acknowledgment gained range: 0.6 → 1 is a brighter beat than 0.7 → 1 was.

## Deliberately NOT done

- **No token restructure beyond these elements.** `--v-ink-50…900` is a dead ladder (nine tokens,
  zero consumers, no `.dark` half) and is left alone; deleting or theming it is not a contrast fix.
- **`text-[#0a0a0a]` on `landing-band.tsx:71`** stays a hex: it is ink on a translucent white pill
  over a photograph, not a token pairing, and it passes.
- **The light theme's muted→quiet step is small** (5.33 vs 5.02) because AA leaves almost no room
  under `--muted-foreground` on a near-white canvas. That is a real constraint, not an oversight;
  it is asserted as a step rather than widened.

---

- [x] A1: Zero accessibility violations on the landing page in **light**, measured by axe in
      Chromium against a production build. Before this change: **20 `color-contrast` violations.**
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/accessibility.spec.ts -g "landing has no accessibility violations in light" --reporter=list 2>&1 | grep -cE '^\s+✓.*landing has no accessibility violations in light' | tr -d ' '
  EXPECT: 1
  EVIDENCE: 12 Sep 2026 — `1`. Before: `color-contrast (20)` — 7 × `.flow-ack` (`data-flow-node`
    you, market, portfolios + `engine-{screen,basket,organize,plan}`), 8 × card subtext, 3 ×
    `nav[aria-label=…] > h2`, `aside > p`, `.space-y-5 > .text-[13px].text-[#6f6f6f]`. After:
    `✓ 2 [chromium] › e2e/accessibility.spec.ts:49:5 › landing has no accessibility violations in
    light (1.4s)`, 38.8s wall including the build.

- [x] A2: Zero accessibility violations on the landing page in **dark**. Separately asserted,
      because a fix that passes light and fails dark is not a fix — and dark's twenty were not the
      same twenty as light's.
  CHECK: cd decile-blueprint/apps/web && pnpm exec playwright test e2e/accessibility.spec.ts -g "landing has no accessibility violations in dark" --reporter=list 2>&1 | grep -cE '^\s+✓.*landing has no accessibility violations in dark' | tr -d ' '
  EXPECT: 1
  EVIDENCE: 12 Sep 2026 — `1`. Before: `color-contrast (20)`, and the membership differs from
    light's — **3 × `.marquee-track` rows** appear (the ticker rail, 2.05:1, which light never
    fails), `aside > p` does **not** (7.70:1 in dark), and only 5 of the 7 `.flow-ack` labels
    fail. That asymmetry is why both themes are asserted rather than one being taken as proof of
    the other. After: `✓ 3 [chromium] › … in dark (1.3s)`.

- [x] A3: The arithmetic holds without a browser. `--quiet-foreground` clears 4.5:1 on
      `--background`, `--card` **and** `--muted` in all four palettes — `:root`, `.dark`, and the
      two painted bands, which `contrast.test.ts` now parses exactly as it parses a theme. That is
      the 11 Sep lesson applied: a palette invisible to this file gets checked by somebody opening
      a browser.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/contrast.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: `Tests  90 passed (90)` — 66 before this change. The new ones: `--quiet-foreground`
    on three surfaces × two themes, the same four pairs × two bands, the muted-vs-quiet step, and
    "a band matches the theme whose surface it paints, so a band is not a third palette".

- [x] A4: No painted-surface colour is spelled as a hex on a marketing component any more. This is
      the failure mode itself, not a style preference: `#6f6f6f` was a token that was never made
      one, and an inline two-property override is what left `--muted` resolving to the wrong theme.
  CHECK: cd decile-blueprint/apps/web && perl -0777 -pe 's{/\*.*?\*/}{}gs' src/components/marketing/*.tsx | grep -cE '(text|border)-\[#(6f6f6f|8a8a8a|a6a6a6|f7f7f7|262626)\]|bg-\[#(0a0a0a|141414|f8fafc)\]|"--muted-foreground" as string' | tr -d ' '
  EXPECT: 0
  EVIDENCE: `0`. Comments are stripped first, because two of them now quote the old hexes on
    purpose — the record of what broke is worth more than a clean grep.

- [x] A5: The new utilities actually reached the compiled stylesheet. G12 of
      `gates/marketing-flow-responsive.md` exists because a class the Tailwind scanner never saw
      fails no build, no typecheck and no jsdom test — and here it would have failed *silently* in
      the worst possible way: an unstyled `text-quiet-foreground` inherits `--foreground`, which is
      high-contrast, so **axe would have gone green on a fix that never shipped.**
  CHECK: cd decile-blueprint/apps/web && CSS=$(find .next-e2e/static/css -name '*.css' | head -1); for C in '.text-quiet-foreground' '.band-dark' '.band-light' '--quiet-foreground:#6c6c6c' '--quiet-foreground:#8a8a8a' '.flow-ack{opacity:.6'; do grep -q -F -- "$C" "$CSS" || echo "MISSING $C"; done; echo "CHECKED $(basename "$CSS")"
  EXPECT: /^CHECKED [0-9a-f]+\.css$/m
  EVIDENCE: `CHECKED b60e2fbfec43ef0f.css`, nothing missing. Reads the build A1/A2 produce.

- [x] A6: The flow stage is unchanged as a piece of motion design. Same one period, same
      choreography, same `prefers-reduced-motion` behaviour — and `.flow-ack` still rests at its
      muted value under reduced motion rather than jumping to full ink, which `opacity: 1` there
      would now mean.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1; sed -n '/Unlayered on purpose/,$p' src/app/globals.css | grep -cE '^\s*\.flow-(pulse|sweep|ack)' | tr -d ' '
  EXPECT: /Tests +\d+ passed[\s\S]*^3$/m
  EVIDENCE: `Tests  40 passed (40)` and `3` — the second half is G8 of
    `gates/marketing-flow-responsive.md` re-run verbatim, so that gate is still true.

- [x] A7: Typecheck clean and eslint adds nothing. The one warning is pre-existing
      (`react-hooks/incompatible-library` on TanStack's `useReactTable`, `data-table.tsx:349`) and
      eslint does not fail on it.
  CHECK: cd decile-blueprint/apps/web && (pnpm exec tsc --noEmit >/dev/null 2>&1 && echo TSC_CLEAN || echo TSC_DIRTY); pnpm exec eslint . 2>&1 | grep -E '✖ [0-9]+ problem' | tail -1
  EXPECT: /^TSC_CLEAN$[\s\S]*0 errors/m
  EVIDENCE: `TSC_CLEAN` and `✖ 1 problem (0 errors, 1 warning)`.

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
