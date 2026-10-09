import type { ReactNode } from "react";

import { BackgroundLines } from "./BackgroundLines";
import { BrandLogo } from "./BrandLogo";
import { HeroCopy } from "./HeroCopy";
import { IsoBars } from "./IsoBars";
import { ScaledStage } from "./ScaledStage";

/**
 * Shared frame of every sign-in page (login, signup, forgot / reset password, verify e-mail): the cream-and-gold backdrop,
 * brand, hero copy and golden glass bars from the design, with the page's own card passed as `children`.
 *
 * Desktop (>= 1100px wide): a fixed 1672x941 canvas scaled to fit the viewport. Narrower: the same pieces stacked
 * (brand, a compact headline, the card). See `app/(auth)/auth.css`.
 */
export function AuthScene({ children }: { children: ReactNode }) {
  return (
    <ScaledStage>
      <div className="auth-glow" aria-hidden="true" />
      <div className="auth-stage">
        <BackgroundLines />
        <IsoBars layer="back" />
        <BrandLogo />
        <HeroCopy />
        {children}
        <IsoBars layer="front" />
      </div>
    </ScaledStage>
  );
}
