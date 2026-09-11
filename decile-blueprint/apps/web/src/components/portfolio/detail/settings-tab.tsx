"use client";

import type { ReactNode } from "react";
import { ArrowRight, Lock } from "lucide-react";

import { MetricValue, Panel } from "@/components/portfolio/detail/primitives";
import { Button } from "@/components/ui/button";
import type { SettingRow } from "@/lib/portfolio/detail-tabs";

/**
 * The Settings tab: read, and hand off. It writes nothing, and it cannot.
 *
 * `gates/pc3.md` G7 greps this whole directory for a POST, PATCH, PUT or DELETE and expects none.
 * That is the right check for the right reason: create, rename, assign, move and delete are PC6's
 * management drawer (`components/portfolio/manage/*`, `lib/portfolio/manage.ts`), and two places
 * that can rename a portfolio is one place too many. The second is always the one that forgets a
 * validation rule.
 *
 * So every row here states a fact and says where it comes from, and the one control on the tab
 * opens the drawer the parent passes in:
 *
 * * `manageSlot?: ReactNode` — PC6's drawer, or its trigger.
 * * `onManage?: () => void` — when the parent owns the open state instead.
 *
 * With neither, the button is disabled beside its reason rather than swallowing a click. PC1
 * shipped that defect once and there are four tests holding it there; this leaf is not shipping
 * it a second time.
 *
 * ## Ownership is on every row on purpose
 *
 * A reader who wants to change the benchmark needs to know whether it is theirs to change. "From
 * your broker" and "From the published model" are both answers that mean no, and they mean no for
 * different reasons.
 */

export interface SettingsTabProps {
  rows: readonly SettingRow[];
  executionNote: string;
  manageSlot?: ReactNode;
  onManage?: (() => void) | undefined;
}

export function SettingsTab({ rows, executionNote, manageSlot, onManage }: SettingsTabProps) {
  return (
    <>
      <Panel
        title="How this portfolio is configured"
        blurb="Everything on this tab is read-only. Changing any of it happens in one place, so that two places cannot disagree about the rules."
        testId="settings-configuration"
      >
        <dl className="divide-y divide-border/60">
          {rows.map((row) => (
            <div
              key={row.id}
              data-testid={`setting-${row.id}`}
              className="grid gap-1 px-4 py-3 sm:grid-cols-[12rem_1fr_10rem] sm:items-start sm:gap-4"
            >
              <dt className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
                {row.label}
              </dt>
              <dd className="min-w-0 text-sm">
                <MetricValue metric={row.figure} kind="text" />
              </dd>
              <p className="text-xs text-muted-foreground sm:text-right">{row.owner}</p>
            </div>
          ))}
        </dl>
      </Panel>

      <Panel
        title="Changing any of this"
        blurb="Renaming, moving holdings, adding a sub-portfolio and deleting all live in the management drawer."
        testId="settings-manage"
        actions={
          onManage ? (
            <Button variant="primary" size="sm" onClick={onManage} data-testid="settings-manage-open">
              Open management
              <ArrowRight aria-hidden="true" />
            </Button>
          ) : manageSlot === undefined ? (
            <Button
              variant="primary"
              size="sm"
              disabled
              title="The management drawer is not wired into this page yet."
              data-testid="settings-manage-open"
            >
              Open management
            </Button>
          ) : null
        }
      >
        {manageSlot ?? (
          <div className="space-y-2 px-4 py-4" data-testid="settings-manage-placeholder">
            <p className="flex items-start gap-2 text-sm text-muted-foreground">
              <Lock aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span className="max-w-[76ch] leading-snug">
                The management drawer is a separate surface and is not attached to this page yet.
                Nothing is hidden behind the button above: until the drawer is passed in, there is
                nothing for it to open.
              </span>
            </p>
            <p className="max-w-[76ch] text-xs leading-snug text-muted-foreground">
              When it is attached, it will cover renaming, assigning and moving holdings, and
              sub-portfolios. Permissions, ownership and audit history are not part of it: Baskfy
              is single-tenant today, and those wait on the multi-tenant work.
            </p>
          </div>
        )}
        <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
          {executionNote} Nothing on this tab writes anything, and nothing on this page places an
          order.
        </p>
      </Panel>
    </>
  );
}
