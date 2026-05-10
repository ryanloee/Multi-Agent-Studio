"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { useRunStore } from "@/stores/runStore";

// ---------------------------------------------------------------------------
// useWebSocket — connects to a run event stream via WebSocket
//
// - All events are forwarded to runStore.addEvent (which handles status sync)
// - Accepts a configurable URL base (defaults to localhost:8000)
// - autoReconnect: when true (default), reconnects on abnormal close; stops
//   reconnecting once the run reaches a terminal state (completed / failed)
// - Exposes disconnect() for manual teardown and isConnected for status
// ---------------------------------------------------------------------------

interface UseWebSocketOptions {
  /** URL of the WebSocket server, e.g. "ws://localhost:8000". Defaults to that. */
  baseUrl?: string;
  /** Whether to automatically reconnect on abnormal close. Defaults to true. */
  autoReconnect?: boolean;
}

interface UseWebSocketReturn {
  /** Manually close the connection and stop reconnecting */
  disconnect: () => void;
  /** Whether the WebSocket is currently open */
  isConnected: boolean;
}

export function useWebSocket(
  runId: string | null,
  options: UseWebSocketOptions = {},
): UseWebSocketReturn {
  const { baseUrl = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000", autoReconnect = true } = options;

  const wsRef = useRef<WebSocket | null>(null);
  const addEvent = useRunStore((s) => s.addEvent);
  const setStatus = useRunStore((s) => s.setStatus);

  // Track whether the user explicitly called disconnect()
  const manualCloseRef = useRef(false);

  // Track connection state for consumers
  const [isConnected, setIsConnected] = useState(false);

  // Reconnect timer ref so we can clean it up
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -----------------------------------------------------------------------
  // disconnect — manual teardown
  // -----------------------------------------------------------------------
  const disconnect = useCallback(() => {
    manualCloseRef.current = true;

    // Clear any pending reconnect timer
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsConnected(false);
  }, []);

  // -----------------------------------------------------------------------
  // connect — establish the WebSocket
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (!runId) return;

    // Reset manual close flag on new connect cycle
    manualCloseRef.current = false;

    let cancelled = false;

    function connect() {
      if (cancelled || manualCloseRef.current) return;

      const url = `${baseUrl}/ws/runs/${runId}/stream`;
      const ws = new WebSocket(url);

      ws.onopen = () => {
        if (!cancelled) setIsConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          addEvent(data);
        } catch {
          // skip non-JSON messages
        }
      };

      ws.onerror = () => {
        // onclose will fire after onerror, so we handle logic there
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        if (!cancelled) setIsConnected(false);

        // If this was a manual close or auto-reconnect is disabled, stop.
        if (manualCloseRef.current || !autoReconnect) return;

        // Abnormal close (code !== 1000) — schedule reconnect
        if (!event.wasClean && !cancelled) {
          reconnectTimerRef.current = setTimeout(() => {
            if (!cancelled && !manualCloseRef.current) {
              connect();
            }
          }, 3000);
        }
      };

      wsRef.current = ws;
    }

    connect();

    return () => {
      cancelled = true;

      // Clear any pending reconnect timer
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setIsConnected(false);
    };
  }, [runId, baseUrl, autoReconnect, addEvent]);

  // -----------------------------------------------------------------------
  // When the run reaches a terminal state, stop reconnecting
  // -----------------------------------------------------------------------
  const runStatus = useRunStore((s) => s.status);

  useEffect(() => {
    if (runStatus === "completed" || runStatus === "failed") {
      // Clean up the connection on terminal state
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      // Mark as manual close so onclose won't schedule another reconnect
      manualCloseRef.current = true;

      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setIsConnected(false);
    }

    // Connection failure: if status is failed and we never connected, sync
    if (runStatus === "failed" && !wsRef.current) {
      setStatus("failed");
    }
  }, [runStatus, setStatus]);

  // -----------------------------------------------------------------------
  // Fallback polling: if status stays "running" too long, poll the REST API
  // to catch cases where WebSocket events were missed.
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (!runId || runStatus !== "running") return;

    const POLL_INTERVAL = 10_000; // 10 seconds
    let cancelled = false;

    const poll = async () => {
      while (!cancelled) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL));
        if (cancelled) break;

        // Only poll if still running
        const currentStatus = useRunStore.getState().status;
        if (currentStatus !== "running") break;

        try {
          const { api } = await import("@/lib/api");
          const runInfo = await api.getRun(runId);
          if (
            runInfo.status === "completed" ||
            runInfo.status === "failed" ||
            runInfo.status === "cancelled"
          ) {
            setStatus(runInfo.status === "cancelled" ? "failed" : runInfo.status);
            break;
          }
        } catch {
          // Ignore poll errors — WebSocket is the primary source
        }
      }
    };

    poll();
    return () => { cancelled = true; };
  }, [runId, runStatus, setStatus]);

  return { disconnect, isConnected };
}
