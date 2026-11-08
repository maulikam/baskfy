import { Badge } from "@/components/ui/badge";
import { stateText, type Tone } from "@/lib/options/view";

const VARIANT: Record<
  Tone,
  "positive" | "negative" | "warning" | "accent" | "neutral"
> = {
  positive: "positive",
  negative: "negative",
  warning: "warning",
  accent: "accent",
  neutral: "neutral",
};

/** `04` §10's state, in words, coloured by what it means for the day. */
export function StateChip({ state }: { state: string }) {
  const { label, tone } = stateText(state);
  return (
    <Badge
      variant={VARIANT[tone]}
      data-testid="options-state"
      data-state={state}
    >
      {label}
    </Badge>
  );
}

/** A clock label from `@/lib/options/view.clockLabel`. */
export function ToneBadge({
  text,
  tone,
  testId,
}: {
  text: string;
  tone: Tone;
  testId?: string;
}) {
  return (
    <Badge
      variant={VARIANT[tone]}
      {...(testId ? { "data-testid": testId } : {})}
    >
      {text}
    </Badge>
  );
}
