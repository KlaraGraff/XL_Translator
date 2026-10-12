// 配色方案（设置 → 外观与语言 → 配色方案）。
//
// 与「主题」（浅色 / 深色 / 跟随系统）是两个独立维度：主题决定 data-theme，配色方案决定
// data-palette，每个方案都有浅色、深色两套 token（默认方案在 styles/tokens.css，其余在
// styles/palettes.css）。后端 settings.appearance.palette 是权威值；localStorage 只是首屏
// 缓存，让启动时不用等后端就能画对颜色。方案 id 与 settings.py 的 PALETTES 保持一致。
//
// swatch 只用于设置页里的小色样预览，取自对应 CSS 块的同名 token，改 CSS 时一并改这里。

import { t } from "./i18n";

export type PaletteId = "warm" | "cool" | "celadon" | "graphite" | "amber";

export const DEFAULT_PALETTE: PaletteId = "warm";
export const PALETTE_STORAGE_KEY = "translator.palette";

export type PaletteSwatch = { ground: string; surface: string; line: string; ink: string; ink3: string; accent: string };

export const PALETTES: {
  id: PaletteId;
  label: () => string;
  hint: () => string;
  swatch: { light: PaletteSwatch; dark: PaletteSwatch };
}[] = [
  {
    id: "warm",
    label: () => t("暖调纸感"),
    hint: () => t("暖米白纸色，夜间不刺眼（默认）"),
    swatch: {
      light: { ground: "#e9e7e1", surface: "#f6f5f1", line: "#cbc7bc", ink: "#1d1c19", ink3: "#64605a", accent: "#4c5ce6" },
      dark: { ground: "#1c1b19", surface: "#2a2926", line: "#524f4a", ink: "#efede8", ink3: "#9c978d", accent: "#9098d2" },
    },
  },
  {
    id: "cool",
    label: () => t("冷调柔光"),
    hint: () => t("蓝灰冷色，保留原来的风格"),
    swatch: {
      light: { ground: "#e6e9f0", surface: "#f4f6f9", line: "#c6ccda", ink: "#161b2c", ink3: "#58617a", accent: "#4c5de8" },
      dark: { ground: "#151a23", surface: "#1e2431", line: "#424c63", ink: "#eef1f8", ink3: "#8f99b2", accent: "#8f9dff" },
    },
  },
  {
    id: "celadon",
    label: () => t("青瓷"),
    hint: () => t("灰绿底色配青绿主色，最柔和"),
    swatch: {
      light: { ground: "#e2e8e4", surface: "#f2f5f2", line: "#bdc9c2", ink: "#17201c", ink3: "#56635c", accent: "#1b7870" },
      dark: { ground: "#161c1a", surface: "#1f2724", line: "#46554e", ink: "#e9efec", ink3: "#8f9e96", accent: "#72b8ad" },
    },
  },
  {
    id: "graphite",
    label: () => t("石墨"),
    hint: () => t("纯中性灰，不带色偏"),
    swatch: {
      light: { ground: "#e5e5e5", surface: "#f4f4f4", line: "#c2c2c2", ink: "#1a1a1a", ink3: "#5e5e5e", accent: "#2462c8" },
      dark: { ground: "#171717", surface: "#222222", line: "#4b4b4b", ink: "#ececec", ink3: "#979797", accent: "#82a9ea" },
    },
  },
  {
    id: "amber",
    label: () => t("琥珀护眼"),
    hint: () => t("米黄纸色，蓝光最少，适合夜间长时间使用"),
    swatch: {
      light: { ground: "#e8dfcf", surface: "#f4eddf", line: "#c8b89c", ink: "#2a2116", ink3: "#665744", accent: "#9f4e18" },
      dark: { ground: "#1d1813", surface: "#28211a", line: "#534838", ink: "#f0e6d6", ink3: "#a5967f", accent: "#d99b62" },
    },
  },
];

export function isPaletteId(value: unknown): value is PaletteId {
  return typeof value === "string" && PALETTES.some((palette) => palette.id === value);
}

export function readStoredPalette(): PaletteId {
  try {
    const stored = window.localStorage.getItem(PALETTE_STORAGE_KEY);
    return isPaletteId(stored) ? stored : DEFAULT_PALETTE;
  } catch {
    return DEFAULT_PALETTE;
  }
}

// 只改可见配色，不碰 localStorage：和主题一样，落盘成功之后才写缓存（见 settings.ts）。
export function applyPalette(palette: PaletteId): void {
  document.documentElement.dataset.palette = palette;
}

export function storePalette(palette: PaletteId): void {
  try {
    window.localStorage.setItem(PALETTE_STORAGE_KEY, palette);
  } catch {
    // 写不进缓存只影响下次启动的首屏颜色，启动后会按后端设置纠正。
  }
}
