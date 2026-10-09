import { MessageSquare, BarChart3, ClipboardList, Settings, Users, type LucideIcon } from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

// Chat is the landing experience (signed-in visitors to the public homepage "/" are redirected to /chat by
// middleware.ts), so there is deliberately no separate Home entry.
/** What every signed-in user sees: Charts, Chat and Evaluations - nothing else. */
export const NAV_ITEMS: readonly NavItem[] = [
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/charts", label: "Charts", icon: BarChart3 },
  { href: "/evaluations", label: "Evaluations", icon: ClipboardList },
];

/** Extra entries for administrators (the server and the middleware also block these pages for everyone else). */
export const ADMIN_NAV_ITEMS: readonly NavItem[] = [
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/admin/users", label: "Users", icon: Users },
];

export function navItemsFor(role: string | null | undefined): readonly NavItem[] {
  return role === "admin" ? [...NAV_ITEMS, ...ADMIN_NAV_ITEMS] : NAV_ITEMS;
}
