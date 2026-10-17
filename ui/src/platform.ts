// 平台判断与快捷键文字。Mac 版是 WKWebView（UA 含 Macintosh），Windows 版是 WebView2
// （UA 含 Windows NT）；两者都不是（例如云端 Linux 上的 Playwright）时按非 Mac 处理。
// 只看 navigator.userAgent，不用已弃用的 navigator.platform。全部是函数，没有依赖。

export function isMac(): boolean {
  return /Macintosh|Mac OS X/i.test(navigator.userAgent);
}

export function isWindows(): boolean {
  return /Windows NT/i.test(navigator.userAgent);
}

/** 修饰键的显示文字：Mac「⌘」，其余「Ctrl」。 */
export function modKeyLabel(): string {
  return isMac() ? "⌘" : "Ctrl";
}

/** 快捷键的显示文字：Mac「⌘K」，其余「Ctrl+K」（Windows 习惯写加号）。 */
export function shortcutLabel(key: string): string {
  return isMac() ? `⌘${key}` : `Ctrl+${key}`;
}

/** aria-keyshortcuts 的写法：Mac「Meta+K」，其余「Control+K」。 */
export function ariaKeyShortcut(key: string): string {
  return isMac() ? `Meta+${key}` : `Control+${key}`;
}

/** 这次按键是否按着本平台的修饰键（Mac 看 ⌘，其余看 Ctrl）。 */
export function isModKey(event: KeyboardEvent): boolean {
  return isMac() ? event.metaKey : event.ctrlKey;
}
