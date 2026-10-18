import { Copy, Download, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import "./ReportDialog.css";

interface Props {
  readonly report: string;
  readonly fileName: string;
  readonly onClose: () => void;
}

/**
 * Modal showing the deterministic report. Focus is trapped and restored; Escape closes.
 * Copy and download stay in the browser: nothing is sent anywhere.
 */
export function ReportDialog({ report, fileName, onClose }: Props) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement | null>(null);
  const restore = useRef<Element | null>(null);
  const [status, setStatus] = useState("");

  useEffect(() => {
    restore.current = document.activeElement;
    panel.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    return () => {
      if (restore.current instanceof HTMLElement) restore.current.focus();
    };
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
    if (e.key !== "Tab" || !panel.current) return;
    const focusable = Array.from(panel.current.querySelectorAll<HTMLElement>("button, [href], [tabindex]:not([tabindex='-1'])"));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };

  const copy = async () => {
    try { await navigator.clipboard.writeText(report); setStatus("Report copied to the clipboard"); }
    catch { setStatus("Copy is not available in this browser"); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([report], { type: "text/markdown;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = fileName;
    a.click();
    // Revoking synchronously can cancel the download in some browsers; release it after the click is handled.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    setStatus(`Saved ${fileName}`);
  };

  return (
    <div className="report__backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="report" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panel} onKeyDown={onKeyDown}>
        <div className="report__head">
          <div>
            <h2 id={titleId} className="report__title">Evidence report</h2>
            <p className="report__sub">Demonstration · deterministic template · no AI model was called</p>
          </div>
          <button type="button" className="report__close" onClick={onClose} aria-label="Close report" data-autofocus>
            <X size={20} strokeWidth={2.2} />
          </button>
        </div>
        <pre className="report__body" tabIndex={0} aria-label="Report text (Markdown)">{report}</pre>
        <div className="report__actions">
          <span className="report__status" role="status" aria-live="polite">{status}</span>
          <button type="button" className="report__button" onClick={copy}><Copy size={16} aria-hidden="true" /> Copy</button>
          <button type="button" className="report__button report__button--primary" onClick={download}><Download size={16} aria-hidden="true" /> Download .md</button>
        </div>
      </div>
    </div>
  );
}
