/** Normalize a path pasted from a shell, chat, or a file manager. */
const OUTER_PAIRS: Record<string, string> = {
  "'": "'",
  '"': '"',
  "`": "`",
  "“": "”",
  "‘": "’",
  "「": "」",
  "『": "』",
  "《": "》",
  "〈": "〉",
  "<": ">",
};

export function normalizeUserPath(value: string): string {
  let normalized = value.trim();
  while (normalized.length >= 2) {
    const closing = OUTER_PAIRS[normalized[0]];
    if (!closing || normalized[normalized.length - 1] !== closing) break;
    normalized = normalized.slice(1, -1).trim();
  }
  return normalized;
}
