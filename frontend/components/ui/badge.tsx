import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-ink text-white",
        outline: "border-hairline bg-white/60 text-ink",
        // Lemon used correctly: filled surface + dark ink text, never text-on-white.
        lemon: "border-transparent bg-lemon text-lemon-ink font-semibold",
        soft: "border-transparent bg-lemon-soft text-lemon-ink",
        muted: "border-white/70 bg-white/55 text-ink-muted",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
