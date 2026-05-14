import { create } from "zustand";
import { useMemo } from "react";
import type { RunStatus } from "@/types/workflow";
import type { StreamEvent, StreamEventType, StatusEvent, ChildCreatedEvent, ChildCompletedEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface RunState {
  runId: string | null;
  status: RunStatus;
  events: StreamEvent[];
  /** Per-node run status, keyed by node_id */
  nodeStatuses: Record<string, RunStatus>;
  /** Currently selected node for run detail view in bottom panel */
  selectedRunNodeId: string | null;
  /** Parent-to-children mapping: parentId → childNodeIds */
  parentChildMap: Record<string, string[]>;
  /** Progress summary from DAG execution */
  progressSummary: { total: number; completed: number; failed: number } | null;

  // Actions
  setRunId: (id: string | null) => void;
  setStatus: (status: RunStatus) => void;
  addEvent: (event: StreamEvent) => void;
  hydrateEvents: (events: StreamEvent[]) => void;
  mergeEvents: (events: StreamEvent[]) => void;
  clearEvents: () => void;
  setNodeStatus: (nodeId: string, status: RunStatus) => void;
  setSelectedRunNode: (id: string | null) => void;
  /** Register a child node under a parent planner */
  registerChild: (parentId: string, childId: string) => void;
}

// ---------------------------------------------------------------------------
// Helper: derive RunStatus from event content string
// ---------------------------------------------------------------------------
function parseRunStatus(raw: string): RunStatus | null {
  const valid: RunStatus[] = ["idle", "pending", "running", "paused", "cancelling", "cancelled", "completed", "failed"];
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
  selectedRunNodeId: null,
  parentChildMap: {},
  progressSummary: null,

  setRunId: (id) => set({ runId: id }),

  setStatus: (status) => set({ status }),

  addEvent: (event: StreamEvent) => {
    const state = get();
    if (event.event_id && state.events.some((existing) => existing.event_id === event.event_id)) {
      return;
    }

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
        event.type === "node_failed" ||
        event.type === "task_blocked" ||
        event.type === "task_unblocked") &&
      event.node_id
    ) {
      const statusMap: Record<string, RunStatus> = {
        node_started: "running",
        node_completed: "completed",
        node_failed: "failed",
        task_blocked: "paused",
        task_unblocked: "running",
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

    // Handle child_created — register parent-child relationship. The child is
    // known to exist, but it should only become running after node/task events.
    if (event.type === "child_created") {
      const childEvent = event as ChildCreatedEvent;
      const parentId = event.node_id;
      const childId = childEvent.child_node_id;
      if (parentId && childId) {
        const existing = state.parentChildMap[parentId] ?? [];
        set({
          events: [...state.events, event],
          parentChildMap: {
            ...state.parentChildMap,
            [parentId]: [...existing, childId],
          },
          nodeStatuses: {
            ...state.nodeStatuses,
            [childId]: state.nodeStatuses[childId] ?? "pending",
          },
        });
        return;
      }
    }

    // Handle child_completed — mark child as completed
    if (event.type === "child_completed") {
      const childEvent = event as ChildCompletedEvent;
      const childId = childEvent.child_node_id;
      if (childId) {
        set({
          events: [...state.events, event],
          nodeStatuses: {
            ...state.nodeStatuses,
            [childId]: "completed",
          },
        });
        return;
      }
    }

    // Handle progress_summary
    if (event.type === "progress_summary") {
      const ps = event as import("@/types/events").ProgressSummaryEvent;
      set({
        events: [...state.events, event],
        progressSummary: { total: ps.total, completed: ps.completed, failed: ps.failed },
      });
      return;
    }

    // Infer node running status from activity events
    // (node_started may be missed if WS connected after it was published)
    if (event.node_id) {
      const currentStatus = state.nodeStatuses[event.node_id];
      if (!currentStatus || currentStatus === "idle") {
        set({
          events: [...state.events, event],
          nodeStatuses: {
            ...state.nodeStatuses,
            [event.node_id]: "running",
          },
        });
        return;
      }
    }

    // Default: just append the event
    set({ events: [...state.events, event] });
  },

  hydrateEvents: (events: StreamEvent[]) => {
    set({ events: [], nodeStatuses: {}, parentChildMap: {}, progressSummary: null });
    for (const event of events) {
      get().addEvent(event);
    }
  },

  mergeEvents: (events: StreamEvent[]) => {
    for (const event of events) {
      get().addEvent(event);
    }
  },

  clearEvents: () => set({ events: [], nodeStatuses: {}, selectedRunNodeId: null, parentChildMap: {}, progressSummary: null }),

  setNodeStatus: (nodeId: string, status: RunStatus) => {
    set({ nodeStatuses: { ...get().nodeStatuses, [nodeId]: status } });
  },

  setSelectedRunNode: (id) => set({ selectedRunNodeId: id }),

  registerChild: (parentId: string, childId: string) => {
    const existing = get().parentChildMap[parentId] ?? [];
    set({
      parentChildMap: {
        ...get().parentChildMap,
        [parentId]: [...existing, childId],
      },
    });
  },
}));

// ---------------------------------------------------------------------------
// Selector helpers — use with useRunStore(selector)
// ---------------------------------------------------------------------------

/** Select events filtered by type */
export function useEventsByType(type: StreamEventType): StreamEvent[] {
  const all = useRunStore((s) => s.events);
  return useMemo(() => all.filter((e) => e.type === type), [all, type]);
}

/** Select events filtered by node_id */
export function useEventsByNode(nodeId: string): StreamEvent[] {
  const all = useRunStore((s) => s.events);
  return useMemo(() => all.filter((e) => e.node_id === nodeId), [all, nodeId]);
}

/** Select the current RunStatus for a specific node */
export function useNodeStatus(nodeId: string): RunStatus {
  return useRunStore(
    (state) => state.nodeStatuses[nodeId] ?? "idle"
  );
}

/** Select child node IDs for a given parent planner node */
export function useChildrenOfNode(parentId: string): string[] {
  const children = useRunStore((state) => state.parentChildMap[parentId]);
  return useMemo(() => children ?? [], [children]);
}
