// Node loader hook: resolves the Next.js "@/..." path alias (tsconfig paths) to files in the project, so modules such
// as middleware.ts can be imported by `node --test`. Registered from the test with `module.register`.
import { existsSync } from "node:fs";
import { resolve as resolvePath } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolvePath(fileURLToPath(new URL("../..", import.meta.url)));

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith("@/")) {
    const base = resolvePath(root, specifier.slice(2));
    for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`]) {
      if (existsSync(candidate) && /\.[tj]sx?$/.test(candidate)) {
        return nextResolve(pathToFileURL(candidate).href, context);
      }
    }
  }
  // Extensionless relative imports inside the TypeScript sources (`./api-client`).
  if (specifier.startsWith(".") && !/\.[a-z]+$/.test(specifier) && context.parentURL) {
    const parent = fileURLToPath(context.parentURL);
    const base = resolvePath(parent, "..", specifier);
    for (const ext of [".ts", ".tsx"]) {
      if (existsSync(base + ext)) return nextResolve(pathToFileURL(base + ext).href, context);
    }
  }
  // `next` ships CommonJS entry points without an exports map: Node's ESM resolver needs the explicit extension.
  if (/^next\/[a-z-]+$/.test(specifier)) return nextResolve(`${specifier}.js`, context);
  return nextResolve(specifier, context);
}
