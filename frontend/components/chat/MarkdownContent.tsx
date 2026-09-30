"use client";

import { useMemo } from "react";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

import { MermaidDiagram } from "./MermaidDiagram";

/**
 * The chat model (GLM-5) sometimes writes list items as a literal bullet glyph
 * ("• item", "◦ item") instead of CommonMark's "- "/"* " list-marker syntax — a real
 * example pulled from this app's own chat history: "Quality (12 KPIs) — broken into:\n•
 * *Software delivery*: ...\n• *Deliverable reviews*: ...". A literal "•" is not a list
 * marker per the CommonMark spec remark-gfm implements, so left as-is it renders as
 * ordinary text with a stray bullet character glued to the front — exactly the
 * "literal bullet characters" symptom this component exists to fix. Rewriting such a
 * line to start with "- " instead (preserving its leading indentation, so a nested
 * "  • sub-item" still nests correctly under a parent bullet) is enough: CommonMark
 * allows an unordered list to interrupt a paragraph with no blank line needed first, so
 * these convert into a real `<ul>`/`<li>` without requiring the model to also insert
 * blank lines around them.
 */
function normalizeBulletGlyphs(content: string): string {
  return content.replace(/^([ \t]*)[•◦‣▪][ \t]+/gm, "$1- ");
}

/**
 * Renders LLM-authored free text (chat messages, judge reasoning) as
 * typography-matched Markdown instead of a raw-text wall of `**`/`•`
 * characters — see `MessageBubble` and `ReasoningDrawer`.
 *
 * Security: no `rehype-raw` plugin is wired in, so raw HTML embedded in the
 * Markdown source is never rendered (react-markdown escapes it to text by
 * default) — this is the only thing standing between LLM output and an
 * HTML-injection surface, so don't add it. Links always open with
 * `rel="noopener noreferrer"` so a malicious link target can't reach back
 * into this window via `window.opener`.
 *
 * Dense tabular data (Markdown tables) is wrapped in a `.solid-panel`
 * surface per this app's design rule that dense data never sits on glass —
 * see `components/design-system/SolidPanel.tsx`. Everything else (the
 * bubble itself) stays on its existing glass/lemon-soft background.
 */
const components: Components = {
  p: ({ children }) => <p className="mb-2 leading-relaxed last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-lemon-ink underline underline-offset-2 hover:text-ink"
    >
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-1 last:mb-0 marker:text-ink-muted">{children}</ul>,
  ol: ({ children }) => (
    <ol className="mb-2 ml-4 list-decimal space-y-1 last:mb-0 marker:text-ink-muted">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-0.5 leading-relaxed">{children}</li>,
  h1: ({ children }) => <h1 className="mb-1.5 mt-3 text-base font-semibold text-ink first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-1.5 mt-3 text-[0.95rem] font-semibold text-ink first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-2.5 text-sm font-semibold text-ink first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-2 text-sm font-semibold text-ink first:mt-0">{children}</h4>,
  h5: ({ children }) => <h5 className="mb-1 mt-2 text-sm font-semibold text-ink first:mt-0">{children}</h5>,
  h6: ({ children }) => <h6 className="mb-1 mt-2 text-sm font-semibold text-ink first:mt-0">{children}</h6>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-hairline pl-3 italic text-ink-muted last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-hairline" />,
  table: ({ children }) => (
    <div className="solid-panel my-2 max-w-full overflow-x-auto rounded-lg thin-scrollbar">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-bg">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => <tr className="border-b border-hairline last:border-b-0">{children}</tr>,
  th: ({ children }) => (
    <th className="border-r border-hairline px-2.5 py-1.5 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted last:border-r-0">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-r border-hairline px-2.5 py-1.5 align-top text-ink last:border-r-0">{children}</td>
  ),
  // react-markdown v9+ no longer passes an `inline` flag to `code` — a
  // `language-*` className is only present on fenced code blocks, so its
  // absence is the reliable inline/block signal here. `pre` is a pass-through
  // below so block code (and Mermaid diagrams) own their own card instead of
  // being double-wrapped in a native <pre>. `node` (react-markdown's
  // `ExtraProps`) is destructured out and deliberately unused — spreading it
  // onto a real DOM element would otherwise serialize the mdast node object
  // as a stray `node="[object Object]"` attribute.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- discarded on purpose, see comment above
  code: ({ className, children, node: _node, ...rest }) => {
    const match = /language-(\w+)/.exec(className ?? "");
    const text = String(children).replace(/\n$/, "");

    if (match?.[1] === "mermaid") {
      return <MermaidDiagram code={text} />;
    }

    if (match) {
      return (
        <div className="my-2 max-w-full overflow-x-auto rounded-xl border border-hairline bg-bg px-3 py-2.5 thin-scrollbar">
          <code
            className={cn("whitespace-pre font-mono text-[11px] leading-relaxed text-ink", className)}
            {...rest}
          >
            {children}
          </code>
        </div>
      );
    }

    return (
      <code className="rounded bg-bg px-1 py-0.5 font-mono text-[0.85em] text-ink" {...rest}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => <>{children}</>,
};

export function MarkdownContent({ content, className }: { content: string; className?: string }) {
  const normalized = useMemo(() => normalizeBulletGlyphs(content), [content]);
  return (
    <div className={cn("text-sm", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {normalized}
      </ReactMarkdown>
    </div>
  );
}
