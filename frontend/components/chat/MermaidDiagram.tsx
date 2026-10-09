"use client";

import { useEffect, useId, useRef, useState } from "react";

/**
 * Client-only Mermaid diagram renderer for fenced ```mermaid code blocks
 * inside `MarkdownContent`. Mermaid manipulates the DOM directly and has no
 * meaningful SSR story, so this component renders nothing on the server and
 * only calls into the `mermaid` package after mount (see `mounted` guard) —
 * avoids the hydration-mismatch pitfalls that come from rendering
 * Mermaid-generated markup during SSR.
 *
 * `mermaid.render()` runs with the library's default `securityLevel: "strict"`,
 * which sanitizes the generated SVG — this never introduces a raw-HTML
 * injection path from LLM-authored diagram source.
 */

let mermaidInitPromise: Promise<typeof import("mermaid").default> | null = null;

function loadMermaid() {
  if (!mermaidInitPromise) {
    mermaidInitPromise = import("mermaid").then((mod) => {
      const mermaid = mod.default;
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        fontFamily: "var(--font-sans, ui-sans-serif, system-ui, sans-serif)",
        theme: "base",
        themeVariables: {
          background: "#ffffff",
          primaryColor: "#fff3cf",
          primaryBorderColor: "rgba(22, 22, 26, 0.18)",
          primaryTextColor: "#16161a",
          secondaryColor: "#fafaf5",
          lineColor: "#5b5b63",
          textColor: "#16161a",
        },
      });
      return mermaid;
    });
  }
  return mermaidInitPromise;
}

export function MermaidDiagram({ code }: { code: string }) {
  const reactId = useId().replace(/[^a-zA-Z0-9]/g, "");
  const renderToken = useRef(0);
  const [mounted, setMounted] = useState(false);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted) return;
    const token = ++renderToken.current;
    setSvg(null);
    setError(null);

    (async () => {
      try {
        const mermaid = await loadMermaid();
        const id = `mermaid-${reactId}-${token}`;
        const valid = await mermaid.parse(code, { suppressErrors: true });
        if (!valid) throw new Error("Invalid diagram syntax");
        const { svg: rendered } = await mermaid.render(id, code);
        if (renderToken.current === token) setSvg(rendered);
      } catch (err) {
        if (renderToken.current === token) {
          setError(err instanceof Error ? err.message : "Could not render diagram");
        }
      }
    })();
  }, [mounted, code, reactId]);

  if (!mounted) {
    return (
      <div className="glass-elev-2 my-2 rounded-xl p-3 text-xs text-ink-muted">Loading diagram…</div>
    );
  }

  if (error) {
    return (
      <div className="my-2 max-w-full overflow-x-auto rounded-xl border border-hairline bg-bg px-3 py-2.5 thin-scrollbar">
        <p className="mb-1.5 text-xs font-medium text-[var(--rag-poor)]">Couldn&apos;t render diagram ({error})</p>
        <pre className="whitespace-pre-wrap font-mono text-[11px] text-ink-muted">{code}</pre>
      </div>
    );
  }

  return (
    <div className="glass-elev-2 my-2 max-w-full overflow-x-auto rounded-xl p-3 thin-scrollbar">
      {svg ? (
        <div
          className="flex justify-center [&_svg]:h-auto [&_svg]:max-w-none"
          // Safe: `svg` comes from mermaid's own sanitized render output
          // (securityLevel "strict"), not raw message content.
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <p className="text-xs text-ink-muted">Rendering diagram…</p>
      )}
    </div>
  );
}
