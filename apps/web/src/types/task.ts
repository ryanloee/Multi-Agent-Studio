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
  dependencies?: string;
  retry_count: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export type ArtifactType =
  | "file_change"
  | "research_note"
  | "test_result"
  | "review_report"
  | "merge_report"
  | "decision"
  | "final_output"
  | "project_summary";

export interface Artifact {
  id: string;
  run_id: string;
  workflow_id: string;
  task_id: string | null;
  node_id: string | null;
  type: ArtifactType;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
  metadata_json?: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
}

export type TaskStatus =
  | "pending"
  | "assigned"
  | "running"
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
    | "user_edit"
    | "worker_question"
    | "worker_answer"
    | "artifact_created";
  content: string;
  target_node_id?: string | null;
  artifact_id?: string | null;
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
  completed: { color: "text-green-600", bgColor: "bg-green-100", label: "Completed", icon: "✓" },
  failed:    { color: "text-red-600",   bgColor: "bg-red-100",   label: "Failed",    icon: "✗" },
};
