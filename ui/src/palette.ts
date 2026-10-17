// 配色方案（设置 → 外观与语言 → 配色方案）。
//
// 与「主题」（浅色 / 深色 / 跟随系统）是两个独立维度：主题决定 data-theme；浅色模式和深色模式
// 各自选一个配色方案，当前生效的那个写到 data-palette 上。可以跨方案搭配，例如白天用流光、
// 晚上用琥珀护眼。每个方案都有浅色、深色两套 token（暖调纸感在 styles/tokens.css 的 :root，
// 其余在 styles/palettes.css），浅色模式只用到方案的浅色那套，深色同理。
//
// 后端 settings.appearance.palette_light / palette_dark 是权威值；localStorage 只是首屏缓存，
// 让启动时不用等后端就能画对颜色。方案 id 与 settings.py 的 PALETTES 保持一致。
// 数组顺序就是设置页的显示顺序，默认方案排第一。
//
// swatch 只用于设置页里的小色样预览，取自对应 CSS 块的同名 token，改 CSS 时一并改这里。

import { t } from "./i18n";

export type PaletteId = "liuguang" | "warm" | "cool" | "celadon" | "graphite" | "amber";
export type PaletteMode = "light" | "dark";
export type PaletteChoice = Record<PaletteMode, PaletteId>;

export const DEFAULT_PALETTE: PaletteId = "liuguang";
const STORAGE_KEYS: Record<PaletteMode, string> = {
  light: "translator.palette.light",
  dark: "translator.palette.dark",
};

export type PaletteSwatch = { ground: string; surface: string; line: string; ink: string; ink3: string; accent: string };

export const PALETTES: {
  id: PaletteId;
  label: () => string;
  hint: () => string;
  swatch: { light: PaletteSwatch; dark: PaletteSwatch };
}[] = [
  {
    id: "liuguang",
    label: () => t("流光"),
    hint: () => t("电光蓝主色，青色只用于进行中（默认）"),
    swatch: {
      light: { ground: "#f3f4f7", surface: "#ffffff", line: "#e5e7ec", ink: "#101828", ink3: "#5e697d", accent: "#2563eb" },
      dark: { ground: "#0b0e13", surface: "#12161d", line: "#2b3340", ink: "#e8ebf1", ink3: "#8b93a2", accent: "#4d84f5" },
    },
  },
  {
    id: "graphite",
    label: () => t("素雅灰"),
    hint: () => t("纯中性灰，不带色偏"),
    swatch: {
      light: { ground: "#e5e5e5", surface: "#f4f4f4", line: "#c2c2c2", ink: "#1a1a1a", ink3: "#5e5e5e", accent: "#2462c8" },
      dark: { ground: "#171717", surface: "#222222", line: "#4b4b4b", ink: "#ececec", ink3: "#979797", accent: "#82a9ea" },
    },
  },
  {
    id: "warm",
    label: () => t("暖调纸感"),
    hint: () => t("暖米白纸色，夜间不刺眼"),
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

function readStoredPalette(mode: PaletteMode): PaletteId {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEYS[mode]);
    return isPaletteId(stored) ? stored : DEFAULT_PALETTE;
  } catch {
    return DEFAULT_PALETTE;
  }
}

// 当前可见的选择（不一定已落盘：设置页先切颜色给反馈，落盘失败再回滚）。
let visible: PaletteChoice = { light: DEFAULT_PALETTE, dark: DEFAULT_PALETTE };

function currentMode(): PaletteMode {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function syncAttribute(): void {
  document.documentElement.dataset.palette = visible[currentMode()];
}

/**
 * 启动时调用一次：按 localStorage 缓存画出首屏，并在 data-theme 变化时（手动切换主题、
 * 跟随系统时系统切换外观）自动换成对应模式的方案，调用方不用各自记得同步。
 */
export function initPalettes(): void {
  visible = { light: readStoredPalette("light"), dark: readStoredPalette("dark") };
  syncAttribute();
  new MutationObserver(syncAttribute).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
}

// 只改可见配色，不碰 localStorage：和主题一样，落盘成功之后才写缓存（见 settings.ts）。
export function applyPalettes(choice: PaletteChoice): void {
  visible = { ...choice };
  syncAttribute();
}

export function storePalettes(choice: PaletteChoice): void {
  try {
    window.localStorage.setItem(STORAGE_KEYS.light, choice.light);
    window.localStorage.setItem(STORAGE_KEYS.dark, choice.dark);
  } catch {
    // 写不进缓存只影响下次启动的首屏颜色，启动后会按后端设置纠正。
  }
}

/** 从后端 appearance 里取两个方案，缺失或不认识的值按默认方案。 */
export function palettesFromAppearance(appearance: Record<string, unknown> | undefined): PaletteChoice {
  const light = appearance?.palette_light;
  const dark = appearance?.palette_dark;
  return {
    light: isPaletteId(light) ? light : DEFAULT_PALETTE,
    dark: isPaletteId(dark) ? dark : DEFAULT_PALETTE,
  };
}
