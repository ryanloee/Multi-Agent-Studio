import { create } from "zustand";
import { persist } from "zustand/middleware";
import { translations, type Locale } from "@/lib/i18n";

interface LocaleState {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string) => string;
}

export const useLocaleStore = create<LocaleState>()(
  persist(
    (set, get) => ({
      locale: "zh",
      setLocale: (locale) => set({ locale }),
      t: (key) => translations[get().locale][key] || key,
    }),
    { name: "mas-locale" }
  )
);
