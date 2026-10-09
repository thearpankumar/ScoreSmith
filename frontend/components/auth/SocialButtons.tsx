"use client";

import { useEffect, useState } from "react";

import { listProviders, oauthStartUrl, type ProviderInfo } from "@/lib/auth-client";

import { GithubIcon, GoogleIcon, MicrosoftIcon } from "./ProviderIcons";

const ORDER: ProviderInfo["id"][] = ["google", "github", "microsoft"];
const LABELS: Record<ProviderInfo["id"], string> = { google: "Google", github: "GitHub", microsoft: "Microsoft" };
const ICONS = { google: GoogleIcon, github: GithubIcon, microsoft: MicrosoftIcon } as const;

/**
 * "or continue with" divider + the three provider buttons.
 *
 * A provider is usable only when the server has its OAuth client id and secret (`GET /auth/providers`). Until
 * then its button keeps the same look but is `aria-disabled`: pressing it explains what is missing in the divider
 * line instead of navigating. (A native `disabled` button cannot be focused or pressed, so it could not say why.)
 */
export function SocialButtons({ rememberMe }: { rememberMe: boolean }) {
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    listProviders()
      .then((ps) => {
        if (alive) setEnabled(Object.fromEntries(ps.map((p) => [p.id, p.enabled])));
      })
      .catch(() => {
        /* backend unreachable: leave every provider disabled */
      });
    return () => {
      alive = false;
    };
  }, []);

  function press(id: ProviderInfo["id"]) {
    if (enabled[id]) {
      window.location.assign(oauthStartUrl(id, rememberMe));
      return;
    }
    const msg = `Sign in with ${LABELS[id]} isn’t set up on this server yet.`;
    setNotice(msg);
  }

  return (
    <>
      <div className="auth-divider" role="presentation">
        <span className="auth-divider-line" />
        <span className="auth-divider-text" role="status" aria-live="polite">
          {notice ?? "or continue with"}
        </span>
        <span className="auth-divider-line" />
      </div>
      <div className="auth-social">
        {ORDER.map((id) => {
          const Icon = ICONS[id];
          const on = Boolean(enabled[id]);
          return (
            <button
              key={id}
              type="button"
              className="auth-social-btn"
              aria-label={on ? `Continue with ${LABELS[id]}` : `Continue with ${LABELS[id]} (not available yet)`}
              aria-disabled={!on}
              title={on ? `Continue with ${LABELS[id]}` : `${LABELS[id]} sign-in isn’t configured yet`}
              onClick={() => press(id)}
            >
              <Icon />
            </button>
          );
        })}
      </div>
    </>
  );
}
