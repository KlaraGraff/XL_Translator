import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { openPath, openUrl } from "@tauri-apps/plugin-opener";
import {
  Activity,
  BookOpen,
  CheckCircle2,
  Cloud,
  Database,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  FolderOpen,
  Gauge,
  Languages,
  RefreshCcw,
  Settings,
  Sparkles,
  Terminal,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { restartWorker, workerInvoke } from "./tauri";
import type {
  AppSettings,
  BootstrapPayload,
  PageId,
  ScanResult,
  TaskEvent,
  TaskLogEntry,
  TaskResult,
  TmSearchResult,
  UpdateCheckResult,
} from "./types";

const navItems: Array<{ id: PageId; label: string; icon: typeof FileSpreadsheet }> = [
  { id: "excel", label: "表格翻译", icon: FileSpreadsheet },
  { id: "word", label: "Word 翻译", icon: FileText },
  { id: "tm", label: "记忆库", icon: Database },
  { id: "settings", label: "设置", icon: Settings },
  { id: "diagnostics", label: "诊断", icon: Terminal },
];

function App() {
  const [activePage, setActivePage] = useState<PageId>("excel");
  const [bootstrap, setBootstrap] = useState<BootstrapPayload | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [excelPath, setExcelPath] = useState("");
  const [wordPath, setWordPath] = useState("");
  const [excelScan, setExcelScan] = useState<ScanResult | null>(null);
  const [wordScan, setWordScan] = useState<ScanResult | null>(null);
  const [taskRunning, setTaskRunning] = useState(false);
  const [taskStatus, setTaskStatus] = useState("就绪");
  const [taskLogs, setTaskLogs] = useState<TaskLogEntry[]>([]);
  const [lastTaskResult, setLastTaskResult] = useState<TaskResult | null>(null);
  const [tmResult, setTmResult] = useState<TmSearchResult | null>(null);
  const [tmKeyword, setTmKeyword] = useState("");
  const [tmDraft, setTmDraft] = useState({ source: "", target: "" });
  const [apiKeyDraft, setApiKeyDraft] = useState("");
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResult | null>(null);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    void loadBootstrap();
    const unlisteners = [
      listen<TaskEvent>("task.event", (event) => {
        const payload = event.payload;
        if (payload.type === "log") {
          appendLog(payload.level || "INFO", payload.message || "");
        }
        if (payload.type === "status" && payload.message) {
          setTaskStatus(payload.message);
        }
        if (payload.type === "progress") {
          setTaskStatus(`${payload.phase_name || "处理中"} ${payload.step_done || 0}/${payload.step_total || 0}`);
        }
      }),
      listen<{ taskId: string; taskType: string; result: TaskResult }>("task.completed", (event) => {
        setTaskRunning(false);
        setTaskStatus("任务完成");
        setLastTaskResult(event.payload.result || null);
        if (event.payload.result?.output_dir) {
          appendLog("OK", `输出目录：${event.payload.result.output_dir}`);
        }
        appendLog("OK", `${event.payload.taskType === "word" ? "Word" : "Excel"} 翻译完成`);
      }),
      listen<{ message: string; detail?: string }>("task.failed", (event) => {
        setTaskRunning(false);
        setTaskStatus("任务失败");
        appendLog("ERROR", event.payload.message || "任务失败");
      }),
      listen<{ level: string; message: string }>("worker-log", (event) => {
        appendLog(event.payload.level || "INFO", event.payload.message || "");
      }),
    ];
    return () => {
      unlisteners.forEach((promise) => {
        void promise.then((unlisten) => unlisten());
      });
    };
  }, []);

  const providerLabel = useMemo(() => {
    if (!bootstrap || !settings) return "";
    const match = Object.entries(bootstrap.engine.cloudEngines).find(
      ([, value]) => value === settings.engine.cloud_provider
    );
    return match?.[0] || settings.engine.cloud_provider;
  }, [bootstrap, settings]);

  async function loadBootstrap() {
    try {
      const payload = await workerInvoke<BootstrapPayload>("app.bootstrap");
      setBootstrap(payload);
      setSettings(payload.settings);
      setNotice("");
    } catch (error) {
      setNotice(`无法连接 Python worker：${String(error)}`);
    }
  }

  function appendLog(level: string, message: string) {
    if (!message) return;
    setTaskLogs((current) => [
      { id: `${Date.now()}-${Math.random()}`, level, message, ts: new Date().toLocaleTimeString() },
      ...current,
    ].slice(0, 120));
  }

  function updateSettings(mutator: (draft: AppSettings) => void) {
    setSettings((current) => {
      if (!current) return current;
      const draft = structuredClone(current);
      mutator(draft);
      return draft;
    });
  }

  async function persistSettings() {
    if (!settings) return;
    const saved = await workerInvoke<AppSettings>("settings.save", { settings });
    setSettings(saved);
    setNotice("设置已保存");
  }

  async function choosePath(kind: "excel" | "word", directory = false) {
    const selected = await open({
      directory,
      multiple: false,
      filters: directory
        ? undefined
        : [
            kind === "excel"
              ? { name: "Excel", extensions: ["xlsx", "xls"] }
              : { name: "Word", extensions: ["docx"] },
          ],
    });
    if (typeof selected !== "string") return;
    if (kind === "excel") setExcelPath(selected);
    else setWordPath(selected);
  }

  async function scan(kind: "excel" | "word") {
    const path = kind === "excel" ? excelPath : wordPath;
    if (!path) return;
    const result = await workerInvoke<ScanResult>(kind === "excel" ? "excel.scan" : "word.scan", { path });
    if (kind === "excel") setExcelScan(result);
    else setWordScan(result);
  }

  async function startTask(kind: "excel" | "word") {
    if (!settings) return;
    const path = kind === "excel" ? excelPath : wordPath;
    if (!path) {
      setNotice("请先选择文件或文件夹");
      return;
    }
    setTaskRunning(true);
    setTaskStatus("正在启动任务...");
    setTaskLogs([]);
    setLastTaskResult(null);
    await workerInvoke(kind === "excel" ? "task.start_excel" : "task.start_word", {
      path,
      settings,
    });
  }

  async function searchTm() {
    const result = await workerInvoke<TmSearchResult>("tm.search", {
      keyword: tmKeyword,
      page: 1,
      pageSize: 80,
    });
    setTmResult(result);
  }

  async function addTmEntry() {
    if (!tmResult) await searchTm();
    const langPair = tmResult?.langPair || bootstrap?.tm.langPair;
    if (!langPair || !tmDraft.source || !tmDraft.target) return;
    await workerInvoke("tm.add", {
      langPair,
      source: tmDraft.source,
      target: tmDraft.target,
    });
    setTmDraft({ source: "", target: "" });
    await searchTm();
  }

  async function runConnectivityCheck() {
    if (!settings) return;
    const result = await workerInvoke<{ ok: boolean; message: string }>("connectivity.check", { settings });
    setNotice(result.message);
  }

  async function runUpdateCheck() {
    const result = await workerInvoke<UpdateCheckResult>("updates.check");
    setUpdateInfo(result);
    setNotice(result.message);
  }

  async function openUpdateDownload() {
    const url = updateInfo?.download_url || updateInfo?.release_url;
    if (!url) return;
    await openUrl(url);
  }

  async function openResultPath(path?: string) {
    if (!path) return;
    await openPath(path);
  }

  async function saveApiKey() {
    if (!settings) return;
    const result = await workerInvoke<{ provider: string; present: boolean }>("settings.save_key", {
      provider: settings.engine.cloud_provider,
      apiKey: apiKeyDraft,
    });
    setApiKeyDraft("");
    setNotice(`${result.provider} API Key 已${result.present ? "保存" : "清除"}`);
    await loadBootstrap();
  }

  if (!bootstrap || !settings) {
    return (
      <main className="boot-screen">
        <div className="boot-card">
          <Sparkles size={32} />
          <h1>XL Translator</h1>
          <p>{notice || "正在启动 Python worker..."}</p>
          <button onClick={() => void restartWorker().then(loadBootstrap)}>重新连接</button>
        </div>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">XL</div>
          <div>
            <h1>{bootstrap.app.name}</h1>
            <span>V{bootstrap.app.version}</span>
          </div>
        </div>
        <nav className="nav-list">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={activePage === item.id ? "nav-item active" : "nav-item"}
                key={item.id}
                onClick={() => setActivePage(item.id)}
              >
                <Icon size={18} />
                {item.label}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <span>{providerLabel}</span>
          <strong>{settings.engine.mode === "local" ? settings.engine.ollama_model : settings.engine.cloud_model}</strong>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Desktop Console</p>
            <h2>{navItems.find((item) => item.id === activePage)?.label}</h2>
          </div>
          <div className="status-pill">
            <Activity size={16} />
            {taskStatus}
          </div>
        </header>

        {notice && <div className="notice">{notice}</div>}

        {activePage === "excel" && (
          <TranslatePage
            title="表格翻译工作区"
            description="扫描 Excel 文件或文件夹，按当前引擎配置执行双语翻译。"
            path={excelPath}
            setPath={setExcelPath}
            onChooseFile={() => choosePath("excel")}
            onChooseFolder={() => choosePath("excel", true)}
            onScan={() => scan("excel")}
            onRun={() => startTask("excel")}
            scanResult={excelScan}
            taskResult={lastTaskResult}
            onOpenPath={openResultPath}
            running={taskRunning}
          />
        )}

        {activePage === "word" && (
          <WordPage
            settings={settings}
            updateSettings={updateSettings}
            path={wordPath}
            setPath={setWordPath}
            onChooseFile={() => choosePath("word")}
            onChooseFolder={() => choosePath("word", true)}
            onScan={() => scan("word")}
            onRun={() => startTask("word")}
            scanResult={wordScan}
            taskResult={lastTaskResult}
            onOpenPath={openResultPath}
            running={taskRunning}
          />
        )}

        {activePage === "tm" && (
          <TmPage
            result={tmResult}
            keyword={tmKeyword}
            setKeyword={setTmKeyword}
            onSearch={searchTm}
            draft={tmDraft}
            setDraft={setTmDraft}
            onAdd={addTmEntry}
          />
        )}

        {activePage === "settings" && (
          <SettingsPage
            bootstrap={bootstrap}
            settings={settings}
            updateSettings={updateSettings}
            onSave={persistSettings}
            onConnectivityCheck={runConnectivityCheck}
            onUpdateCheck={runUpdateCheck}
            updateInfo={updateInfo}
            onDownloadUpdate={openUpdateDownload}
            apiKeyDraft={apiKeyDraft}
            setApiKeyDraft={setApiKeyDraft}
            onSaveApiKey={saveApiKey}
          />
        )}

        {activePage === "diagnostics" && (
          <DiagnosticsPage logs={taskLogs} bootstrap={bootstrap} onRestart={() => restartWorker().then(loadBootstrap)} />
        )}
      </section>
    </main>
  );
}

function TranslatePage(props: {
  title: string;
  description: string;
  path: string;
  setPath: (value: string) => void;
  onChooseFile: () => void;
  onChooseFolder: () => void;
  onScan: () => void;
  onRun: () => void;
  scanResult: ScanResult | null;
  taskResult: TaskResult | null;
  onOpenPath: (path?: string) => void;
  running: boolean;
}) {
  return (
    <div className="page-grid">
      <section className="panel primary-panel">
        <div className="section-heading">
          <div>
            <h3>{props.title}</h3>
            <p>{props.description}</p>
          </div>
          <Languages size={22} />
        </div>
        <PathPicker {...props} />
      </section>
      <ScanPanel result={props.scanResult} />
      <ResultPanel result={props.taskResult} onOpenPath={props.onOpenPath} />
    </div>
  );
}

function WordPage(props: {
  settings: AppSettings;
  updateSettings: (mutator: (draft: AppSettings) => void) => void;
  path: string;
  setPath: (value: string) => void;
  onChooseFile: () => void;
  onChooseFolder: () => void;
  onScan: () => void;
  onRun: () => void;
  scanResult: ScanResult | null;
  taskResult: TaskResult | null;
  onOpenPath: (path?: string) => void;
  running: boolean;
}) {
  const batch = props.settings.word_batch;
  return (
    <div className="page-grid">
      <section className="panel primary-panel">
        <div className="section-heading">
          <div>
            <h3>Word 翻译工作区</h3>
            <p>扫描 DOCX 文件，执行批次翻译、严格重试与语义恢复。</p>
          </div>
          <BookOpen size={22} />
        </div>
        <PathPicker {...props} />
      </section>
      <section className="panel">
        <div className="section-heading compact">
          <h3>高级批次策略</h3>
          <Gauge size={20} />
        </div>
        <div className="number-grid">
          <NumberField label="每批最多段落" min={1} max={16} value={batch.max_paragraphs_per_batch}
            onChange={(value) => props.updateSettings((draft) => { draft.word_batch.max_paragraphs_per_batch = value; })} />
          <NumberField label="每批字符上限" min={800} max={12000} step={100} value={batch.max_chars_per_batch}
            onChange={(value) => props.updateSettings((draft) => { draft.word_batch.max_chars_per_batch = value; })} />
          <NumberField label="长段拆分阈值" min={1500} max={30000} step={500} value={batch.split_paragraph_chars}
            onChange={(value) => props.updateSettings((draft) => { draft.word_batch.split_paragraph_chars = value; })} />
          <NumberField label="失败重试次数" min={1} max={8} value={batch.strict_retry_attempts}
            onChange={(value) => props.updateSettings((draft) => { draft.word_batch.strict_retry_attempts = value; })} />
        </div>
      </section>
      <ScanPanel result={props.scanResult} />
      <ResultPanel result={props.taskResult} onOpenPath={props.onOpenPath} />
    </div>
  );
}

function PathPicker(props: {
  path: string;
  setPath: (value: string) => void;
  onChooseFile: () => void;
  onChooseFolder: () => void;
  onScan: () => void;
  onRun: () => void;
  running: boolean;
}) {
  return (
    <div className="path-card">
      <label>文件或文件夹路径</label>
      <div className="path-row">
        <input value={props.path} onChange={(event) => props.setPath(event.target.value)} placeholder="选择文件或文件夹..." />
        <button className="icon-button" onClick={props.onChooseFile} title="选择文件"><FileText size={18} /></button>
        <button className="icon-button" onClick={props.onChooseFolder} title="选择文件夹"><FolderOpen size={18} /></button>
      </div>
      <div className="action-row">
        <button className="secondary" onClick={props.onScan}>扫描</button>
        <button className="primary" disabled={props.running} onClick={props.onRun}>
          {props.running ? "运行中" : "开始翻译"}
        </button>
      </div>
    </div>
  );
}

function ScanPanel({ result }: { result: ScanResult | null }) {
  return (
    <section className="panel scan-panel">
      <div className="section-heading compact">
        <h3>扫描结果</h3>
        <CheckCircle2 size={20} />
      </div>
      {!result ? (
        <p className="empty">尚未扫描。</p>
      ) : (
        <>
          <div className="metric-row">
            <Metric label="文件数" value={String(result.count)} />
            <Metric label="来源" value={result.path ? "已选择" : "未选择"} />
          </div>
          <div className="result-list">
            {result.items.map((item) => (
              <div className="result-item" key={item.path}>
                <strong>{item.name}</strong>
                <span>{item.sizeKb} KB</span>
                {item.sheets && <small>{item.sheets.join(" / ")}</small>}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function ResultPanel(props: { result: TaskResult | null; onOpenPath: (path?: string) => void }) {
  const outputs = props.result?.successful_outputs || [];
  if (!props.result) return null;

  return (
    <section className="panel result-panel">
      <div className="section-heading compact">
        <h3>任务结果</h3>
        <CheckCircle2 size={20} />
      </div>
      <div className="metric-row">
        <Metric label="处理文件" value={String(props.result.file_count || outputs.length || 0)} />
        <Metric label="API 调用" value={String(props.result.api_call_count || 0)} />
      </div>
      {props.result.output_dir && (
        <div className="output-box">
          <span>{props.result.output_dir}</span>
          <button className="secondary" onClick={() => props.onOpenPath(props.result?.output_dir)}>
            <FolderOpen size={16} />
            打开目录
          </button>
        </div>
      )}
      {outputs.length > 0 && (
        <div className="result-list compact-list">
          {outputs.slice(0, 8).map((path) => (
            <button className="file-link" key={path} onClick={() => props.onOpenPath(path)}>
              <FileText size={15} />
              <span>{path}</span>
            </button>
          ))}
        </div>
      )}
      {props.result.report_path && (
        <button className="secondary" onClick={() => props.onOpenPath(props.result?.report_path)}>
          <ExternalLink size={16} />
          打开质检报告
        </button>
      )}
    </section>
  );
}

function promptForPreset(bootstrap: BootstrapPayload, presetName: string, targetLang: string) {
  const preset = bootstrap.domain.presets[presetName] || {};
  return preset[targetLang] || preset._base || "";
}

function concurrencyRange(bootstrap: BootstrapPayload, settings: AppSettings): [number, number] {
  if (settings.engine.mode === "local") {
    return settings.engine.concurrency_unlocked
      ? bootstrap.engine.unlockedLocalConcurrencyRange
      : bootstrap.engine.localConcurrencyRange;
  }
  return settings.engine.concurrency_unlocked
    ? bootstrap.engine.unlockedCloudConcurrencyRange
    : bootstrap.engine.cloudConcurrencyRange;
}

function SettingsPage(props: {
  bootstrap: BootstrapPayload;
  settings: AppSettings;
  updateSettings: (mutator: (draft: AppSettings) => void) => void;
  onSave: () => void;
  onConnectivityCheck: () => void;
  onUpdateCheck: () => void;
  updateInfo: UpdateCheckResult | null;
  onDownloadUpdate: () => void;
  apiKeyDraft: string;
  setApiKeyDraft: (value: string) => void;
  onSaveApiKey: () => void;
}) {
  const engine = props.settings.engine;
  const isHermes = engine.mode === "cloud" && engine.cloud_provider === "hermes";
  const batchRange = engine.mode === "cloud" ? props.bootstrap.engine.cloudBatchRange : props.bootstrap.engine.localBatchRange;
  const [concMin, concMax] = concurrencyRange(props.bootstrap, props.settings);
  const concurrencyValue = engine.mode === "local" ? engine.ollama_concurrency : engine.concurrency;
  const domainNames = Object.keys(props.bootstrap.domain.presets);
  const presetDefaultPrompt = promptForPreset(props.bootstrap, props.settings.domain_preset, props.settings.target_lang);
  const promptValue = props.settings.domain_preset === "自定义"
    ? props.settings.custom_prompt
    : props.settings.domain_prompt_overrides[props.settings.domain_preset] || presetDefaultPrompt;

  async function chooseOutputDir() {
    const selected = await open({ directory: true, multiple: false });
    if (typeof selected !== "string") return;
    props.updateSettings((draft) => {
      draft.output.use_custom_output_dir = true;
      draft.output.custom_output_dir = selected;
    });
  }

  function updateConcurrency(value: number) {
    props.updateSettings((draft) => {
      if (draft.engine.mode === "local") draft.engine.ollama_concurrency = value;
      else draft.engine.concurrency = value;
    });
  }

  function updateDomainPrompt(value: string) {
    props.updateSettings((draft) => {
      if (draft.domain_preset === "自定义") {
        draft.custom_prompt = value;
        return;
      }
      const defaultPrompt = promptForPreset(props.bootstrap, draft.domain_preset, draft.target_lang);
      if (!value.trim() || value.trim() === defaultPrompt.trim()) {
        delete draft.domain_prompt_overrides[draft.domain_preset];
      } else {
        draft.domain_prompt_overrides[draft.domain_preset] = value;
      }
    });
  }

  return (
    <div className="settings-grid">
      <section className="panel">
        <div className="section-heading compact">
          <h3>引擎</h3>
          <Cloud size={20} />
        </div>
        <div className="field-grid">
          <label>
            引擎类型
            <select value={engine.mode} onChange={(event) => props.updateSettings((draft) => { draft.engine.mode = event.target.value as "cloud" | "local"; })}>
              <option value="cloud">云端 API</option>
              <option value="local">本地 Ollama</option>
            </select>
          </label>
          {engine.mode === "cloud" ? (
            <>
              <label>
                服务商
                <select value={engine.cloud_provider} onChange={(event) => props.updateSettings((draft) => { draft.engine.cloud_provider = event.target.value; })}>
                  {Object.entries(props.bootstrap.engine.cloudEngines).map(([label, value]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
              {isHermes ? (
                <p className="inline-note">Hermes 会读取本机 ~/.hermes 配置，不需要在这里重复填写模型、Base URL 或 API Key。</p>
              ) : (
                <>
                  <label>
                    模型名称
                    <input value={engine.cloud_model} onChange={(event) => props.updateSettings((draft) => { draft.engine.cloud_model = event.target.value; })} />
                  </label>
                  <label>
                    Base URL
                    <input value={engine.cloud_base_url} onChange={(event) => props.updateSettings((draft) => { draft.engine.cloud_base_url = event.target.value; })} />
                  </label>
                  <label>
                    API Key
                    <input type="password" value={props.apiKeyDraft} placeholder="留空不会读取已有密钥" onChange={(event) => props.setApiKeyDraft(event.target.value)} />
                  </label>
                </>
              )}
            </>
          ) : (
            <label>
              Ollama 模型
              <input list="ollama-model-options" value={engine.ollama_model} onChange={(event) => props.updateSettings((draft) => { draft.engine.ollama_model = event.target.value; })} />
              <datalist id="ollama-model-options">
                {props.bootstrap.engine.ollamaRecommendedModels.map((model) => <option key={model} value={model} />)}
              </datalist>
            </label>
          )}
        </div>
        <div className="number-grid">
          <NumberField label="批次大小" min={batchRange[0]} max={batchRange[1]} value={engine.batch_size}
            onChange={(value) => props.updateSettings((draft) => { draft.engine.batch_size = value; })} />
          <NumberField label="并发数" min={concMin} max={concMax} value={concurrencyValue}
            onChange={updateConcurrency} />
        </div>
        <div className="action-row">
          <button className="secondary" onClick={props.onConnectivityCheck}>测试连接</button>
          {engine.mode === "cloud" && !isHermes && <button className="secondary" onClick={props.onSaveApiKey}>保存 Key</button>}
          <button className="primary" onClick={props.onSave}>保存设置</button>
        </div>
      </section>

      <section className="panel">
        <div className="section-heading compact">
          <h3>语言与输出</h3>
          <Languages size={20} />
        </div>
        <div className="field-grid">
          <label>
            源语言
            <select value={props.settings.source_lang} onChange={(event) => props.updateSettings((draft) => { draft.source_lang = event.target.value; })}>
              {Object.entries(props.bootstrap.languages.source).map(([label, value]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            目标语言
            <select value={props.settings.target_lang} onChange={(event) => props.updateSettings((draft) => { draft.target_lang = event.target.value; })}>
              {Object.entries(props.bootstrap.languages.target).map(([label, value]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <Checkbox label="保留原始工作表" checked={props.settings.output.keep_original_sheets}
            onChange={(value) => props.updateSettings((draft) => { draft.output.keep_original_sheets = value; })} />
          <Checkbox label="公式空值回填显示值" checked={props.settings.output.formula_display_value_backfill}
            onChange={(value) => props.updateSettings((draft) => { draft.output.formula_display_value_backfill = value; })} />
          <Checkbox label="Excel 自动列宽" checked={props.settings.output.enable_excel_autofit}
            onChange={(value) => props.updateSettings((draft) => { draft.output.enable_excel_autofit = value; })} />
          <Checkbox label="锁定行高" checked={props.settings.output.lock_row_height}
            onChange={(value) => props.updateSettings((draft) => { draft.output.lock_row_height = value; })} />
          <Checkbox label="启用任务日志" checked={props.settings.output.enable_task_log}
            onChange={(value) => props.updateSettings((draft) => { draft.output.enable_task_log = value; })} />
          <Checkbox label="使用自定义输出目录" checked={props.settings.output.use_custom_output_dir}
            onChange={(value) => props.updateSettings((draft) => { draft.output.use_custom_output_dir = value; })} />
          {props.settings.output.use_custom_output_dir && (
            <div className="path-row two-actions">
              <input value={props.settings.output.custom_output_dir} onChange={(event) => props.updateSettings((draft) => { draft.output.custom_output_dir = event.target.value; })} />
              <button className="icon-button" onClick={chooseOutputDir} title="选择输出目录"><FolderOpen size={18} /></button>
            </div>
          )}
        </div>
        <div className="action-row">
          <button className="secondary" onClick={props.onUpdateCheck}>检查更新</button>
          {props.updateInfo?.status === "available" && (
            <button className="primary" onClick={props.onDownloadUpdate}>
              <ExternalLink size={16} />
              下载 V{props.updateInfo.latest_version || "新版"}
            </button>
          )}
        </div>
        {props.updateInfo?.status === "available" && (
          <div className="update-card">
            <strong>{props.updateInfo.message}</strong>
            <span>{props.updateInfo.asset_name || "打开 GitHub Release 下载页"}</span>
          </div>
        )}
      </section>

      <section className="panel wide-panel">
        <div className="section-heading compact">
          <h3>专业领域与 Prompt</h3>
          <Sparkles size={20} />
        </div>
        <div className="field-grid">
          <label>
            领域预设
            <select value={props.settings.domain_preset} onChange={(event) => props.updateSettings((draft) => { draft.domain_preset = event.target.value; })}>
              {domainNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label>
            Prompt
            <textarea value={promptValue} rows={7} onChange={(event) => updateDomainPrompt(event.target.value)} />
          </label>
        </div>
      </section>
    </div>
  );
}

function TmPage(props: {
  result: TmSearchResult | null;
  keyword: string;
  setKeyword: (value: string) => void;
  onSearch: () => void;
  draft: { source: string; target: string };
  setDraft: (value: { source: string; target: string }) => void;
  onAdd: () => void;
}) {
  return (
    <div className="page-grid">
      <section className="panel">
        <div className="section-heading compact">
          <h3>记忆库搜索</h3>
          <Database size={20} />
        </div>
        <div className="path-row">
          <input value={props.keyword} onChange={(event) => props.setKeyword(event.target.value)} placeholder="搜索原文或译文" />
          <button className="primary" onClick={props.onSearch}>搜索</button>
        </div>
        <div className="result-list tm-list">
          {props.result?.rows.map((row) => (
            <div className="result-item" key={row.id}>
              <strong>{row.source_text}</strong>
              <span>{row.target_text}</span>
              <small>{row.word_type} · {row.pinned ? "已固定" : "未固定"}</small>
            </div>
          )) || <p className="empty">输入关键词或直接搜索当前语言对。</p>}
        </div>
      </section>
      <section className="panel">
        <div className="section-heading compact">
          <h3>手动新增</h3>
          <Sparkles size={20} />
        </div>
        <div className="field-grid">
          <label>原文<input value={props.draft.source} onChange={(event) => props.setDraft({ ...props.draft, source: event.target.value })} /></label>
          <label>译文<input value={props.draft.target} onChange={(event) => props.setDraft({ ...props.draft, target: event.target.value })} /></label>
        </div>
        <button className="primary" onClick={props.onAdd}>新增词条</button>
      </section>
    </div>
  );
}

function DiagnosticsPage(props: { logs: TaskLogEntry[]; bootstrap: BootstrapPayload; onRestart: () => void }) {
  return (
    <section className="panel diagnostics">
      <div className="section-heading compact">
        <h3>诊断与日志</h3>
        <Terminal size={20} />
      </div>
      <div className="metric-row">
        <Metric label="版本" value={`V${props.bootstrap.app.version}`} />
        <Metric label="当前语言对" value={props.bootstrap.tm.langPair} />
        <Metric label="TM 词条" value={String(props.bootstrap.tm.stats.total || 0)} />
      </div>
      <button className="secondary" onClick={props.onRestart}><RefreshCcw size={16} /> 重启 Worker</button>
      <div className="log-list">
        {props.logs.map((log) => (
          <div className="log-row" key={log.id}>
            <span>{log.ts}</span>
            <strong>{log.level}</strong>
            <p>{log.message}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function NumberField(props: { label: string; value: number; min: number; max: number; step?: number; onChange: (value: number) => void }) {
  return (
    <label className="number-field">
      <span>{props.label}</span>
      <small>{props.min} - {props.max}</small>
      <input type="number" min={props.min} max={props.max} step={props.step || 1} value={props.value}
        onChange={(event) => props.onChange(Number(event.target.value))} />
    </label>
  );
}

function Checkbox(props: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="check-row">
      <input type="checkbox" checked={props.checked} onChange={(event) => props.onChange(event.target.checked)} />
      {props.label}
    </label>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default App;
