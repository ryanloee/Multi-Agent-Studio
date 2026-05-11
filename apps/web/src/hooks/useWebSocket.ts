"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { withAccessToken } from "@/lib/auth";
import type { AgentNodeType, RunStatus } from "@/types/workflow";

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

function defaultWsBaseUrl(): string {
  if (process.env.NEXT_PUBLIC_WS_URL) return process.env.NEXT_PUBLIC_WS_URL;
  if (typeof window === "undefined") return "ws://localhost:8000";
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.hostname}:8000`;
}

export function useWebSocket(
  runId: string | null,
  options: UseWebSocketOptions = {},
): UseWebSocketReturn {
  const { baseUrl = defaultWsBaseUrl(), autoReconnect = true } = options;

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

      const url = withAccessToken(`${baseUrl}/ws/runs/${runId}/stream`);
      const ws = new WebSocket(url);

      ws.onopen = () => {
        if (!cancelled) {
          setIsConnected(true);
          useTaskStore.getState().setCurrentRunId(runId);
        }
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          addEvent(data);

          // Handle task-related events
          const taskStore = useTaskStore.getState();
          const workflowStore = useWorkflowStore.getState();
          const runStore = useRunStore.getState();
          if (data.type === "task_created") {
            taskStore.upsertTask({
              id: data.task_id,
              run_id: runId!,
              parent_task_id: null,
              title: data.task_title || "",
              description: data.task_description || "",
              status: data.status || "pending",
              assigned_node_id: data.child_node_id || null,
              assigned_worker_label: null,
              progress: 0,
              result_summary: "",
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            });
            if (data.child_node_id) {
              runStore.setNodeStatus(data.child_node_id, (data.status || "pending") as RunStatus);
              const nodeExists = workflowStore.nodes.some((node) => node.id === data.child_node_id);
              if (!nodeExists) {
                workflowStore.addDynamicNode(data.node_id || "planner", {
                  id: data.child_node_id,
                  type: "coder",
                  prompt: data.task_description || "",
                  model: "",
                });
              }
            }
          } else if (data.type === "task_updated") {
            const existing = taskStore.tasks.find((t) => t.id === data.task_id);
            if (existing) {
              taskStore.upsertTask({
                ...existing,
                status: data.status || existing.status,
                progress: data.progress ?? existing.progress,
                assigned_node_id: data.assigned_node_id || existing.assigned_node_id,
                assigned_worker_label: data.assigned_worker_label || existing.assigned_worker_label,
                result_summary: data.result_summary || existing.result_summary,
                updated_at: new Date().toISOString(),
              });
              const nodeId = data.assigned_node_id || existing.assigned_node_id;
              if (nodeId && data.status) {
                runStore.setNodeStatus(nodeId, data.status as RunStatus);
              }
            }
          } else if (data.type === "task_message") {
            taskStore.appendMessage(data.task_id, {
              id: `msg_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
              task_id: data.task_id,
              sender_type: data.sender_type,
              sender_id: data.sender_id,
              message_type: data.message_type,
              content: data.content,
              created_at: new Date().toISOString(),
            });
          } else if (data.type === "child_created") {
            const parentId = data.node_id || "planner";
            const nodeExists = workflowStore.nodes.some((node) => node.id === data.child_node_id);
            const edgeExists = workflowStore.edges.some((edge) =>
              edge.source === parentId && edge.target === data.child_node_id
            );
            if (!nodeExists || (!edgeExists && parentId !== "planner")) {
              workflowStore.addDynamicNode(parentId, {
                id: data.child_node_id,
                type: (data.child_type || "coder") as AgentNodeType,
                prompt: data.child_prompt || "",
                model: data.child_model || "",
              });
            }
          }
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
