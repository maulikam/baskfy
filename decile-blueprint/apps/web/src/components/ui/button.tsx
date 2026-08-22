import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

/**
 * docs/08 §"Design principles": "one accent for primary actions". Exactly one variant uses it.
 * Every variant keeps a visible focus ring (docs/08 §"Accessibility & quality bar") and animates
 * only colour — docs/08 §Motion: "<=150 ms, transform/opacity only".
 *
 * ## M36
 *
 * `primary` is `DESIGN.md`'s orange at its exact brand value, with **coffee ink on it rather than
 * white**: white on `#ff4f00` measures 3.30:1 and the ink measures 5.40:1. The brand colour is
 * kept and the label is legible, which the obvious pairing does not manage.
 *
 * `secondary` is the ink fill `DESIGN.md` names, so the two together give the light/dark button
 * pair the system is built around rather than two greys.
 *
 * Every variant now presses. `active:translate-y-px` costs one compositor property and is the
 * difference between a control that acknowledges a click and one that appears not to have
 * registered it.
 */
const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold",
    "transition-[background-color,border-color,color,box-shadow,transform] duration-150",
    "active:translate-y-px disabled:pointer-events-none disabled:opacity-50",
    "[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  ],
  {
    variants: {
      variant: {
        primary: "bg-brand text-brand-foreground hover:brightness-[0.94]",
        secondary: "bg-foreground text-background hover:brightness-125",
        outline: "border border-input bg-background hover:border-foreground/60 hover:bg-muted",
        ghost: "hover:bg-muted",
        destructive: "bg-negative text-background hover:brightness-110",
        link: "text-accent underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 rounded-sm px-3 text-xs",
        md: "h-9 px-4",
        lg: "h-11 px-6 text-base",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export function Button({
  className,
  variant,
  size,
  asChild = false,
  type = "button",
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={cn(buttonVariants({ variant, size }), className)}
      {...(asChild ? {} : { type })}
      {...props}
    />
  );
}

export { buttonVariants };
