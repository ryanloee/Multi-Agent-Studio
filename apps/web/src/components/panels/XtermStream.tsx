"use client";

import { useEffect, useRef } from "react";
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
  const termRef = useRef<any>(null); // Terminal instance (any to avoid import at top-level)
  const fitAddonRef = useRef<any>(null);
  const xtermLoaded = useRef(false);
  const disposeRef = useRef<(() => void) | null>(null);
  const fallbackRef = useRef<HTMLPreElement>(null);

  // We read events reactively via the store selector
  const events = useRunStore((s) => {
    const all = s.events;
    return all.filter(
      (e): e is ShellStdoutEvent | ShellStderrEvent =>
        (e.type === "shell_stdout" || e.type === "shell_stderr") &&
        (nodeId === "" || e.node_id === nodeId)
    );
  });

  // Track how many events we have already written to avoid duplicates
  const writtenCount = useRef(0);

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
        await import("@xterm/addon-fit");
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
        fitAddon.fit();

        termRef.current = term;
        fitAddonRef.current = fitAddon;
        xtermLoaded.current = true;

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
        // Dynamic import failed — xterm unavailable, use fallback <pre>
        xtermLoaded.current = false;
      }
    })();

    return () => {
      cancelled = true;
      disposeRef.current?.();
      disposeRef.current = null;
      termRef.current = null;
      fitAddonRef.current = null;
      xtermLoaded.current = false;
    };
  }, []);

  // -----------------------------------------------------------------------
  // Write new events to terminal (or fallback <pre>)
  // -----------------------------------------------------------------------
  useEffect(() => {
    const newEvents = events.slice(writtenCount.current);
    if (newEvents.length === 0) return;
    writtenCount.current = events.length;

    if (xtermLoaded.current && termRef.current) {
      for (const ev of newEvents) {
        termRef.current.write(ev.content + "\n");
      }
      // Scroll to bottom
      termRef.current.scrollToBottom();
    } else if (fallbackRef.current) {
      // Fallback: append to <pre>
      const pre = fallbackRef.current;
      for (const ev of newEvents) {
        pre.textContent += ev.content + "\n";
      }
      pre.scrollTop = pre.scrollHeight;
    }
  }, [events]);

  return (
    <div className="w-full h-full relative">
      {/* xterm container — always rendered, xterm will attach if loaded */}
      <div
        ref={containerRef}
        className="w-full h-full"
        style={{ display: xtermLoaded.current ? "block" : "none" }}
      />

      {/* Fallback plain <pre> when xterm is not loaded */}
      {!xtermLoaded.current && (
        <pre
          ref={fallbackRef}
          className="w-full h-full bg-[#1e1e1e] text-[#d4d4d4] p-2 overflow-auto text-xs font-mono leading-5 m-0 whitespace-pre-wrap"
        />
      )}
    </div>
  );
}
