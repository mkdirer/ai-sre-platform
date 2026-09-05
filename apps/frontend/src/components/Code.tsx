import { useEffect, useRef, useState } from "react";

/** Inline code token for IDs, hashes, routes, and other technical values. */
export function Mono({ children }: { children: React.ReactNode }) {
  return <code className="mono">{children}</code>;
}

/** Small copy button for technical IDs. Degrades to a no-op label where the
 * clipboard API is unavailable (the full value stays visible in the UI). */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  async function copy() {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard) {
        await navigator.clipboard.writeText(value);
        setCopied(true);
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1500);
      }
    } catch {
      // Clipboard failures must never break the surrounding view.
    }
  }

  return (
    <button
      type="button"
      className="copy-btn"
      aria-label={`Copy ${label}`}
      aria-live="polite"
      onClick={() => void copy()}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
