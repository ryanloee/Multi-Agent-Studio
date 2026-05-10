import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AppSettings, GeneralSettings, DisplaySettings, ModelEntry } from "@/types/settings";
import { api } from "@/lib/api";

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
        set({ loading: true });
        try {
          const serverSettings = await api.getSettings();
          // Migration: old format had models: { openai: {}, claude: {} }
          // New format expects models: ModelEntry[]
          if (serverSettings.models && !Array.isArray(serverSettings.models)) {
            serverSettings.models = [];
          }
          set({ settings: { ...DEFAULT_SETTINGS, ...serverSettings, models: serverSettings.models || [] }, loading: false });
        } catch {
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

      addModel: (model) => {
        set((s) => {
          const models = Array.isArray(s.settings.models) ? s.settings.models : [];
          return {
            settings: { ...s.settings, models: [...models, model] },
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
                m.id === id ? { ...m, ...patch } : m
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
        set({ loading: true, saveError: null });
        try {
          const settingsToSave = {
            ...get().settings,
            models: Array.isArray(get().settings.models) ? get().settings.models : [],
          };
          const saved = await api.updateSettings(settingsToSave);
          // Ensure models is always an array after save
          if (!Array.isArray(saved.models)) {
            saved.models = [];
          }
          set({ settings: saved, loading: false, lastSaved: Date.now(), saveError: null });
        } catch (err) {
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
