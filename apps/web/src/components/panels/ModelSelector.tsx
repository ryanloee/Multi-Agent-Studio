"use client";

import { useEffect } from "react";
import { useSettingsStore } from "@/stores/settingsStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { ModelEntry } from "@/types/settings";

interface ModelSelectorProps {
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

export default function ModelSelector({ value, onChange, disabled }: ModelSelectorProps) {
  const t = useLocaleStore((s) => s.t);
  const rawModels = useSettingsStore((s) => s.settings.models);
  const loading = useSettingsStore((s) => s.loading);
  const loadFromServer = useSettingsStore((s) => s.loadFromServer);
  const models: ModelEntry[] = Array.isArray(rawModels) ? rawModels : [];

  useEffect(() => {
    if (!loading && models.length === 0) {
      void loadFromServer();
    }
  }, [loadFromServer, loading, models.length]);

  // Group models by (format + base_url) as provider key
  // Only include enabled models
  const grouped: Record<string, { label: string; models: ModelEntry[] }> = {};
  const providerOrder: string[] = [];

  for (const model of models) {
    if (!model.enabled) continue;
    // Use format + base_url as the provider group key
    const providerKey = `${model.format}:${model.base_url}`;
    if (!grouped[providerKey]) {
      grouped[providerKey] = {
        label: `${model.format.toUpperCase()} - ${model.base_url.replace(/^https?:\/\//, "").split("/")[0]}`,
        models: [],
      };
      providerOrder.push(providerKey);
    }
    grouped[providerKey].models.push(model);
  }

  // Store only what executor needs: provider format + model id. The base URL
  // and API key are resolved from settings at runtime.
  const getOptionValue = (m: ModelEntry) => `${m.format}/${m.default_model || m.name}`;

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("model.label")}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="w-full rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none disabled:opacity-50"
      >
        <option value="">{t("model.selectPlaceholder")}</option>
        {providerOrder.map((providerKey) => {
          const group = grouped[providerKey];
          if (!group || group.models.length === 0) return null;
          return (
            <optgroup key={providerKey} label={group.label}>
              {group.models.map((m) => (
                <option key={m.id} value={getOptionValue(m)}>
                  {m.name || m.default_model}
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
      {models.length === 0 && (
        <p className="text-[10px] text-amber-500 mt-0.5">
          {loading ? "正在加载模型配置..." : (t("model.noModelsHint") || "请先在设置中添加模型")}
        </p>
      )}
    </div>
  );
}
