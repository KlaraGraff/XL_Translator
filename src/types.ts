export type PageId = "excel" | "word" | "tm" | "settings" | "diagnostics";

export type WorkerResult<T> = Promise<T>;

export interface EngineSettings {
  mode: "cloud" | "local";
  cloud_provider: string;
  cloud_model: string;
  cloud_base_url: string;
  ollama_model: string;
  concurrency: number;
  ollama_concurrency: number;
  concurrency_unlocked: boolean;
  batch_size: number;
}

export interface AppSettings {
  engine: EngineSettings;
  tm: { max_len: number };
  output: {
    keep_original_sheets: boolean;
    formula_display_value_backfill: boolean;
    enable_print_guard: boolean;
    use_custom_output_dir: boolean;
    custom_output_dir: string;
    enable_excel_autofit: boolean;
    lock_row_height: boolean;
    enable_task_log: boolean;
  };
  word_batch: {
    max_paragraphs_per_batch: number;
    max_chars_per_batch: number;
    split_paragraph_chars: number;
    strict_retry_attempts: number;
  };
  word_review: {
    highlight_unresolved: boolean;
    highlight_color: string;
  };
  settings_version: number;
  source_lang: string;
  target_lang: string;
  custom_target_langs: unknown[];
  recent_target_langs: string[];
  domain_preset: string;
  custom_prompt: string;
  last_source_folder: string;
  cleaner_mode: string;
  cleaner_engine: string;
  cleaner_model: string;
  auto_pin_after_clean: boolean;
  cleaner_prompt_extras: Record<string, string>;
  cleaner_full_prompt_overrides: Record<string, string>;
  domain_name_overrides: Record<string, string>;
  domain_prompt_overrides: Record<string, string>;
}

export interface BootstrapPayload {
  app: { name: string; version: string };
  settings: AppSettings;
  keys: Record<string, boolean>;
  engine: {
    cloudEngines: Record<string, string>;
    defaultCloudProvider: string;
    defaultCloudModel: string;
    defaultCustomOpenAiBaseUrl: string;
    ollamaRecommendedModels: string[];
    cloudBatchRange: [number, number];
    localBatchRange: [number, number];
    cloudConcurrencyRange: [number, number];
    localConcurrencyRange: [number, number];
    unlockedCloudConcurrencyRange: [number, number];
    unlockedLocalConcurrencyRange: [number, number];
    defaultCloudConcurrency: number;
    defaultLocalConcurrency: number;
  };
  wordBatch: {
    paragraphs: [number, number, number];
    chars: [number, number, number];
    splitChars: [number, number, number];
    retryAttempts: [number, number, number];
  };
  languages: {
    source: Record<string, string>;
    target: Record<string, string>;
    defaultSource: string;
    defaultTarget: string;
  };
  tm: {
    langPair: string;
    stats: Record<string, number>;
    pinCount: Record<string, number>;
  };
  domain: {
    presets: Record<string, Record<string, string>>;
  };
}

export interface TaskResult {
  source_path?: string;
  output_dir?: string;
  file_results?: Array<Record<string, unknown>>;
  successful_outputs?: string[];
  elapsed_sec?: number;
  tm_hit_count?: number;
  api_call_count?: number;
  file_count?: number;
  report_path?: string;
  issues?: Array<Record<string, unknown>>;
}

export interface ScanResult {
  path: string;
  count: number;
  items: Array<{
    path: string;
    name: string;
    sizeKb: number;
    sheets?: string[];
  }>;
}

export interface TaskLogEntry {
  id: string;
  level: string;
  message: string;
  ts?: string;
}

export interface TaskEvent {
  taskId: string;
  taskType: string;
  type?: string;
  level?: string;
  message?: string;
  phase_name?: string;
  step_done?: number;
  step_total?: number;
}

export interface TmSearchResult {
  langPair: string;
  keyword: string;
  page: number;
  pageSize: number;
  total: number;
  rows: Array<{
    id: number;
    source_text: string;
    target_text: string;
    word_type: string;
    source_engine: string;
    updated_at: string;
    pinned: number;
  }>;
  stats: Record<string, number>;
  pinCount: Record<string, number>;
}

export interface UpdateCheckResult {
  ok: boolean;
  status: "available" | "current" | "error" | string;
  message: string;
  current_version: string;
  latest_version?: string;
  latest_tag?: string;
  release_url?: string;
  asset_name?: string;
  download_url?: string;
}
