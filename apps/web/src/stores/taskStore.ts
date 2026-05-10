import { create } from "zustand";
import type { Task, TaskMessage } from "@/types/task";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface TaskState {
  /** All tasks for the current run */
  tasks: Task[];
  /** Currently selected task (for detail view) */
  selectedTaskId: string | null;
  /** Messages per task ID */
  taskMessages: Record<string, TaskMessage[]>;
  /** Current run ID — used to scope task operations */
  currentRunId: string | null;

  // ---- Actions ----
  setCurrentRunId: (runId: string | null) => void;
  setTasks: (tasks: Task[]) => void;
  upsertTask: (task: Task) => void;
  selectTask: (id: string | null) => void;
  setTaskMessages: (taskId: string, messages: TaskMessage[]) => void;
  appendMessage: (taskId: string, msg: TaskMessage) => void;
  optimisticUpdateTask: (id: string, patch: Partial<Task>) => void;
  clearTasks: () => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useTaskStore = create<TaskState>((set, get) => ({
  tasks: [],
  selectedTaskId: null,
  taskMessages: {},
  currentRunId: null,

  setCurrentRunId: (runId) => set({ currentRunId: runId }),

  setTasks: (tasks) => set({ tasks }),

  upsertTask: (task) =>
    set((state) => {
      const idx = state.tasks.findIndex((t) => t.id === task.id);
      if (idx >= 0) {
        const next = [...state.tasks];
        next[idx] = task;
        return { tasks: next };
      }
      return { tasks: [...state.tasks, task] };
    }),

  selectTask: (id) => set({ selectedTaskId: id }),

  setTaskMessages: (taskId, messages) =>
    set((state) => ({
      taskMessages: { ...state.taskMessages, [taskId]: messages },
    })),

  appendMessage: (taskId, msg) =>
    set((state) => {
      const existing = state.taskMessages[taskId] ?? [];
      return {
        taskMessages: { ...state.taskMessages, [taskId]: [...existing, msg] },
      };
    }),

  optimisticUpdateTask: (id, patch) =>
    set((state) => ({
      tasks: state.tasks.map((t) => (t.id === id ? { ...t, ...patch } : t)),
    })),

  clearTasks: () => set({ tasks: [], selectedTaskId: null, taskMessages: {} }),
}));
