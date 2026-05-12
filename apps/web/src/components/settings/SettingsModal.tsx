"use client";

import { useState, useCallback, useEffect } from "react";
import {
  X,
  Settings,
  Globe,
  Monitor,
  Cpu,
  Save,
  CheckCircle2,
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  Plus,
  Trash2,
  Wifi,
  WifiOff,
  Play,
} from "lucide-react";
import { useSettingsStore } from "@/stores/settingsStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { SettingsTab, ModelEntry } from "@/types/settings";
import DirectoryPicker from "@/components/common/DirectoryPicker";
import { api } from "@/lib/api";

// ---------------------------------------------------------------------------
// Tab config
// ---------------------------------------------------------------------------

const TABS: { key: SettingsTab; icon: typeof Globe; labelKey: string }[] = [
  { key: "general", icon: Globe, labelKey: "settings.tabGeneral" },
  { key: "display", icon: Monitor, labelKey: "settings.tabDisplay" },
  { key: "models", icon: Cpu, labelKey: "settings.tabModels" },
];

const DEFAULT_CONTEXT_WINDOW = 128000;
const DEFAULT_MAX_OUTPUT_TOKENS = 4096;

function normalizeNumericInput(
  raw: string,
  fallback: number,
  minValue: number,
): number {
  const parsed = Number(raw.trim());
  if (!Number.isFinite(parsed) || parsed < minValue) {
    return fallback;
  }
  return Math.round(parsed);
}

function parseValidNumericInput(
  raw: string,
  minValue: number,
): number | null {
  const parsed = Number(raw.trim());
  if (!Number.isFinite(parsed) || parsed < minValue) {
    return null;
  }
  return Math.round(parsed);
}

// ---------------------------------------------------------------------------
// Helper: generate model ID
// ---------------------------------------------------------------------------

let modelIdCounter = 0;
function nextModelId(): string {
  modelIdCounter += 1;
  return `model_${Date.now()}_${modelIdCounter}`;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function SettingsModal() {
  const t = useLocaleStore((s) => s.t);
  const modalOpen = useSettingsStore((s) => s.modalOpen);
  const closeModal = useSettingsStore((s) => s.closeModal);
  const settings = useSettingsStore((s) => s.settings);
  const loading = useSettingsStore((s) => s.loading);
  const saveError = useSettingsStore((s) => s.saveError);
  const lastSaved = useSettingsStore((s) => s.lastSaved);
  const updateGeneral = useSettingsStore((s) => s.updateGeneral);
  const updateDisplay = useSettingsStore((s) => s.updateDisplay);
  const save = useSettingsStore((s) => s.save);

  const [activeTab, setActiveTab] = useState<SettingsTab>("general");
  const [saving, setSaving] = useState(false);
  const [successMsg, setSuccessMsg] = useState("");

  useEffect(() => {
    if (!lastSaved) return;
    setSuccessMsg(t("settings.saveSuccess"));
    const timer = setTimeout(() => setSuccessMsg(""), 3000);
    return () => clearTimeout(timer);
  }, [lastSaved, t]);

  const handleSave = useCallback(async () => {
    const activeElement = document.activeElement;
    if (activeElement instanceof HTMLElement) {
      activeElement.blur();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
    setSaving(true);
    setSuccessMsg("");
    try {
      await save();
    } catch {
      // saveError is set in the store
    } finally {
      setSaving(false);
    }
  }, [save]);

  if (!modalOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={closeModal} />

      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[85vh] flex overflow-hidden">
        {/* Sidebar tabs */}
        <nav className="w-52 bg-gray-50 border-r border-gray-200 py-6 flex flex-col gap-1 shrink-0">
          <div className="px-5 mb-4 flex items-center gap-2">
            <Settings size={18} className="text-gray-500" />
            <h2 className="text-sm font-bold text-gray-800">{t("settings.title")}</h2>
          </div>
          {TABS.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`flex items-center gap-3 px-5 py-2.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-blue-50 text-blue-700 border-r-2 border-blue-600"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
                }`}
              >
                <Icon size={16} />
                {t(tab.labelKey)}
              </button>
            );
          })}
        </nav>

        {/* Content area */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
            <h3 className="text-base font-semibold text-gray-800">
              {t(`settings.tab${activeTab.charAt(0).toUpperCase() + activeTab.slice(1)}`)}
            </h3>
            <button
              onClick={closeModal}
              className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
            >
              <X size={18} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-6 py-5">
            {activeTab === "general" && (
              <GeneralTab settings={settings} updateGeneral={updateGeneral} />
            )}
            {activeTab === "display" && (
              <DisplayTab settings={settings} updateDisplay={updateDisplay} />
            )}
            {activeTab === "models" && <ModelsTab />}
          </div>

          <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-between bg-gray-50/50">
            <div className="flex items-center gap-2">
              {successMsg && (
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-green-600">
                  <CheckCircle2 size={14} />
                  {successMsg}
                </span>
              )}
              {saveError && (
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-red-600">
                  <AlertCircle size={14} />
                  {saveError}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={closeModal}
                className="px-4 py-2 text-sm font-medium text-gray-600 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors"
              >
                {t("settings.cancel")}
              </button>
              <button
                onClick={handleSave}
                disabled={saving || loading}
                className="inline-flex items-center gap-2 px-5 py-2 text-sm font-semibold rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition-colors shadow-sm disabled:opacity-50"
              >
                {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                {t("settings.save")}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// General Tab
// ---------------------------------------------------------------------------

function GeneralTab({
  settings,
  updateGeneral,
}: {
  settings: import("@/types/settings").AppSettings;
  updateGeneral: (patch: Partial<import("@/types/settings").GeneralSettings>) => void;
}) {
  const t = useLocaleStore((s) => s.t);
  const setLocale = useLocaleStore((s) => s.setLocale);

  return (
    <div className="space-y-6">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1.5">
          {t("settings.language")}
        </label>
        <select
          value={settings.general.language}
          onChange={(e) => {
            updateGeneral({ language: e.target.value });
            setLocale(e.target.value as "zh" | "en");
          }}
          className="w-full max-w-xs rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none transition-all"
        >
          <option value="zh">中文</option>
          <option value="en">English</option>
        </select>
        <p className="mt-1 text-xs text-gray-400">{t("settings.languageDesc")}</p>
      </div>

      <DirectoryPicker
        value={settings.general.default_workspace}
        onChange={(v) => updateGeneral({ default_workspace: v })}
        placeholder={t("settings.defaultWorkspacePlaceholder")}
        label={t("settings.defaultWorkspace")}
      />
      <p className="text-xs text-gray-400 -mt-4">{t("settings.defaultWorkspaceDesc")}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Display Tab
// ---------------------------------------------------------------------------

function DisplayTab({
  settings,
  updateDisplay,
}: {
  settings: import("@/types/settings").AppSettings;
  updateDisplay: (patch: Partial<import("@/types/settings").DisplaySettings>) => void;
}) {
  const t = useLocaleStore((s) => s.t);

  return (
    <div className="space-y-6">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1.5">
          {t("settings.theme")}
        </label>
        <div className="flex gap-3">
          {(["light", "dark", "system"] as const).map((theme) => (
            <button
              key={theme}
              onClick={() => updateDisplay({ theme })}
              className={`flex items-center gap-2 px-4 py-2.5 rounded-lg border text-sm font-medium transition-all ${
                settings.display.theme === theme
                  ? "border-blue-500 bg-blue-50 text-blue-700"
                  : "border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50"
              }`}
            >
              {theme === "light" && <Eye size={14} />}
              {theme === "dark" && <EyeOff size={14} />}
              {theme === "system" && <Monitor size={14} />}
              {t(`settings.theme${theme.charAt(0).toUpperCase() + theme.slice(1)}`)}
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-xs text-gray-400">{t("settings.themeDesc")}</p>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <div>
            <label className="text-sm font-medium text-gray-700">
              {t("settings.compactMode")}
            </label>
            <p className="text-xs text-gray-400 mt-0.5">{t("settings.compactModeDesc")}</p>
          </div>
          <button
            onClick={() => updateDisplay({ compact_mode: !settings.display.compact_mode })}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
              settings.display.compact_mode ? "bg-blue-600" : "bg-gray-300"
            }`}
          >
            <span
              className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${
                settings.display.compact_mode ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Models Tab — left: add form, right: model list with test
// ---------------------------------------------------------------------------

function ModelsTab() {
  const t = useLocaleStore((s) => s.t);
  const rawModels = useSettingsStore((s) => s.settings.models);
  const models: ModelEntry[] = Array.isArray(rawModels) ? rawModels : [];
  const addModel = useSettingsStore((s) => s.addModel);
  const updateModel = useSettingsStore((s) => s.updateModel);
  const removeModel = useSettingsStore((s) => s.removeModel);

  // Form state
  const [formFormat, setFormFormat] = useState<"openai" | "anthropic">("openai");
  const [formBaseUrl, setFormBaseUrl] = useState("https://api.openai.com/v1");
  const [formApiKey, setFormApiKey] = useState("");
  const [formContextWindow, setFormContextWindow] = useState(String(DEFAULT_CONTEXT_WINDOW));
  const [formMaxOutputTokens, setFormMaxOutputTokens] = useState(String(DEFAULT_MAX_OUTPUT_TOKENS));
  const [showApiKey, setShowApiKey] = useState(false);
  const [modelDrafts, setModelDrafts] = useState<Record<string, { contextWindow: string; maxOutputTokens: string }>>({});

  // Fetch state — after connecting, show available models
  const [fetching, setFetching] = useState(false);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());

  // Test state per model in the list
  const [testResults, setTestResults] = useState<Record<string, import("@/types/settings").ModelTestResult>>({});
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setModelDrafts((prev) => {
      const next: Record<string, { contextWindow: string; maxOutputTokens: string }> = {};
      for (const model of models) {
        next[model.id] = prev[model.id] ?? {
          contextWindow: String(model.context_window ?? DEFAULT_CONTEXT_WINDOW),
          maxOutputTokens: String(model.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS),
        };
      }
      return next;
    });
  }, [models]);

  useEffect(() => {
    for (const model of models) {
      const draft = modelDrafts[model.id];
      if (!draft) continue;

      const parsedContext = parseValidNumericInput(draft.contextWindow, 1024);
      if (parsedContext !== null && parsedContext !== model.context_window) {
        updateModel(model.id, { context_window: parsedContext });
      }

      const parsedOutput = parseValidNumericInput(draft.maxOutputTokens, 256);
      if (parsedOutput !== null && parsedOutput !== model.max_output_tokens) {
        updateModel(model.id, { max_output_tokens: parsedOutput });
      }
    }
  }, [modelDrafts, models, updateModel]);

  // Reset form defaults when format changes
  const handleFormatChange = useCallback((format: "openai" | "anthropic") => {
    setFormFormat(format);
    setFetchedModels([]);
    setFetchError(null);
    setSelectedModels(new Set());
    if (format === "openai") {
      setFormBaseUrl("https://api.openai.com/v1");
    } else {
      setFormBaseUrl("https://api.anthropic.com");
    }
  }, []);

  // Fetch available models from API
  const handleFetchModels = useCallback(async () => {
    if (!formBaseUrl.trim()) return;
    setFetching(true);
    setFetchError(null);
    setFetchedModels([]);
    setSelectedModels(new Set());

    try {
      const result = await api.testModelUrl({
        format: formFormat,
        base_url: formBaseUrl.trim(),
        api_key: formApiKey.trim(),
      });
      if (result.success && result.model_names && result.model_names.length > 0) {
        setFetchedModels(result.model_names);
        // Auto-select all by default
        setSelectedModels(new Set(result.model_names));
      } else if (result.success) {
        // Connected but no model list returned (e.g. Anthropic)
        setFetchError(result.error || t("settings.noModelsFetched"));
      } else {
        setFetchError(result.error || t("settings.testFailed"));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : t("settings.testFailed");
      setFetchError(msg);
    } finally {
      setFetching(false);
    }
  }, [formFormat, formBaseUrl, formApiKey, t]);

  // Batch add selected models
  const handleBatchAdd = useCallback(() => {
    if (selectedModels.size === 0) return;
    const existingIds = new Set(models.map((m) => `${m.format}:${m.base_url}:${m.default_model}`));
    for (const modelName of selectedModels) {
      const dedupeKey = `${formFormat}:${formBaseUrl.trim()}:${modelName}`;
      if (existingIds.has(dedupeKey)) continue;
      addModel({
        id: nextModelId(),
        name: modelName,
        format: formFormat,
        base_url: formBaseUrl.trim(),
        api_key: formApiKey.trim(),
        default_model: modelName,
        context_window: normalizeNumericInput(formContextWindow, DEFAULT_CONTEXT_WINDOW, 1024),
        max_output_tokens: normalizeNumericInput(formMaxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 256),
      });
      existingIds.add(dedupeKey);
    }
    // Reset fetch state but keep connection info
    setFetchedModels([]);
    setSelectedModels(new Set());
  }, [selectedModels, formFormat, formBaseUrl, formApiKey, formContextWindow, formMaxOutputTokens, models, addModel]);

  // Add a single custom model (for Anthropic or manual entry)
  const [customModelName, setCustomModelName] = useState("");
  const handleAddCustom = useCallback(() => {
    if (!customModelName.trim() || !formBaseUrl.trim()) return;
    addModel({
      id: nextModelId(),
      name: customModelName.trim(),
      format: formFormat,
      base_url: formBaseUrl.trim(),
      api_key: formApiKey.trim(),
      default_model: customModelName.trim(),
      context_window: normalizeNumericInput(formContextWindow, DEFAULT_CONTEXT_WINDOW, 1024),
      max_output_tokens: normalizeNumericInput(formMaxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 256),
    });
    setCustomModelName("");
  }, [customModelName, formFormat, formBaseUrl, formApiKey, formContextWindow, formMaxOutputTokens, addModel]);

  const commitModelNumericField = useCallback((
    model: ModelEntry,
    key: "context_window" | "max_output_tokens",
  ) => {
    const draft = modelDrafts[model.id];
    const fallback = key === "context_window" ? DEFAULT_CONTEXT_WINDOW : DEFAULT_MAX_OUTPUT_TOKENS;
    const minValue = key === "context_window" ? 1024 : 256;
    const raw =
      key === "context_window"
        ? draft?.contextWindow ?? String(model.context_window ?? fallback)
        : draft?.maxOutputTokens ?? String(model.max_output_tokens ?? fallback);
    const normalized = normalizeNumericInput(raw, fallback, minValue);
    updateModel(model.id, { [key]: normalized });
    setModelDrafts((prev) => ({
      ...prev,
      [model.id]: {
        contextWindow:
          key === "context_window"
            ? String(normalized)
            : (prev[model.id]?.contextWindow ?? String(model.context_window ?? DEFAULT_CONTEXT_WINDOW)),
        maxOutputTokens:
          key === "max_output_tokens"
            ? String(normalized)
            : (prev[model.id]?.maxOutputTokens ?? String(model.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS)),
      },
    }));
  }, [modelDrafts, updateModel]);

  // Toggle model selection
  const toggleModel = useCallback((name: string) => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  // Select/deselect all
  const toggleAll = useCallback(() => {
    if (selectedModels.size === fetchedModels.length) {
      setSelectedModels(new Set());
    } else {
      setSelectedModels(new Set(fetchedModels));
    }
  }, [selectedModels, fetchedModels]);

  // Test a model's URL
  const handleTest = useCallback(async (model: ModelEntry) => {
    setTestingIds((prev) => new Set(prev).add(model.id));
    setTestResults((prev) => { const n = { ...prev }; delete n[model.id]; return n; });

    try {
      const result = await api.testModelUrl({
        format: model.format,
        base_url: model.base_url,
        api_key: model.api_key,
        default_model: model.default_model,
      });
      setTestResults((prev) => ({ ...prev, [model.id]: result }));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "请求失败";
      setTestResults((prev) => ({
        ...prev,
        [model.id]: { success: false, status_code: null, latency_ms: null, models_count: null, model_names: [], error: msg },
      }));
    } finally {
      setTestingIds((prev) => { const n = new Set(prev); n.delete(model.id); return n; });
    }
  }, []);

  return (
    <div className="flex gap-6 min-h-[400px]">
      {/* Left: Add form */}
      <div className="w-[340px] shrink-0 space-y-4">
        <h4 className="text-sm font-semibold text-gray-800">{t("settings.addModel")}</h4>

        {/* Format selector */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            {t("settings.modelFormat")}
          </label>
          <div className="flex rounded-lg border border-gray-200 overflow-hidden">
            <button
              onClick={() => handleFormatChange("openai")}
              className={`flex-1 py-2 text-xs font-medium transition-colors ${
                formFormat === "openai" ? "bg-green-500 text-white" : "bg-white text-gray-500 hover:bg-gray-50"
              }`}
            >
              OpenAI
            </button>
            <button
              onClick={() => handleFormatChange("anthropic")}
              className={`flex-1 py-2 text-xs font-medium border-l border-gray-200 transition-colors ${
                formFormat === "anthropic" ? "bg-orange-500 text-white" : "bg-white text-gray-500 hover:bg-gray-50"
              }`}
            >
              Anthropic
            </button>
          </div>
        </div>

        {/* Base URL */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            {t("settings.baseUrl")}
          </label>
          <input
            type="url"
            value={formBaseUrl}
            onChange={(e) => { setFormBaseUrl(e.target.value); setFetchedModels([]); }}
            placeholder={formFormat === "openai" ? "https://api.openai.com/v1" : "https://api.anthropic.com"}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none placeholder-gray-300"
          />
        </div>

        {/* API Key */}
        <div>
          <label className="block text-xs font-medium text-gray-600 mb-1">
            {t("settings.apiKey")}
          </label>
          <div className="relative">
            <input
              type={showApiKey ? "text" : "password"}
              value={formApiKey}
              onChange={(e) => setFormApiKey(e.target.value)}
              placeholder={formFormat === "openai" ? "sk-..." : "sk-ant-..."}
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 pr-10 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none placeholder-gray-300"
            />
            <button
              type="button"
              onClick={() => setShowApiKey(!showApiKey)}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-600"
            >
              {showApiKey ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        {/* Fetch / Connect button */}
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              {t("settings.contextWindow")}
            </label>
            <input
              type="number"
              min={1024}
              step={1024}
              value={formContextWindow}
              onChange={(e) => setFormContextWindow(e.target.value)}
              onBlur={() => setFormContextWindow(String(
                normalizeNumericInput(formContextWindow, DEFAULT_CONTEXT_WINDOW, 1024)
              ))}
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              {t("settings.maxOutputTokens")}
            </label>
            <input
              type="number"
              min={256}
              step={256}
              value={formMaxOutputTokens}
              onChange={(e) => setFormMaxOutputTokens(e.target.value)}
              onBlur={() => setFormMaxOutputTokens(String(
                normalizeNumericInput(formMaxOutputTokens, DEFAULT_MAX_OUTPUT_TOKENS, 256)
              ))}
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none"
            />
          </div>
        </div>

        <button
          onClick={handleFetchModels}
          disabled={fetching || !formBaseUrl.trim()}
          className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition-colors shadow-sm disabled:opacity-50"
        >
          {fetching ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
          {t("settings.fetchModels")}
        </button>

        {/* Fetched model list for batch add */}
        {fetchedModels.length > 0 && (
          <div className="space-y-2 p-3 bg-blue-50 rounded-lg border border-blue-100">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-blue-700">
                {t("settings.availableModels")} ({fetchedModels.length})
              </span>
              <button
                onClick={toggleAll}
                className="text-xs text-blue-600 hover:text-blue-800"
              >
                {selectedModels.size === fetchedModels.length ? t("settings.deselectAll") : t("settings.selectAll")}
              </button>
            </div>
            <div className="max-h-48 overflow-y-auto space-y-1">
              {fetchedModels.map((name) => (
                <label key={name} className="flex items-center gap-2 py-0.5 cursor-pointer hover:bg-blue-100 rounded px-1">
                  <input
                    type="checkbox"
                    checked={selectedModels.has(name)}
                    onChange={() => toggleModel(name)}
                    className="w-3.5 h-3.5 rounded border-gray-300 text-blue-600 focus:ring-blue-400"
                  />
                  <span className="text-xs text-gray-700 truncate">{name}</span>
                </label>
              ))}
            </div>
            <button
              onClick={handleBatchAdd}
              disabled={selectedModels.size === 0}
              className="w-full inline-flex items-center justify-center gap-1.5 py-2 text-xs font-semibold rounded-lg bg-green-600 text-white hover:bg-green-700 transition-colors shadow-sm disabled:opacity-50"
            >
              <Plus size={12} />
              {t("settings.addSelected")} ({selectedModels.size})
            </button>
          </div>
        )}

        {/* Fetch error */}
        {fetchError && (
          <div className="px-3 py-2 bg-red-50 rounded-lg border border-red-100 text-xs text-red-600">
            {fetchError}
          </div>
        )}

        {/* Manual add for Anthropic or custom models */}
        <div className="pt-2 border-t border-gray-200">
          <p className="text-xs text-gray-500 mb-2">{t("settings.addCustomModel")}</p>
          <div className="flex gap-2">
            <input
              type="text"
              value={customModelName}
              onChange={(e) => setCustomModelName(e.target.value)}
              placeholder="model-name"
              className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-200 focus:outline-none placeholder-gray-300"
              onKeyDown={(e) => { if (e.key === "Enter") handleAddCustom(); }}
            />
            <button
              onClick={handleAddCustom}
              disabled={!customModelName.trim()}
              className="shrink-0 inline-flex items-center gap-1 px-3 py-1.5 text-xs font-semibold rounded-lg border border-gray-300 text-gray-600 hover:bg-gray-50 transition-colors disabled:opacity-50"
            >
              <Plus size={12} />
              {t("settings.addModel")}
            </button>
          </div>
        </div>
      </div>

      {/* Right: Model list */}
      <div className="flex-1 min-w-0 space-y-3">
        <h4 className="text-sm font-semibold text-gray-800">
          {t("settings.modelList")} ({models.length})
        </h4>

        {models.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-gray-400">
            <Cpu size={32} className="mb-3 opacity-40" />
            <p className="text-sm">{t("settings.noModels")}</p>
          </div>
        ) : (
          <div className="space-y-2">
            {models.map((model) => {
              const testResult = testResults[model.id];
              const isTesting = testingIds.has(model.id);
              return (
                <div
                  key={model.id}
                  className="p-3 rounded-lg border border-gray-200 bg-white hover:border-gray-300 transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                        model.format === "openai"
                          ? "bg-green-100 text-green-700"
                          : "bg-orange-100 text-orange-700"
                      }`}
                    >
                      {model.format.toUpperCase()}
                    </span>
                    <span className="text-sm font-medium text-gray-800 truncate">
                      {model.name || model.default_model}
                    </span>
                    <span className="text-xs text-gray-400 truncate flex-1">
                      {model.base_url}
                    </span>

                    <button
                      onClick={() => handleTest(model)}
                      disabled={isTesting}
                      className="shrink-0 p-1.5 rounded-lg text-gray-400 hover:text-blue-600 hover:bg-blue-50 transition-colors disabled:opacity-50"
                      title={t("settings.testUrl")}
                    >
                      {isTesting ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : testResult?.success ? (
                        <Wifi size={14} className="text-green-500" />
                      ) : testResult ? (
                        <WifiOff size={14} className="text-red-400" />
                      ) : (
                        <Play size={14} />
                      )}
                    </button>

                    <button
                      onClick={() => removeModel(model.id)}
                      className="shrink-0 p-1.5 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors"
                      title={t("settings.deleteModel")}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>

                  <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-500">
                    <span>{model.default_model}</span>
                    <span>{t("settings.contextWindowShort")}: {model.context_window ?? 128000}</span>
                    <span>{t("settings.maxOutputShort")}: {model.max_output_tokens ?? 4096}</span>
                    {model.api_key && (
                      <span className="text-gray-300">•••{model.api_key.slice(-4)}</span>
                    )}
                  </div>

                  <div className="grid grid-cols-2 gap-2 mt-2">
                    <label className="text-[10px] text-gray-500">
                      {t("settings.contextWindow")}
                      <input
                        type="number"
                        min={1024}
                        step={1024}
                        value={modelDrafts[model.id]?.contextWindow ?? String(model.context_window ?? DEFAULT_CONTEXT_WINDOW)}
                        onChange={(e) => {
                          const nextValue = e.target.value;
                          setModelDrafts((prev) => ({
                            ...prev,
                            [model.id]: {
                              contextWindow: nextValue,
                              maxOutputTokens: prev[model.id]?.maxOutputTokens ?? String(model.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS),
                            },
                          }));
                          const parsed = parseValidNumericInput(nextValue, 1024);
                          if (parsed !== null) {
                            updateModel(model.id, { context_window: parsed });
                          }
                        }}
                        onBlur={() => commitModelNumericField(model, "context_window")}
                        className="mt-1 w-full rounded border border-gray-200 px-2 py-1 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
                      />
                    </label>
                    <label className="text-[10px] text-gray-500">
                      {t("settings.maxOutputTokens")}
                      <input
                        type="number"
                        min={256}
                        step={256}
                        value={modelDrafts[model.id]?.maxOutputTokens ?? String(model.max_output_tokens ?? DEFAULT_MAX_OUTPUT_TOKENS)}
                        onChange={(e) => {
                          const nextValue = e.target.value;
                          setModelDrafts((prev) => ({
                            ...prev,
                            [model.id]: {
                              contextWindow: prev[model.id]?.contextWindow ?? String(model.context_window ?? DEFAULT_CONTEXT_WINDOW),
                              maxOutputTokens: nextValue,
                            },
                          }));
                          const parsed = parseValidNumericInput(nextValue, 256);
                          if (parsed !== null) {
                            updateModel(model.id, { max_output_tokens: parsed });
                          }
                        }}
                        onBlur={() => commitModelNumericField(model, "max_output_tokens")}
                        className="mt-1 w-full rounded border border-gray-200 px-2 py-1 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
                      />
                    </label>
                  </div>

                  {testResult && (
                    <div
                      className={`mt-2 px-2.5 py-1.5 rounded text-xs ${
                        testResult.success
                          ? "bg-green-50 text-green-700 border border-green-100"
                          : "bg-red-50 text-red-700 border border-red-100"
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        <span>
                          {testResult.success ? t("settings.testSuccess") : t("settings.testFailed")}
                        </span>
                        {testResult.latency_ms != null && <span>{testResult.latency_ms}ms</span>}
                        {testResult.status_code != null && <span>HTTP {testResult.status_code}</span>}
                        {testResult.models_count != null && (
                          <span>{testResult.models_count} {t("settings.modelsAvailable")}</span>
                        )}
                      </div>
                      {testResult.error && <p className="mt-1 text-red-500">{testResult.error}</p>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
