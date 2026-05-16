"use client";

import { useEffect, useRef, useMemo, useState } from "react";
import { useRunStore } from "@/stores/runStore";
import type { ShellStdoutEvent, ShellStderrEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface XtermStreamProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

// ---------------------------------------------------------------------------
// XtermStream — renders shell_stdout / shell_stderr events in an xterm.js
// terminal.  Falls back to a plain <pre> when @xterm/xterm cannot be loaded.
// ---------------------------------------------------------------------------
export default function XtermStream({ nodeId = "" }: XtermStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<any>(null);
  const fitAddonRef = useRef<any>(null);
  const disposeRef = useRef<(() => void) | null>(null);
  const fallbackRef = useRef<HTMLPreElement>(null);

  const [xtermReady, setXtermReady] = useState(false);

  // Track how many events have been written to avoid full rewrites
  const renderedCountRef = useRef(0);
  const prevNodeIdRef = useRef(nodeId);

  const allEvents = useRunStore((s) => s.events);
  const events = useMemo(
    () =>
      allEvents.filter(
        (e): e is ShellStdoutEvent | ShellStderrEvent =>
          (e.type === "shell_stdout" || e.type === "shell_stderr") &&
          (nodeId === "" || e.node_id === nodeId)
      ),
    [allEvents, nodeId]
  );

  // -----------------------------------------------------------------------
  // Initialise xterm.js via dynamic import (browser-only)
  // -----------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const container = containerRef.current;
    if (!container) return;

    let observer: ResizeObserver | null = null;

    (async () => {
      try {
        const { Terminal } = await import("@xterm/xterm");
        const { FitAddon } = await import("@xterm/addon-fit");

        if (cancelled) return;

        const term = new Terminal({
          theme: {
            background: "#1e1e1e",
            foreground: "#d4d4d4",
            cursor: "#d4d4d4",
            selectionBackground: "#264f78",
          },
          fontFamily: "Menlo, Monaco, 'Courier New', monospace",
          fontSize: 13,
          lineHeight: 1.2,
          cursorBlink: false,
          disableStdin: true,
          convertEol: true,
          scrollback: 5000,
        });

        const fitAddon = new FitAddon();
        term.loadAddon(fitAddon);

        term.open(container);

        // Defer fit() to next frame so the browser has laid out the container
        requestAnimationFrame(() => {
          try { fitAddon.fit(); } catch {}
        });

        termRef.current = term;
        fitAddonRef.current = fitAddon;
        setXtermReady(true);

        // ResizeObserver to keep the terminal fitted to its container
        observer = new ResizeObserver(() => {
          try {
            fitAddon.fit();
          } catch {
            // Ignore fit errors during unmount/transitions
          }
        });
        observer.observe(container);

        disposeRef.current = () => {
          observer?.disconnect();
          term.dispose();
        };
      } catch {
        setXtermReady(false);
      }
    })();

    return () => {
      cancelled = true;
      disposeRef.current?.();
      disposeRef.current = null;
      termRef.current = null;
      fitAddonRef.current = null;
    };
  }, []);

  // -----------------------------------------------------------------------
  // Write terminal content incrementally — only new events since last render
  // -----------------------------------------------------------------------
  useEffect(() => {
    const nodeIdChanged = prevNodeIdRef.current !== nodeId;
    prevNodeIdRef.current = nodeId;

    if (nodeIdChanged) {
      // Full rebuild on node filter change
      renderedCountRef.current = 0;
      const text = events.map((ev) => ev.content).join("\n");
      if (termRef.current) {
        termRef.current.clear();
        if (text) termRef.current.write(text + "\n");
        termRef.current.scrollToBottom();
      } else if (fallbackRef.current) {
        const pre = fallbackRef.current;
        pre.textContent = text ? `${text}\n` : "";
        pre.scrollTop = pre.scrollHeight;
      }
      renderedCountRef.current = events.length;
      return;
    }

    // Incremental: write only new events
    const prevCount = renderedCountRef.current;
    if (events.length <= prevCount) return;

    const newEvents = events.slice(prevCount);
    renderedCountRef.current = events.length;

    if (newEvents.length === 0) return;

    const text = newEvents.map((ev) => ev.content).join("\n");

    if (termRef.current) {
      termRef.current.write(text + "\n");
      termRef.current.scrollToBottom();
    } else if (fallbackRef.current) {
      const pre = fallbackRef.current;
      pre.textContent += text + "\n";
      pre.scrollTop = pre.scrollHeight;
    }
  }, [events, xtermReady, nodeId]);

  return (
    <div className="w-full h-full relative bg-[#1e1e1e] overflow-hidden">
      <div ref={containerRef} className="w-full h-full bg-[#1e1e1e]" />
      {!xtermReady && (
        <pre
          ref={fallbackRef}
          className="absolute inset-0 bg-[#1e1e1e] text-[#d4d4d4] p-2 overflow-auto text-xs font-mono leading-5 m-0 whitespace-pre-wrap"
        />
      )}
    </div>
  );
}
