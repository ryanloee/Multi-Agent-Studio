"use client";

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
  const models: ModelEntry[] = Array.isArray(rawModels) ? rawModels : [];

  // Group models by (format + base_url) as provider key
  const grouped: Record<string, { label: string; models: ModelEntry[] }> = {};
  const providerOrder: string[] = [];

  for (const model of models) {
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

  // Build the full_id format: format/base_url/model_name
  const getOptionValue = (m: ModelEntry) => `${m.format}/${m.base_url}/${m.default_model}`;

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
          {t("model.noModelsHint") || "请先在设置中添加模型"}
        </p>
      )}
    </div>
  );
}
