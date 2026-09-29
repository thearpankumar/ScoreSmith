import { MessageSquare, BarChart3, ClipboardList, Settings } from "lucide-react";

// Chat is the landing experience ("/" redirects to /chat — see app/page.tsx), so there
// is deliberately no separate Home entry.
export const NAV_ITEMS = [
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/charts", label: "Charts", icon: BarChart3 },
  { href: "/evaluations", label: "Evaluations", icon: ClipboardList },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;
