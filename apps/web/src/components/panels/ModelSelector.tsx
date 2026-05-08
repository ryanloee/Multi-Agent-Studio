"use client";

import { useEffect, useState } from "react";
import type { ModelInfo, ModelProvider } from "@/types/api";
import { api } from "@/lib/api";
import { useLocaleStore } from "@/stores/localeStore";

// ---------------------------------------------------------------------------
// Static fallback models used when the API is unavailable
// ---------------------------------------------------------------------------
const FALLBACK_MODELS: ModelInfo[] = [
  { id: "gpt-4o", name: "GPT-4o", provider: "openai", context_length: 128000, max_tokens: 16384 },
  { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai", context_length: 128000, max_tokens: 16384 },
  { id: "gpt-3.5-turbo", name: "GPT-3.5 Turbo", provider: "openai", context_length: 16384, max_tokens: 4096 },
  { id: "claude-sonnet-4-20250514", name: "Claude Sonnet 4", provider: "anthropic", context_length: 200000, max_tokens: 8192 },
  { id: "claude-3.5-haiku-20241022", name: "Claude 3.5 Haiku", provider: "anthropic", context_length: 200000, max_tokens: 8192 },
  { id: "deepseek-coder-v2", name: "DeepSeek Coder V2", provider: "deepseek", context_length: 128000, max_tokens: 16384 },
  { id: "gemini-2.0-flash", name: "Gemini 2.0 Flash", provider: "google", context_length: 1048576, max_tokens: 8192 },
  { id: "llama3.1:70b", name: "LLaMA 3.1 70B", provider: "ollama", context_length: 128000, max_tokens: 8192 },
  { id: "qwen2.5-coder:32b", name: "Qwen 2.5 Coder 32B", provider: "ollama", context_length: 131072, max_tokens: 8192 },
  { id: "codestral:22b", name: "Codestral 22B", provider: "ollama", context_length: 32768, max_tokens: 8192 },
];

// ---------------------------------------------------------------------------
// Provider display names
// ---------------------------------------------------------------------------
const PROVIDER_LABELS: Record<ModelProvider, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  google: "Google",
  ollama: "Ollama",
};

interface ModelSelectorProps {
  value: string;
  onChange: (modelId: string) => void;
}

export default function ModelSelector({ value, onChange }: ModelSelectorProps) {
  const t = useLocaleStore((s) => s.t);
  const [models, setModels] = useState<ModelInfo[]>(FALLBACK_MODELS);
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
      .catch(() => {
        // Use fallback models on error
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Group models by provider
  const grouped: Partial<Record<ModelProvider, ModelInfo[]>> = {};
  for (const model of models) {
    const provider = model.provider as ModelProvider;
    if (!grouped[provider]) grouped[provider] = [];
    grouped[provider]!.push(model);
  }

  const providerOrder: ModelProvider[] = ["openai", "anthropic", "google", "deepseek", "ollama"];

  return (
    <div className="space-y-1">
      <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
        {t("model.label")}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={loading}
        className="w-full rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none disabled:opacity-50"
      >
        <option value="">{t("model.selectPlaceholder")}</option>
        {providerOrder.map((provider) => {
          const items = grouped[provider];
          if (!items || items.length === 0) return null;
          return (
            <optgroup key={provider} label={PROVIDER_LABELS[provider] ?? provider}>
              {items.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
    </div>
  );
}
