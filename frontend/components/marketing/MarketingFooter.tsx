import Link from "next/link";

import { BrandMark } from "@/components/auth/BrandLogo";

export function MarketingFooter({ startHref }: { startHref: string }) {
  return (
    <footer className="mk-footer">
      <div className="mk-footer-inner">
        <Link href="/" className="mk-logo" aria-label="KPI Metrics home">
          <BrandMark className="mk-logo-mark" />
          <span className="mk-logo-name">KPI Metrics</span>
        </Link>
        <nav aria-label="Footer" className="mk-footer-links">
          <a href="#product">Product</a>
          <a href="#outcomes">Use Cases</a>
          <Link href="/login">Sign in</Link>
          <Link href={startHref}>Get Started</Link>
        </nav>
        <p className="mk-copy">&copy; {new Date().getFullYear()} Arpan Kumar. All rights reserved.</p>
      </div>
    </footer>
  );
}
