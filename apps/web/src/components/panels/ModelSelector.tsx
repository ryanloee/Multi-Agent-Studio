"use client";

import { useEffect, useState } from "react";
import type { ModelInfo } from "@/types/api";
import { api } from "@/lib/api";
import { useLocaleStore } from "@/stores/localeStore";

interface ModelSelectorProps {
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

export default function ModelSelector({ value, onChange, disabled }: ModelSelectorProps) {
  const t = useLocaleStore((s) => s.t);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .listModels()
      .then((data) => {
        if (!cancelled && Array.isArray(data) && data.length > 0) {
          setModels(data);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  // Group models by provider, preserving backend order
  const grouped: Record<string, ModelInfo[]> = {};
  const providerOrder: string[] = [];
  for (const model of models) {
    const p = model.provider;
    if (!grouped[p]) {
      grouped[p] = [];
      providerOrder.push(p);
    }
    grouped[p].push(model);
  }

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("model.label")}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled || loading}
        className="w-full rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none disabled:opacity-50"
      >
        <option value="">{t("model.selectPlaceholder")}</option>
        {providerOrder.map((provider) => {
          const items = grouped[provider];
          if (!items || items.length === 0) return null;
          const label = items[0].provider_label ?? provider;
          return (
            <optgroup key={provider} label={label}>
              {items.map((m) => (
                <option key={m.full_id ?? m.id} value={m.full_id ?? m.id}>
                  {m.free ? "🆓 " : ""}{m.name}
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
    </div>
  );
}
