/** Settings types matching the backend Pydantic models. */

/** A single model provider entry */
export interface ModelEntry {
  id: string;
  name: string;
  format: "openai" | "anthropic";
  base_url: string;
  api_key: string;
  default_model: string;
}

export interface GeneralSettings {
  language: string;
  default_workspace: string;
}

export interface DisplaySettings {
  theme: string;
  compact_mode: boolean;
}

export interface AppSettings {
  general: GeneralSettings;
  display: DisplaySettings;
  models: ModelEntry[];
}

export type SettingsTab = "general" | "display" | "models";

// ---------------------------------------------------------------------------
// Path validation (from POST /api/settings/validate-path)
// ---------------------------------------------------------------------------

export interface PathValidateResult {
  valid: boolean;
  exists: boolean;
  is_dir: boolean;
  is_absolute: boolean;
  message: string;
}

// ---------------------------------------------------------------------------
// Model URL test result (from POST /api/settings/test-model-url)
// ---------------------------------------------------------------------------

export interface ModelTestResult {
  success: boolean;
  status_code: number | null;
  latency_ms: number | null;
  models_count: number | null;
  model_names: string[];
  error: string | null;
}

// ---------------------------------------------------------------------------
// Chat history (from GET /api/planner/history/{workflow_id})
// ---------------------------------------------------------------------------

export interface ChatHistoryItem {
  id: string;
  workflow_id: string;
  node_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string | null;
}
