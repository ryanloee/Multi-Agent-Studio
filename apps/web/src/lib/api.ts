import type {
  WorkflowSummary,
  WorkflowDetail,
  CreateWorkflowRequest,
  UpdateWorkflowRequest,
  RunInfo,
  TriggerRunResponse,
  ModelInfo,
  ApiError,
} from "@/types/api";
import type { AppSettings } from "@/types/settings";
import type { PathValidateResult, ModelTestResult } from "@/types/settings";
import { authHeaders } from "@/lib/auth";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "/api";

// ---------------------------------------------------------------------------
// Custom error class — wraps API errors with structured data
// ---------------------------------------------------------------------------
export class ApiRequestError extends Error {
  /** HTTP status code */
  public readonly status: number;
  /** Machine-readable error code from the server (if available) */
  public readonly code: string;
  /** Additional details from the server (if available) */
  public readonly details?: Record<string, unknown>;

  constructor(status: number, message: string, code?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code ?? `HTTP_${status}`;
    this.details = details;
  }

  /** Convert to the ApiError type from types/api.ts */
  toApiError(): ApiError {
    return {
      status: this.status,
      code: this.code,
      message: this.message,
      details: this.details,
    };
  }
}

// ---------------------------------------------------------------------------
// Internal request helper — typed, with unified error handling
// ---------------------------------------------------------------------------
async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(options?.headers || {}),
      },
    });
  } catch (err) {
    // Network-level error (no response at all)
    throw new ApiRequestError(
      0,
      err instanceof Error ? err.message : "Network request failed",
      "NETWORK_ERROR",
    );
  }

  // ---- Handle non-OK responses ----
  if (!response.ok) {
    await handleHttpError(response);
  }

  // ---- Handle 204 No Content (e.g. DELETE, cancel) ----
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as unknown as T;
  }

  // ---- Parse JSON ----
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiRequestError(
      response.status,
      "Failed to parse API response as JSON",
      "PARSE_ERROR",
    );
  }
}

// ---------------------------------------------------------------------------
// Error handler — maps HTTP status codes to user-friendly messages
// ---------------------------------------------------------------------------
async function handleHttpError(response: Response): Promise<never> {
  const { status } = response;

  // Attempt to parse the body as ApiError from the server
  let body: Partial<ApiError> | null = null;
  try {
    body = await response.json();
  } catch {
    // Body is not JSON — ignore
  }

  const serverMessage = body?.message;
  const serverCode = body?.code;
  const serverDetails = body?.details;

  switch (status) {
    case 400:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Bad request. Please check your input.",
        serverCode ?? "BAD_REQUEST",
        serverDetails,
      );

    case 401:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Authentication required. Please log in.",
        serverCode ?? "UNAUTHORIZED",
        serverDetails,
      );

    case 403:
      throw new ApiRequestError(
        status,
        serverMessage ?? "You do not have permission to perform this action.",
        serverCode ?? "FORBIDDEN",
        serverDetails,
      );

    case 404:
      throw new ApiRequestError(
        status,
        serverMessage ?? "The requested resource was not found.",
        serverCode ?? "NOT_FOUND",
        serverDetails,
      );

    case 422:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Validation error. Please check your input.",
        serverCode ?? "VALIDATION_ERROR",
        serverDetails,
      );

    case 429:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Too many requests. Please try again later.",
        serverCode ?? "RATE_LIMITED",
        serverDetails,
      );

    case 500:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Internal server error. Please try again later.",
        serverCode ?? "INTERNAL_ERROR",
        serverDetails,
      );

    case 502:
    case 503:
    case 504:
      throw new ApiRequestError(
        status,
        serverMessage ?? "Service unavailable. Please try again later.",
        serverCode ?? "SERVICE_UNAVAILABLE",
        serverDetails,
      );

    default:
      throw new ApiRequestError(
        status,
        serverMessage ?? `Request failed with status ${status}`,
        serverCode ?? `HTTP_${status}`,
        serverDetails,
      );
  }
}

// ---------------------------------------------------------------------------
// Typed API client
// ---------------------------------------------------------------------------
export const api = {
  // ---- Workflows ----

  /** List all workflows (summary) */
  listWorkflows: (): Promise<WorkflowSummary[]> =>
    request<WorkflowSummary[]>("/workflows"),

  /** Get full workflow detail by ID */
  getWorkflow: (id: string): Promise<WorkflowDetail> =>
    request<WorkflowDetail>(`/workflows/${id}`),

  /** Create a new workflow */
  createWorkflow: (data: CreateWorkflowRequest): Promise<WorkflowDetail> =>
    request<WorkflowDetail>("/workflows", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  /** Update an existing workflow */
  updateWorkflow: (id: string, data: UpdateWorkflowRequest): Promise<WorkflowDetail> =>
    request<WorkflowDetail>(`/workflows/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  /** Delete a workflow */
  deleteWorkflow: (id: string): Promise<void> =>
    request<void>(`/workflows/${id}`, { method: "DELETE" }),

  // ---- Runs ----

  /** Trigger a new run for a workflow */
  triggerRun: (workflowId: string, dag?: { nodes: unknown[]; edges: unknown[] }): Promise<TriggerRunResponse> =>
    request<TriggerRunResponse>(`/runs/${workflowId}/run`, {
      method: "POST",
      body: dag ? JSON.stringify({ dag }) : undefined,
    }),

  /** Get run details by ID */
  getRun: (id: string): Promise<RunInfo> =>
    request<RunInfo>(`/runs/${id}`),

  /** List all runs */
  listRuns: (): Promise<RunInfo[]> =>
    request<RunInfo[]>("/runs"),

  /** Cancel a running workflow */
  cancelRun: (id: string): Promise<void> =>
    request<void>(`/runs/${id}/cancel`, { method: "POST" }),

  /** Approve a paused run (Human-in-the-Loop) */
  approveRun: (id: string): Promise<void> =>
    request<void>(`/runs/${id}/approve`, { method: "POST" }),

  /** Reject a paused run (Human-in-the-Loop) */
  rejectRun: (id: string): Promise<void> =>
    request<void>(`/runs/${id}/reject`, { method: "POST" }),

  /** Get the diff for a paused run (Human-in-the-Loop) */
  getRunDiff: async (id: string): Promise<string> => {
    const data = await request<{ run_id: string; diff: string; node_id?: string }>(`/runs/${id}/diff`);
    return data.diff ?? "";
  },

  // ---- Tasks ----

  /** List all tasks for a run */
  listTasks: (runId: string): Promise<import("@/types/task").Task[]> =>
    request<import("@/types/task").Task[]>(`/runs/${runId}/tasks`),

  /** Get a single task */
  getTask: (runId: string, taskId: string): Promise<import("@/types/task").Task> =>
    request<import("@/types/task").Task>(`/runs/${runId}/tasks/${taskId}`),

  /** Create a new task manually */
  createTask: (
    runId: string,
    body: {
      title: string;
      description?: string;
      assigned_node_id?: string;
      assigned_worker_label?: string;
    },
  ): Promise<import("@/types/task").Task> =>
    request<import("@/types/task").Task>(`/runs/${runId}/tasks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Update a task (user edits) */
  updateTask: (
    runId: string,
    taskId: string,
    patch: Partial<import("@/types/task").Task>,
  ): Promise<import("@/types/task").Task> =>
    request<import("@/types/task").Task>(`/runs/${runId}/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /** Assign a task to a specific node and start execution */
  assignTask: (
    runId: string,
    taskId: string,
    body: {
      node_id: string;
      node_label?: string;
      agent_type?: string;
      model_provider?: string;
      model_id?: string;
      prompt?: string;
    },
  ): Promise<import("@/types/task").Task> =>
    request<import("@/types/task").Task>(`/runs/${runId}/tasks/${taskId}/assign`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Restart a failed/completed task */
  restartTask: (runId: string, taskId: string): Promise<import("@/types/task").Task> =>
    request<import("@/types/task").Task>(`/runs/${runId}/tasks/${taskId}/restart`, {
      method: "POST",
    }),

  /** Send a message to a task */
  sendTaskMessage: (
    runId: string,
    taskId: string,
    body: {
      sender_type: "planner" | "worker" | "user";
      sender_id: string;
      message_type: string;
      content: string;
    },
  ): Promise<import("@/types/task").TaskMessage> =>
    request<import("@/types/task").TaskMessage>(
      `/runs/${runId}/tasks/${taskId}/messages`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  /** List messages for a task */
  listTaskMessages: (
    runId: string,
    taskId: string,
  ): Promise<import("@/types/task").TaskMessage[]> =>
    request<import("@/types/task").TaskMessage[]>(
      `/runs/${runId}/tasks/${taskId}/messages`,
    ),

  // ---- Models ----

  /** List available LLM models */
  listModels: async (): Promise<ModelInfo[]> => {
    const data = await request<{ models: ModelInfo[] }>("/models");
    return data.models ?? [];
  },

  // ---- Settings ----

  /** Get global application settings */
  getSettings: (): Promise<AppSettings> =>
    request<AppSettings>("/settings"),

  /** Update global application settings (partial merge) */
  updateSettings: (settings: AppSettings): Promise<AppSettings> =>
    request<AppSettings>("/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),

  /** Validate a filesystem path */
  validatePath: (path: string): Promise<PathValidateResult> =>
    request<PathValidateResult>("/settings/validate-path", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  /** Test model provider URL connectivity */
  testModelUrl: (params: { format: string; base_url: string; api_key: string; default_model?: string }): Promise<ModelTestResult> =>
    request<ModelTestResult>("/settings/test-model-url", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  // ---- Planner Chat ----

  /** Load persisted chat history for a workflow + node */
  getChatHistory: (workflowId: string, nodeId: string = "planner"): Promise<import("@/types/settings").ChatHistoryItem[]> =>
    request<import("@/types/settings").ChatHistoryItem[]>(`/planner/history/${workflowId}?node_id=${nodeId}`),
};
