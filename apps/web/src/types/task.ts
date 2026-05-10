/** Task and TaskMessage types — mirrors the backend schemas. */

export interface Task {
  id: string;
  run_id: string;
  parent_task_id: string | null;
  title: string;
  description: string;
  status: TaskStatus;
  assigned_node_id: string | null;
  assigned_worker_label: string | null;
  progress: number;
  result_summary: string;
  created_at: string;
  updated_at: string;
}

export type TaskStatus =
  | "pending"
  | "assigned"
  | "running"
  | "blocked"
  | "completed"
  | "failed";

export interface TaskMessage {
  id: string;
  task_id: string;
  sender_type: "planner" | "worker" | "user";
  sender_id: string;
  message_type:
    | "assignment"
    | "question"
    | "answer"
    | "escalation"
    | "update"
    | "user_edit";
  content: string;
  created_at: string;
}

/** Status config for rendering */
export const TASK_STATUS_CONFIG: Record<
  TaskStatus,
  { color: string; bgColor: string; label: string; icon: string }
> = {
  pending:   { color: "text-gray-400",  bgColor: "bg-gray-100",  label: "Pending",   icon: "○" },
  assigned:  { color: "text-blue-500",  bgColor: "bg-blue-100",  label: "Assigned",  icon: "◎" },
  running:   { color: "text-blue-600",  bgColor: "bg-blue-100",  label: "Running",   icon: "●" },
  blocked:   { color: "text-amber-600", bgColor: "bg-amber-100", label: "Blocked",   icon: "⊘" },
  completed: { color: "text-green-600", bgColor: "bg-green-100", label: "Completed", icon: "✓" },
  failed:    { color: "text-red-600",   bgColor: "bg-red-100",   label: "Failed",    icon: "✗" },
};
