// Browser-download helpers for the Excel export. `filenameFromDisposition` is pure (node:test loads it directly).

/** Extracts the filename from a Content-Disposition header (RFC 6266 `filename*` preferred), else `fallback`. */
export function filenameFromDisposition(header: string | null | undefined, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (star) {
    try {
      const decoded = decodeURIComponent(star[2].trim().replace(/^"|"$/g, ""));
      if (decoded) return sanitize(decoded, fallback);
    } catch {
      // malformed percent-encoding — fall through to the plain filename
    }
  }
  const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(header);
  if (plain) {
    const value = (plain[2] ?? plain[1]).trim();
    if (value) return sanitize(value, fallback);
  }
  return fallback;
}

/** Never let a server-provided name carry a path or control characters into the download. */
function sanitize(name: string, fallback: string): string {
  const cleaned = name.replace(/[\\/\x00-\x1f]/g, "_").trim();
  return cleaned || fallback;
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the click has been dispatched; some browsers cancel the download if revoked synchronously.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
