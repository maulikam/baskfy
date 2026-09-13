"use client";

import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Name and delete for a screen. Templates can be renamed in this field — the editor persists
 * that by duplicating into an owned copy. Delete is only offered when the caller owns the row.
 */
export function ScreenIdentity({
  name,
  onNameChange,
  canEdit,
  canDelete,
  onDelete,
}: {
  name: string;
  onNameChange: (name: string) => void;
  canEdit: boolean;
  canDelete: boolean;
  onDelete?: () => void;
}) {
  return (
    <div className="min-w-0 space-y-2">
      <p className="vaaya-eyebrow">Your screen</p>
      {canEdit ? (
        <input
          id="screen-name"
          data-testid="screen-name"
          aria-label="Screen name"
          value={name}
          onChange={(event) => onNameChange(event.target.value)}
          className="vaaya-display w-full max-w-xl truncate border-0 bg-transparent p-0 text-3xl outline-none ring-0 sm:text-4xl"
        />
      ) : (
        <h1 className="vaaya-display truncate text-3xl sm:text-4xl">{name}</h1>
      )}
      {canDelete && onDelete ? (
        <div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-negative hover:bg-negative-muted"
            data-testid="delete-screen"
            onClick={onDelete}
          >
            <Trash2 aria-hidden="true" />
            Delete screen
          </Button>
        </div>
      ) : null}
    </div>
  );
}
