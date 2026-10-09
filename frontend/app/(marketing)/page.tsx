import { DashboardMock } from "@/components/marketing/DashboardMock";
import { DemoButton } from "@/components/marketing/DemoDialog";
import { FeatureSection } from "@/components/marketing/FeatureSection";
import { FloatingCards } from "@/components/marketing/FloatingCards";
import { MarketingFooter } from "@/components/marketing/MarketingFooter";
import { MarketingNav } from "@/components/marketing/MarketingNav";
import { CtaLink, Perks, Swoosh } from "@/components/marketing/parts";
import { getAuthConfig } from "@/lib/auth-client";

/**
 * The public homepage. Visitors who already have a session never get here: middleware.ts redirects them to /chat before
 * any HTML is rendered, so the page only ever shows the signed-out calls to action.
 */
export default async function HomePage() {
  const config = await getAuthConfig();
  const startHref = config.signupEnabled ? "/signup" : "/login";
  return (
    <>
      <Swoosh />
      <MarketingNav startHref={startHref} />
      <main id="main">
        <section id="product" className="mk-hero" aria-labelledby="mk-h1">
          <div className="mk-hero-copy">
            <p className="mk-eyebrow">From goals to real impact</p>
            <h1 id="mk-h1" className="mk-h1">
              Design Better KPIs.
              <br />
              Evaluate Smarter.
              <br />
              <span className="mk-gold-text">Drive Real Progress.</span>
            </h1>
            <p className="mk-lead">
              A modern platform to design, track and evaluate KPIs that align teams, improve performance and turn data
              into meaningful outcomes.
            </p>
            <div className="mk-cta-row">
              <CtaLink href={startHref} large>
                Get Started
              </CtaLink>
              <DemoButton />
            </div>
            <Perks />
          </div>
          <div className="mk-hero-art">
            <div className="mk-stage-wrap">
              <div className="mk-stage">
                <DashboardMock />
                <FloatingCards />
              </div>
            </div>
          </div>
        </section>
        <FeatureSection />
      </main>
      <MarketingFooter startHref={startHref} />
    </>
  );
}
