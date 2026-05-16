import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AppSettings, GeneralSettings, DisplaySettings, ModelEntry, ModelStrategy } from "@/types/settings";
import { api } from "@/lib/api";

const DEFAULT_CONTEXT_WINDOW = 128000;
const DEFAULT_MAX_OUTPUT_TOKENS = 4096;

function normalizeTokenField(
  value: unknown,
  fallback: number,
  minValue: number,
): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed) || parsed < minValue) {
    return fallback;
  }
  return Math.round(parsed);
}

function normalizeModelEntry(model: ModelEntry): ModelEntry {
  return {
    ...model,
    context_window: normalizeTokenField(
      model.context_window,
      DEFAULT_CONTEXT_WINDOW,
      1024,
    ),
    max_output_tokens: normalizeTokenField(
      model.max_output_tokens,
      DEFAULT_MAX_OUTPUT_TOKENS,
      256,
    ),
  };
}

function normalizeModels(models: ModelEntry[] | undefined): ModelEntry[] {
  if (!Array.isArray(models)) return [];
  return models.map(normalizeModelEntry);
}

let settingsRequestSeq = 0;

// ---------------------------------------------------------------------------
// Default settings
// ---------------------------------------------------------------------------

const DEFAULT_SETTINGS: AppSettings = {
  general: {
    language: "zh",
    default_workspace: "",
  },
  display: {
    theme: "system",
    compact_mode: false,
  },
  models: [],
  model_strategy: {
    planner: "",
    design: "",
    review: "",
    merge: "",
    explore: "",
    coder: "",
    shell: "",
  },
};

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface SettingsState {
  settings: AppSettings;
  modalOpen: boolean;
  loading: boolean;
  saveError: string | null;
  lastSaved: number | null;

  openModal: () => void;
  closeModal: () => void;
  loadFromServer: () => Promise<void>;
  updateGeneral: (patch: Partial<GeneralSettings>) => void;
  updateDisplay: (patch: Partial<DisplaySettings>) => void;
  updateModelStrategy: (patch: Partial<ModelStrategy>) => void;
  addModel: (model: ModelEntry) => void;
  updateModel: (id: string, patch: Partial<ModelEntry>) => void;
  removeModel: (id: string) => void;
  save: () => Promise<void>;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set, get) => ({
      settings: { ...DEFAULT_SETTINGS },
      modalOpen: false,
      loading: false,
      saveError: null,
      lastSaved: null,

      openModal: () => {
        set({ modalOpen: true, saveError: null });
        get().loadFromServer();
      },

      closeModal: () => set({ modalOpen: false }),

      loadFromServer: async () => {
        const requestSeq = ++settingsRequestSeq;
        set({ loading: true });
        try {
          const serverSettings = await api.getSettings();
          if (requestSeq !== settingsRequestSeq) return;
          set({
            settings: {
              ...DEFAULT_SETTINGS,
              ...serverSettings,
              models: normalizeModels(serverSettings.models),
            },
            loading: false,
          });
        } catch {
          if (requestSeq !== settingsRequestSeq) return;
          set({ loading: false });
        }
      },

      updateGeneral: (patch) => {
        set((s) => ({
          settings: { ...s.settings, general: { ...s.settings.general, ...patch } },
          saveError: null,
        }));
      },

      updateDisplay: (patch) => {
        set((s) => ({
          settings: { ...s.settings, display: { ...s.settings.display, ...patch } },
          saveError: null,
        }));
      },

      updateModelStrategy: (patch) => {
        set((s) => ({
          settings: {
            ...s.settings,
            model_strategy: { ...s.settings.model_strategy, ...patch },
          },
          saveError: null,
        }));
      },

      addModel: (model) => {
        set((s) => {
          const models = Array.isArray(s.settings.models) ? s.settings.models : [];
          return {
            settings: { ...s.settings, models: [...models, normalizeModelEntry(model)] },
            saveError: null,
          };
        });
      },

      updateModel: (id, patch) => {
        set((s) => {
          const models = Array.isArray(s.settings.models) ? s.settings.models : [];
          return {
            settings: {
              ...s.settings,
              models: models.map((m) =>
                m.id === id ? normalizeModelEntry({ ...m, ...patch }) : m
              ),
            },
            saveError: null,
          };
        });
      },

      removeModel: (id) => {
        set((s) => {
          const models = Array.isArray(s.settings.models) ? s.settings.models : [];
          return {
            settings: { ...s.settings, models: models.filter((m) => m.id !== id) },
            saveError: null,
          };
        });
      },

      save: async () => {
        const requestSeq = ++settingsRequestSeq;
        set({ loading: true, saveError: null });
        try {
          const settingsToSave = {
            ...get().settings,
            models: normalizeModels(get().settings.models),
          };
          const saved = await api.updateSettings(settingsToSave);
          if (requestSeq !== settingsRequestSeq) return;
          set({
            settings: {
              ...DEFAULT_SETTINGS,
              ...saved,
              models: normalizeModels(saved.models),
            },
            loading: false,
            lastSaved: Date.now(),
            saveError: null,
          });
        } catch (err) {
          if (requestSeq !== settingsRequestSeq) return;
          const msg = err instanceof Error ? err.message : "保存失败";
          set({ loading: false, saveError: msg });
          throw err;
        }
      },
    }),
    {
      name: "mas-settings",
      partialize: (state) => ({
        settings: {
          general: state.settings.general,
          display: state.settings.display,
        },
      }),
    }
  )
);
