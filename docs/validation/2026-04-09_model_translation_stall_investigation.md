# Model Translation Stall Investigation

最后更新：2026-04-09

## 现象

- 用户反馈翻译任务卡在“调用大模型翻译”阶段，长时间等不到结果。

## 当前有效配置

- 配置文件：`~/.xl_translator/settings.json`
- 当前模式：`local`
- 当前本地模型：`qwen3.5:35b-a3b`
- 当前本地并发：`5`
- 当前批大小：`10`
- 当前 Ollama 会话参数：
  - `OLLAMA_CONTEXT_LENGTH=32768`
  - `OLLAMA_NUM_PARALLEL=5`
  - `OLLAMA_MAX_LOADED_MODELS=2`

## 关键日志时间线

### 应用侧日志

来源：`~/.xl_translator/app.log`

- `08:17:44` 阶段 2 开始，待 API 词条 `113`
- `08:23:50` 任务中止，未写入剩余翻译结果

来源：`.runtime/launcher.log`

- `08:17:44` 发送 API 请求，共 `113` 词条
- `08:19:44` Ollama 第 1 次重试
- `08:21:45` Ollama 第 2 次重试
- `08:23:47` Ollama 第 3 次重试
- `08:23:49` Ollama 重试耗尽，降级返回原文

### Ollama 侧日志

来源：`~/.ollama/logs/server.log`

- `08:17:44` 模型开始装载
- `08:17:50` runner 可用
- `08:19:16` 有请求在 `1m31s` 后返回 `200`
- `08:19:44` 同一轮另外多条 `/api/chat` 在 `2m0s` 超时后被客户端中断，返回 `500`
- `08:21:45` 第二轮再次出现 `2m0s` 超时
- `08:23:47` 第三轮再次出现 `~2m0s` 超时

## 结论

当前“卡住”不是前端没动，而是本地 Ollama 请求在持续排队和超时重试。

根因由三项叠加导致：

1. 应用在本地模式下，把一个批次再拆成多个并发子请求。
2. 当前配置把本地并发设为 `5`，批大小设为 `10`。
3. Ollama runner 日志显示本次实际服务能力是 `Parallel:1`，即一次只能有效处理 1 条生成请求。

因此当前批次会出现这种行为：

- 每 10 个词条作为一个大批次进入本地翻译阶段。
- `engines/ollama_engine.py` 会把这 10 个词条再拆成最多 5 个并发子请求。
- 但 Ollama 实际只能串行处理 1 条。
- 第一个请求大约 `1m31s` 才完成。
- 后面排队中的请求会把等待时间也算进客户端 `120s` 超时。
- 到 `120s` 后客户端主动断开，于是 Ollama 侧记录 `aborting completion request due to client closing the connection`。
- 这个过程重复 3 次，所以单轮看起来会卡住大约 6 分钟。

## 代码对应点

- 本地模式串行批次入口：`core/engine_dispatcher.py`
- 本地批次内再次并发拆分：`engines/ollama_engine.py`
- Ollama 单请求超时：`config.py` 中 `OLLAMA_TIMEOUT = 120`
- Ollama 重试次数：`config.py` 中 `RETRY_MAX_ATTEMPTS = 3`
- 已修复一个本地并发字段错位问题：
  - `build_engine()` 在本地模式下原先错误读取 `settings.engine.concurrency`
  - 现已改为读取 `settings.engine.ollama_concurrency`
  - 这样 UI 里的本地并发设置才会真正传给 `OllamaEngine`

## 补充风险

- 当前配置文件里虽然保留了云端 `custom_openai` 配置，但本次卡住的任务实际走的是 `local` 模式，不是云端模式。
- 历史日志中另有 `api.asxs.top` 的 `401 invalid_api_key` 与 `502 Bad gateway` 记录，这说明如果切回云端模式，也存在另一条独立的不稳定链路。

## 下一步建议

优先级从高到低：

1. 将本地并发从 `5` 降到 `1` 或 `2`，先验证是否不再出现 `120s` 排队超时。
2. 将本地模型从 `qwen3.5:35b-a3b` 切到更轻的模型，例如已安装的 `qwen3.5:27b` 或 `gemma4:26b` 做对照测试。
3. 如继续保留大模型，需同步调整：
   - 单请求超时
   - 本地并发策略
   - UI 阶段 2 的进度提示粒度
4. 若要继续使用云端模式，需要单独修复 `api.asxs.top` 的鉴权/网关稳定性问题。

## 当前落地动作

- 已把 Ollama 当前用户会话默认 context 调整为 `32768`
- 已把 Ollama 当前用户会话 `OLLAMA_NUM_PARALLEL` 调整为 `5`
- 已把 Ollama 当前用户会话 `OLLAMA_MAX_LOADED_MODELS` 调整为 `2`
- 已修复应用本地模式读取错误并发字段的问题，并补充针对性自测
