import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  // `package.json`'s `lint` script runs raw `eslint .` (not `next lint`, which
  // auto-ignores both of these) — so once `.next/` exists (after any `next build` or
  // `next dev` run), ESLint's flat config otherwise happily lints Next's own generated
  // `.next/types/**` output and reports thousands of problems that are 100% noise, none
  // of them real. `next-env.d.ts` is also Next-generated — its own header says "This
  // file should not be edited" — and its `/// <reference path="./.next/types/..." />`
  // trips `@typescript-eslint/triple-slash-reference` on every single run (confirmed:
  // `npm run lint` with `.next/` present reported exactly this one "error" and nothing
  // else once `.next/**` was ignored) — there's no way to "fix" a file that shouldn't be
  // hand-edited, so it's ignored too, same as `next lint` already does for it.
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;
