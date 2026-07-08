# Runtime Configuration

Use lazy configuration. Do not ask the user for all credentials during install.
For each request, run the router in dry-run mode first and ask only for the
missing configuration reported by `missing_requirements`.

Default direct-mode values:

- Source language: `auto`; the router samples Excel/Word content when possible.
- Text concurrency: `10`.
- PDF image page concurrency: `3`.
- PDF review: disabled unless the user asks for it.
- Output root: let the underlying CLI create a timestamped output folder unless
  the user provides `--output-dir`.

Current-agent configuration means process-visible credentials, not hidden
platform secrets. Supported environment variables include:

- Text/OpenAI-compatible: `OPENAI_API_KEY`, `OPENAI_MODEL`,
  `OPENAI_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY`,
  `OPENAI_COMPATIBLE_MODEL`, `OPENAI_COMPATIBLE_BASE_URL`,
  `TRANSLATOR_API_KEY`, `TRANSLATOR_TEXT_MODEL`, `TRANSLATOR_BASE_URL`.
- PDF image: `OPENAI_API_KEY`, `TRANSLATOR_IMAGE_MODEL`,
  `OPENAI_IMAGE_MODEL`; default model is `gpt-image-2` when OpenAI credentials
  are available and no image model is configured.
- Claude text: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`.
- DashScope text: `DASHSCOPE_API_KEY`, `DASHSCOPE_MODEL`.
- Zhipu text: `ZHIPUAI_API_KEY`, `ZHIPU_MODEL`.
- SiliconFlow: `SILICONFLOW_API_KEY`, `SILICONFLOW_MODEL`,
  `SILICONFLOW_IMAGE_MODEL`, `SILICONFLOW_VISION_MODEL`.

When no usable key or model is available, ask the user to choose:

1. Use current agent API configuration by exposing the relevant environment vars.
2. Provide a temporary key/model for this run.
3. Save credentials in Translator local settings.
4. For Excel/Word only, switch to a configured local model.
