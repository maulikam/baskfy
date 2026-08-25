# Gates: 1.4.2 Share card + export nested

Scope: share-card.ts, share-button.tsx, export-button.tsx

- [x] G1: share card supports story and square
  CHECK: rg -n "story:|square:" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/lib/screens/share-card.ts
  EXPECT: story:
  EVIDENCE: 31:  story: { w: 1080, h: 1920 }, | 32:  square: { w: 1080, h: 1080 },

- [x] G2: share card bakes a disclaimer string
  CHECK: rg -n "Not investment advice|SEBI" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/lib/screens/share-card.ts
  EXPECT: SEBI
  EVIDENCE: 28:  "Not investment advice. Past performance is not indicative of future results. SEBI-registered advisers only.";

- [x] G3: copy image + download exist
  CHECK: rg -n "share-copy|share-download" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/share-button.tsx
  EXPECT: share-copy
  EVIDENCE: 208:              data-testid="share-copy" | 224:              data-testid="share-download"

- [x] G4: CSV export is nested in a menu (not a peer primary)
  CHECK: rg -n "DropdownMenu|export-csv" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/apps/web/src/components/screens/export-button.tsx
  EXPECT: DropdownMenu
  EVIDENCE: 120:        </DropdownMenuContent> | 121:      </DropdownMenu>
