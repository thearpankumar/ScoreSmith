import { Badge } from "@/components/ui/badge";
import type { ScorecardStatus } from "@/lib/types";

const VARIANT: Record<ScorecardStatus, "default" | "soft" | "muted"> = {
  published: "default",
  draft: "soft",
  archived: "muted",
};

const LABEL: Record<ScorecardStatus, string> = {
  published: "Published",
  draft: "Draft",
  archived: "Archived",
};

export function ScorecardStatusBadge({ status }: { status: ScorecardStatus }) {
  return <Badge variant={VARIANT[status]}>{LABEL[status]}</Badge>;
}
