import { create } from "zustand";
import type { RunStatus } from "@/types/workflow";
import type { StreamEvent, StreamEventType, StatusEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface RunState {
  runId: string | null;
  status: RunStatus;
  events: StreamEvent[];
  /** Per-node run status, keyed by node_id */
  nodeStatuses: Record<string, RunStatus>;

  // Actions
  setRunId: (id: string | null) => void;
  setStatus: (status: RunStatus) => void;
  addEvent: (event: StreamEvent) => void;
  clearEvents: () => void;
  setNodeStatus: (nodeId: string, status: RunStatus) => void;
}

// ---------------------------------------------------------------------------
// Helper: derive RunStatus from event content string
// ---------------------------------------------------------------------------
function parseRunStatus(raw: string): RunStatus | null {
  const valid: RunStatus[] = ["idle", "running", "paused", "completed", "failed"];
  if (valid.includes(raw as RunStatus)) return raw as RunStatus;
  return null;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useRunStore = create<RunState>((set, get) => ({
  runId: null,
  status: "idle",
  events: [],
  nodeStatuses: {},

  setRunId: (id) => set({ runId: id }),

  setStatus: (status) => set({ status }),

  addEvent: (event: StreamEvent) => {
    const state = get();

    // Sync status events to nodeStatuses / global status
    if (event.type === "status") {
      const content = (event as StatusEvent).content;
      const parsed = parseRunStatus(content);

      if (parsed !== null) {
        if (event.node_id) {
          // Node-level status
          set({
            events: [...state.events, event],
            nodeStatuses: { ...state.nodeStatuses, [event.node_id]: parsed },
          });
          return;
        }
      }
    }

    // Handle node lifecycle events (node_started, node_completed, node_failed)
    if (
      (event.type === "node_started" ||
        event.type === "node_completed" ||
        event.type === "node_failed") &&
      event.node_id
    ) {
      const statusMap: Record<string, RunStatus> = {
        node_started: "running",
        node_completed: "completed",
        node_failed: "failed",
      };
      set({
        events: [...state.events, event],
        nodeStatuses: {
          ...state.nodeStatuses,
          [event.node_id]: statusMap[event.type],
        },
      });
      return;
    }

    // Handle run-level events
    if (event.type === "run_started") {
      set({ events: [...state.events, event], status: "running" });
      return;
    }
    if (event.type === "run_completed") {
      set({ events: [...state.events, event], status: "completed" });
      return;
    }
    if (event.type === "run_failed") {
      set({ events: [...state.events, event], status: "failed" });
      return;
    }

    // Default: just append the event
    set({ events: [...state.events, event] });
  },

  clearEvents: () => set({ events: [], nodeStatuses: {} }),

  setNodeStatus: (nodeId: string, status: RunStatus) => {
    set({ nodeStatuses: { ...get().nodeStatuses, [nodeId]: status } });
  },
}));

// ---------------------------------------------------------------------------
// Selector helpers — use with useRunStore(selector)
// ---------------------------------------------------------------------------

/** Select events filtered by type */
export function useEventsByType(type: StreamEventType): StreamEvent[] {
  return useRunStore((state) => state.events.filter((e) => e.type === type));
}

/** Select events filtered by node_id */
export function useEventsByNode(nodeId: string): StreamEvent[] {
  return useRunStore((state) =>
    state.events.filter((e) => e.node_id === nodeId)
  );
}

/** Select the current RunStatus for a specific node */
export function useNodeStatus(nodeId: string): RunStatus {
  return useRunStore(
    (state) => state.nodeStatuses[nodeId] ?? "idle"
  );
}
