import Link from "next/link";

import { BrandMark } from "@/components/auth/BrandLogo";

import { MobileMenu } from "./MobileMenu";
import { CtaLink } from "./parts";

const LINKS = [
  { href: "#product", label: "Product" },
  { href: "#outcomes", label: "Use Cases" },
  { href: "#why-us", label: "Why Us" },
] as const;

export function MarketingNav({ startHref }: { startHref: string }) {
  return (
    <header className="mk-nav-wrap">
      <a href="#main" className="mk-skip">
        Skip to content
      </a>
      <div className="mk-nav">
        <Link href="/" className="mk-logo" aria-label="Score Smith home">
          <BrandMark className="mk-logo-mark" />
          <span>
            <span className="mk-logo-name">Score Smith</span>
            <span className="mk-logo-sub">Designer &amp; Evaluator</span>
          </span>
        </Link>
        <nav className="mk-nav-links" aria-label="Primary">
          {LINKS.map((l) => (
            <a key={l.href} href={l.href}>
              {l.label}
            </a>
          ))}
        </nav>
        <div className="mk-nav-actions">
          <Link href="/login" className="mk-signin mk-signin-desktop">
            Sign in
          </Link>
          <CtaLink href={startHref}>Get Started</CtaLink>
          <MobileMenu links={LINKS} startHref={startHref} />
        </div>
      </div>
    </header>
  );
}
