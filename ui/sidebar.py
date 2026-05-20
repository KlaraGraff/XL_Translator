"""
侧边栏：导航 + 专业领域 + 翻译引擎配置。
目标语言、输出选项已移至主页面；TM阈值已移至记忆库管理页。
"""
import webbrowser

import streamlit as st

from app_meta import APP_NAME, APP_VERSION_LABEL
from config import (
    CLOUD_ENGINES, OLLAMA_RECOMMENDED_MODELS, DOMAIN_PRESETS, LANYI_BASE_URL,
    CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX, CHUNK_CLOUD_DEFAULT,
    CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX, CHUNK_LOCAL_DEFAULT,
    get_concurrency_bounds,
    get_default_concurrency,
    is_valid_concurrency_unlock_code,
)
from core.api_health import build_connectivity_signature
from core.connectivity_check import ConnectivityResult, check_connectivity
from core.model_catalog import (
    ModelCatalogResult,
    build_model_catalog_signature,
    fetch_openai_compatible_models,
)
from core.update_checker import UpdateCheckResult, check_for_updates
from settings import AppSettings, get_key, save_key
from ui.branding import build_sidebar_brand_label_html
from ui.components import build_sidebar_tooltip_html, render_field_group


_BRAND_TOOLTIP = {
    "title": APP_NAME,
    "title_meta": "by OA",
    "summary": f"{APP_NAME} 是一个面向 Excel 和 Word 文档的本地翻译器，左侧完成配置，右侧执行任务并维护统一记忆库。",
    "items": [
        "表格翻译页用于扫描 Excel 文件、执行翻译和查看结果。",
        "Word 翻译页用于扫描 DOCX 文件并生成双语 Word。",
        "记忆库管理页用于搜索、新增、固定和清理共享词条。",
    ],
}

_DOMAIN_TOOLTIP = {
    "title": "专业领域",
    "summary": "先选最接近当前资料的领域预设，再决定是否细调 Prompt。",
    "items": [
        "预设会带入该领域常用术语、语气和翻译侧重。",
        "常规任务优先直接使用预设，只有特殊要求时再改 Prompt。",
        "同一批文件尽量保持同一领域，结果通常更稳定。",
    ],
}

_PROMPT_TOOLTIP = {
    "title": "Prompt",
    "summary": "这是本次翻译的工作指令，会直接影响术语、语气和约束。",
    "items": [
        "跟随领域预设时，可以在默认内容上小幅微调。",
        "清空修改内容会恢复为当前预设的默认值。",
        "选择“自定义”后，这里的内容会完整作为本次任务 Prompt。",
        "建议只保留必要规则，避免重复和过长。",
    ],
}

_ENGINE_TOOLTIP = {
    "title": "翻译引擎",
    "summary": "选择本次任务使用云端 API 还是本地 Ollama，在质量、灵活性与文件隐私之间做取舍。",
    "items": [
        "云端 API 适合模型选择更多、通用质量更高、接入更灵活的场景。",
        "本地 Ollama 更适合处理隐私敏感文件，因为翻译内容不会上传到云端。",
        "切换引擎后，下方可配置项和吞吐范围会同步变化。",
    ],
}

_CLOUD_SETTINGS_TOOLTIP = {
    "title": "云端 API 设置",
    "summary": "这组配置决定请求会发送到哪个云端服务，以及由哪个模型完成翻译。",
    "items": [
        "服务商用于切换当前接入渠道。",
        "API Key 用于身份认证。",
        "Base URL 主要用于兼容接口或自定义网关。",
        "模型名称决定本次实际调用的云端模型。",
    ],
}

_OLLAMA_TOOLTIP = {
    "title": "Ollama 模型",
    "summary": "本地模型运行在当前设备上，适合对数据不出本机有要求的翻译任务。",
    "items": [
        "推荐列表适合快速选择常用模型，也可以手动填写本机已安装的其他模型名。",
        "模型越大，通常效果更好，但也会占用更多本机资源。",
        "只要使用本地引擎，翻译内容就不会发送到外部云端服务。",
    ],
}

_TUNING_TOOLTIP = {
    "title": "吞吐调优",
    "summary": "批次大小和并发数一起决定速度、稳定性和资源占用。",
    "items": [
        "批次越大通常越快，但更容易超时或带来上下文压力。",
        "并发越高整体吞吐越高，但也更容易限流或占满本机资源。",
        "遇到超时、失败重试或机器负载偏高时，优先把这两项调低。",
    ],
}

_UPDATE_CHECK_RESULT_KEY = "sidebar_update_check_result"
_CONNECTIVITY_RESULT_KEY = "sidebar_connectivity_result"
_MODEL_CATALOG_RESULT_KEY = "sidebar_model_catalog_result"
_MODEL_CATALOG_SELECT_KEY = "sidebar_model_catalog_select"
_MODEL_CATALOG_PLACEHOLDER = "选择模型..."
_MODEL_INPUT_KEY = "sidebar_cloud_model_input"
_MODEL_INPUT_PROVIDER_KEY = "sidebar_cloud_model_provider"
_MODEL_CATALOG_PROVIDERS = {"openai", "custom_openai", "lanyi", "siliconflow"}


def _build_sidebar_section_title_html(
    label: str,
    title: str,
    summary: str,
    items: list[str],
) -> str:
    """Render a sidebar section title that opens the shared sidebar tooltip card."""
    title_tooltip = build_sidebar_tooltip_html(
        label,
        title,
        summary,
        items,
        label_class="sidebar-shell-title sidebar-tooltip__label--section",
        trigger_class="sidebar-tooltip--section",
        card_class="sidebar-tooltip-card--section",
    )
    return f'<div class="sidebar-shell-title-row">{title_tooltip}</div>'


def _build_sidebar_field_tooltip_html(
    label: str,
    title: str,
    summary: str,
    items: list[str],
) -> str:
    """Render a sidebar field label that reuses the shared sidebar tooltip card."""
    return build_sidebar_tooltip_html(
        label,
        title,
        summary,
        items,
        label_class="sidebar-tooltip__label--field",
        trigger_class="sidebar-tooltip--field",
        card_class="sidebar-tooltip-card--field",
    )


def _normalize_concurrency_widget_value(
    raw_value: str,
    *,
    current_value: int,
    mode: str,
    unlocked: bool,
) -> tuple[int, bool, str]:
    if is_valid_concurrency_unlock_code(raw_value):
        unlocked = True
        conc_min, conc_max = get_concurrency_bounds(mode, unlocked)
        current_value = max(conc_min, min(conc_max, current_value))
        return current_value, unlocked, str(current_value)

    conc_min, conc_max = get_concurrency_bounds(mode, unlocked)
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        current_value = max(conc_min, min(conc_max, current_value))
        return current_value, unlocked, str(current_value)

    current_value = max(conc_min, min(conc_max, parsed_value))
    return current_value, unlocked, str(current_value)


def _render_update_check(is_running: bool) -> None:
    if st.button(
        "检查更新",
        key="sidebar_check_update",
        use_container_width=True,
        disabled=is_running,
    ):
        with st.spinner("正在检查更新..."):
            st.session_state[_UPDATE_CHECK_RESULT_KEY] = check_for_updates()

    result = st.session_state.get(_UPDATE_CHECK_RESULT_KEY)
    if not isinstance(result, UpdateCheckResult):
        return

    if result.has_update:
        st.success(result.message)
        if result.asset_name:
            st.caption(f"下载项：{result.asset_name}")
        download_url = result.download_url or result.release_url
        if st.button(
            "下载新版",
            key="sidebar_download_update",
            use_container_width=True,
            disabled=is_running or not download_url,
        ):
            webbrowser.open(download_url)
    elif result.ok:
        st.caption(result.message)
    else:
        st.warning(result.message)


def _render_connectivity_result(entry: dict) -> None:
    result = entry.get("result")
    if not isinstance(result, ConnectivityResult):
        return

    if result.ok:
        st.success(result.message)
    else:
        st.warning(result.message)
        if result.detail:
            st.caption(result.detail)


def _render_connectivity_check(settings: AppSettings, is_running: bool) -> None:
    signature = build_connectivity_signature(settings)
    if st.button(
        "测试连接",
        key="sidebar_test_connectivity",
        use_container_width=True,
        disabled=is_running,
    ):
        with st.spinner("正在测试连接..."):
            st.session_state[_CONNECTIVITY_RESULT_KEY] = {
                "signature": signature,
                "result": check_connectivity(settings),
            }

    entry = st.session_state.get(_CONNECTIVITY_RESULT_KEY)
    if not isinstance(entry, dict):
        return

    if entry.get("signature") != signature:
        st.caption("配置已变化，请重新测试连接。")
        return

    _render_connectivity_result(entry)


def _apply_model_catalog_selection() -> None:
    selected_model = st.session_state.get(_MODEL_CATALOG_SELECT_KEY)
    if selected_model and selected_model != _MODEL_CATALOG_PLACEHOLDER:
        st.session_state[_MODEL_INPUT_KEY] = selected_model


def _render_model_catalog_result(entry: dict, settings: AppSettings, is_running: bool) -> None:
    result = entry.get("result")
    if not isinstance(result, ModelCatalogResult):
        return

    current_model = str(settings.engine.cloud_model or "").strip()
    if result.ok:
        st.caption(result.message)
        options = [_MODEL_CATALOG_PLACEHOLDER] + result.models
        if current_model and current_model in result.models:
            st.session_state[_MODEL_CATALOG_SELECT_KEY] = current_model
        elif not current_model or current_model not in result.models:
            st.session_state[_MODEL_CATALOG_SELECT_KEY] = _MODEL_CATALOG_PLACEHOLDER

        st.selectbox(
            "可用模型",
            options,
            key=_MODEL_CATALOG_SELECT_KEY,
            label_visibility="collapsed",
            disabled=is_running,
            on_change=_apply_model_catalog_selection,
        )
        settings.engine.cloud_model = st.session_state.get(_MODEL_INPUT_KEY, current_model)
        return

    st.warning(result.message)
    if result.detail:
        st.caption(result.detail)


def _render_model_catalog_picker(
    settings: AppSettings,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    is_running: bool,
) -> None:
    if provider not in _MODEL_CATALOG_PROVIDERS:
        return

    signature = build_model_catalog_signature(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
    )
    needs_base_url = provider in {"custom_openai", "lanyi", "siliconflow"}
    fetch_disabled = is_running or not api_key or (needs_base_url and not str(base_url or "").strip())
    if st.button(
        "获取模型列表",
        key="sidebar_fetch_model_catalog",
        use_container_width=True,
        disabled=fetch_disabled,
    ):
        with st.spinner("正在获取模型列表..."):
            st.session_state[_MODEL_CATALOG_RESULT_KEY] = {
                "signature": signature,
                "result": fetch_openai_compatible_models(
                    provider=provider,
                    api_key=api_key,
                    base_url=base_url,
                ),
            }

    entry = st.session_state.get(_MODEL_CATALOG_RESULT_KEY)
    if not isinstance(entry, dict):
        if fetch_disabled and not is_running:
            hint = "填写 API Key 和 Base URL 后，可获取模型列表。" if needs_base_url else "填写 API Key 后，可获取模型列表。"
            st.caption(hint)
        return

    if entry.get("signature") != signature:
        st.caption("API Key 或 Base URL 已变化，请重新获取模型列表。")
        return

    _render_model_catalog_result(entry, settings, is_running)


def render_sidebar(settings: AppSettings, active_page: str, is_running: bool) -> tuple[AppSettings, str]:
    """
    渲染侧边栏。
    返回 (更新后的 settings, 新的 active_page)。
    """
    with st.sidebar:
        # ── 应用标题 ──────────────────────────────────────
        brand_tooltip_html = build_sidebar_tooltip_html(
            APP_NAME,
            _BRAND_TOOLTIP["title"],
            _BRAND_TOOLTIP["summary"],
            _BRAND_TOOLTIP["items"],
            title_meta=_BRAND_TOOLTIP["title_meta"],
            label_class="sidebar-title sidebar-tooltip__label--brand",
            trigger_class="sidebar-tooltip--brand",
            card_class="sidebar-tooltip-card--brand",
            label_html=build_sidebar_brand_label_html(APP_NAME),
        )
        top_shell = st.container(key="sidebar-top-shell")
        with top_shell:
            st.markdown(
                '<div class="sidebar-brand">'
                '  <div class="sidebar-brand__title-row">'
                f'{brand_tooltip_html}'
                f'    <div class="sidebar-version">{APP_VERSION_LABEL}</div>'
                '  </div>'
                '</div>',
                unsafe_allow_html=True,
            )
            _render_update_check(is_running)
            st.markdown('<div class="nav-section">', unsafe_allow_html=True)
            btn_excel = st.button(
                "表格翻译",
                key="nav_excel_translate",
                use_container_width=True,
                type="primary" if active_page in ("excel_translate", "translate") else "secondary",
                disabled=is_running,
            )
            btn_word = st.button(
                "Word 翻译",
                key="nav_word_translate",
                use_container_width=True,
                type="primary" if active_page == "word_translate" else "secondary",
                disabled=is_running,
            )
            btn_tm = st.button(
                "记忆库管理",
                key="nav_tm",
                use_container_width=True,
                type="primary" if active_page == "tm" else "secondary",
                disabled=is_running,
            )
            st.markdown('</div>', unsafe_allow_html=True)

        new_page = active_page
        if btn_excel:
            new_page = "excel_translate"
        elif btn_word:
            new_page = "word_translate"
        elif btn_tm:
            new_page = "tm"

        # ── 专业领域 ──────────────────────────────────────
        domain_shell = st.container(key="sidebar-domain-shell")
        with domain_shell:
            st.markdown(
                _build_sidebar_section_title_html(
                    "专业领域",
                    _DOMAIN_TOOLTIP["title"],
                    _DOMAIN_TOOLTIP["summary"],
                    _DOMAIN_TOOLTIP["items"],
                ),
                unsafe_allow_html=True,
            )
            preset_keys = list(DOMAIN_PRESETS.keys())

            current_idx = preset_keys.index(settings.domain_preset) \
                          if settings.domain_preset in preset_keys else 0

            preset_field = render_field_group(
                domain_shell,
                key="sidebar-domain-preset",
                label="领域预设",
            )
            with preset_field:
                selected_key = st.selectbox(
                    "领域预设",
                    options=preset_keys,
                    index=current_idx,
                    label_visibility="collapsed",
                    disabled=is_running,
                )
            settings.domain_preset = selected_key

            # 可编辑 Prompt
            if selected_key == "自定义":
                prompt_field = render_field_group(
                    domain_shell,
                    key="sidebar-domain-custom-prompt",
                    label_html=_build_sidebar_field_tooltip_html(
                        "Prompt",
                        _PROMPT_TOOLTIP["title"],
                        _PROMPT_TOOLTIP["summary"],
                        _PROMPT_TOOLTIP["items"],
                    ),
                )
                with prompt_field:
                    settings.custom_prompt = st.text_area(
                        "Prompt",
                        value=settings.custom_prompt,
                        height=96,
                        placeholder="输入专属 System Prompt...",
                        label_visibility="collapsed",
                        disabled=is_running,
                    )
            else:
                _preset_val = DOMAIN_PRESETS.get(selected_key, "")
                default_prompt = _preset_val.get("_base", "") if isinstance(_preset_val, dict) else _preset_val
                current_prompt = settings.domain_prompt_overrides.get(selected_key, default_prompt)

                prompt_field = render_field_group(
                    domain_shell,
                    key="sidebar-domain-prompt",
                    label_html=_build_sidebar_field_tooltip_html(
                        "Prompt",
                        _PROMPT_TOOLTIP["title"],
                        _PROMPT_TOOLTIP["summary"],
                        _PROMPT_TOOLTIP["items"],
                    ),
                )
                with prompt_field:
                    new_prompt = st.text_area(
                        "Prompt",
                        value=current_prompt,
                        height=96,
                        label_visibility="collapsed",
                        disabled=is_running,
                    )
                if new_prompt != current_prompt:
                    if new_prompt.strip() == default_prompt.strip() or not new_prompt.strip():
                        settings.domain_prompt_overrides.pop(selected_key, None)
                    else:
                        settings.domain_prompt_overrides[selected_key] = new_prompt

        # ── 翻译引擎 ──────────────────────────────────────
        engine_shell = st.container(key="sidebar-engine-shell")
        with engine_shell:
            st.markdown(
                _build_sidebar_section_title_html(
                    "翻译引擎",
                    _ENGINE_TOOLTIP["title"],
                    _ENGINE_TOOLTIP["summary"],
                    _ENGINE_TOOLTIP["items"],
                ),
                unsafe_allow_html=True,
            )

            _mode_key = "sb_engine_mode"
            if _mode_key not in st.session_state:
                st.session_state[_mode_key] = "云端 API" if settings.engine.mode == "cloud" else "本地 Ollama"
            mode_field = render_field_group(
                engine_shell,
                key="sidebar-engine-mode",
                label="引擎类型",
            )
            with mode_field:
                mode = st.radio(
                    "引擎类型",
                    options=["云端 API", "本地 Ollama"],
                    key=_mode_key,
                    horizontal=True,
                    label_visibility="collapsed",
                    disabled=is_running,
                )
            settings.engine.mode = "cloud" if mode == "云端 API" else "local"

            if settings.engine.mode == "cloud":
                provider_labels = list(CLOUD_ENGINES.keys())
                provider_keys   = list(CLOUD_ENGINES.values())
                effective_provider = (
                    "custom_openai"
                    if settings.engine.cloud_provider == "lanyi"
                    else settings.engine.cloud_provider
                )
                current_idx = provider_keys.index(effective_provider) \
                              if effective_provider in provider_keys else 0

                cloud_settings_field = render_field_group(
                    engine_shell,
                    key="sidebar-cloud-settings",
                    label_html=_build_sidebar_field_tooltip_html(
                        "云端 API 设置",
                        _CLOUD_SETTINGS_TOOLTIP["title"],
                        _CLOUD_SETTINGS_TOOLTIP["summary"],
                        _CLOUD_SETTINGS_TOOLTIP["items"],
                    ),
                )
                with cloud_settings_field:
                    provider_row = st.container(key="sidebar-cloud-subfield-provider")
                    with provider_row:
                        st.markdown(
                            '<div class="sidebar-inline-setting-label">服务商</div>',
                            unsafe_allow_html=True,
                        )
                        selected_label = st.selectbox(
                            "服务商",
                            provider_labels,
                            index=current_idx,
                            label_visibility="collapsed",
                            disabled=is_running,
                        )
                        selected_provider = CLOUD_ENGINES[selected_label]
                        previous_provider = settings.engine.cloud_provider
                        settings.engine.cloud_provider = selected_provider
                        if selected_provider == "openai":
                            settings.engine.cloud_base_url = ""

                    if selected_provider == "hermes":
                        st.caption(
                            "Hermes 内置引擎：自动跟随 ~/.hermes/config.yaml 的主模型配置，"
                            "无需在这里重复填写 API Key、Base URL 或模型名称。"
                        )
                    else:
                        current_key_provider = (
                            "lanyi"
                            if previous_provider == "lanyi" and selected_provider == "custom_openai"
                            else selected_provider
                        )
                        current_key = get_key(current_key_provider)

                        api_key_row = st.container(key="sidebar-cloud-subfield-api-key")
                        with api_key_row:
                            st.markdown(
                                '<div class="sidebar-inline-setting-label">API Key</div>',
                                unsafe_allow_html=True,
                            )
                            new_key = st.text_input(
                                "API Key",
                                value=current_key,
                                type="password",
                                placeholder="sk-...",
                                label_visibility="collapsed",
                                disabled=is_running,
                            )
                        if new_key != current_key:
                            save_key(current_key_provider, new_key)
                            if current_key_provider != selected_provider:
                                save_key(selected_provider, new_key)
                        effective_api_key = new_key

                        if current_key_provider == "lanyi" and not settings.engine.cloud_base_url:
                            settings.engine.cloud_base_url = LANYI_BASE_URL
                        if selected_provider == "openai":
                            settings.engine.cloud_base_url = ""

                        if settings.engine.cloud_provider in ("siliconflow", "custom_openai"):
                            base_url_row = st.container(key="sidebar-cloud-subfield-base-url")
                            with base_url_row:
                                st.markdown(
                                    '<div class="sidebar-inline-setting-label">Base URL</div>',
                                    unsafe_allow_html=True,
                                )
                                settings.engine.cloud_base_url = st.text_input(
                                    "Base URL",
                                    value=settings.engine.cloud_base_url,
                                    placeholder="https://api.example.com/v1",
                                    label_visibility="collapsed",
                                    disabled=is_running,
                                )

                        model_row = st.container(key="sidebar-cloud-subfield-model")
                        with model_row:
                            st.markdown(
                                '<div class="sidebar-inline-setting-label">模型名称</div>',
                                unsafe_allow_html=True,
                            )
                            if (
                                _MODEL_INPUT_KEY not in st.session_state
                                or st.session_state.get(_MODEL_INPUT_PROVIDER_KEY) != selected_provider
                            ):
                                st.session_state[_MODEL_INPUT_KEY] = settings.engine.cloud_model
                                st.session_state[_MODEL_INPUT_PROVIDER_KEY] = selected_provider
                            settings.engine.cloud_model = st.text_input(
                                "模型名称",
                                key=_MODEL_INPUT_KEY,
                                placeholder="claude-sonnet-4-6",
                                label_visibility="collapsed",
                                disabled=is_running,
                            )
                            settings.engine.cloud_model = st.session_state.get(_MODEL_INPUT_KEY, settings.engine.cloud_model)
                            _render_model_catalog_picker(
                                settings,
                                provider=selected_provider,
                                api_key=effective_api_key,
                                base_url=settings.engine.cloud_base_url,
                                is_running=is_running,
                            )
                    _render_connectivity_check(settings, is_running)
            else:
                model_options = OLLAMA_RECOMMENDED_MODELS + ["自定义..."]
                if settings.engine.ollama_model in OLLAMA_RECOMMENDED_MODELS:
                    model_idx = OLLAMA_RECOMMENDED_MODELS.index(settings.engine.ollama_model)
                else:
                    model_idx = len(model_options) - 1

                ollama_model_field = render_field_group(
                    engine_shell,
                    key="sidebar-ollama-model",
                    label_html=_build_sidebar_field_tooltip_html(
                        "Ollama 模型",
                        _OLLAMA_TOOLTIP["title"],
                        _OLLAMA_TOOLTIP["summary"],
                        _OLLAMA_TOOLTIP["items"],
                    ),
                )
                with ollama_model_field:
                    selected_model = st.selectbox(
                        "Ollama 模型",
                        model_options,
                        index=model_idx,
                        disabled=is_running,
                    )
                if selected_model == "自定义...":
                    with ollama_model_field:
                        settings.engine.ollama_model = st.text_input(
                            "模型名称",
                            value=settings.engine.ollama_model
                                  if settings.engine.ollama_model not in OLLAMA_RECOMMENDED_MODELS
                                  else "",
                            placeholder="qwen2.5:14b",
                            disabled=is_running,
                        )
                else:
                    settings.engine.ollama_model = selected_model
                _render_connectivity_check(settings, is_running)

        # ── 批次大小 / 并发数：按模式动态适配区间 ──
        _batch_key = "sb_batch_size_input"
        _conc_value_key = "sb_concurrency_value"
        _conc_input_key = "sb_concurrency_input"
        _prev_mode_key = "sb_prev_engine_mode"

        if settings.engine.mode == "cloud":
            bs_min, bs_max, bs_default = CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX, CHUNK_CLOUD_DEFAULT
        else:
            bs_min, bs_max, bs_default = CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX, CHUNK_LOCAL_DEFAULT
        conc_min, conc_max = get_concurrency_bounds(
            settings.engine.mode,
            settings.engine.concurrency_unlocked,
        )
        mode_persisted_concurrency = (
            settings.engine.concurrency
            if settings.engine.mode == "cloud"
            else settings.engine.ollama_concurrency
        )

        prev_mode = st.session_state.get(_prev_mode_key)
        mode_changed = prev_mode is not None and prev_mode != settings.engine.mode

        # 首次初始化：将持久化值钳位到合法区间
        if _batch_key not in st.session_state:
            st.session_state[_batch_key] = max(bs_min, min(bs_max, settings.engine.batch_size))
        if _conc_value_key not in st.session_state:
            st.session_state[_conc_value_key] = max(
                conc_min,
                min(conc_max, mode_persisted_concurrency),
            )
        if _conc_input_key not in st.session_state:
            st.session_state[_conc_input_key] = str(st.session_state[_conc_value_key])

        # 检测模式切换 → 批次大小恢复新模式默认值；并发数仅做钳位
        if mode_changed:
            if _prev_mode_key in st.session_state:
                st.session_state[_batch_key] = bs_default
                switched_mode_value = (
                    settings.engine.concurrency
                    if settings.engine.mode == "cloud"
                    else settings.engine.ollama_concurrency
                )
                if not switched_mode_value:
                    switched_mode_value = get_default_concurrency(settings.engine.mode)
                st.session_state[_conc_value_key] = max(
                    conc_min,
                    min(conc_max, switched_mode_value),
                )
                st.session_state[_conc_input_key] = str(st.session_state[_conc_value_key])

        # 钳位：防止存量配置超出当前模式合法区间
        st.session_state[_batch_key] = max(bs_min, min(bs_max, st.session_state[_batch_key]))
        st.session_state[_conc_value_key] = max(
            conc_min,
            min(conc_max, st.session_state[_conc_value_key]),
        )
        st.session_state[_prev_mode_key] = settings.engine.mode

        tuning_shell = st.container(key="sidebar-tuning-shell")
        with tuning_shell:
            st.markdown(
                _build_sidebar_section_title_html(
                    "吞吐调优",
                    _TUNING_TOOLTIP["title"],
                    _TUNING_TOOLTIP["summary"],
                    _TUNING_TOOLTIP["items"],
                ),
                unsafe_allow_html=True,
            )

            current_concurrency = int(st.session_state[_conc_value_key])
            current_concurrency, settings.engine.concurrency_unlocked, normalized_concurrency_text = (
                _normalize_concurrency_widget_value(
                    st.session_state.get(_conc_input_key, str(current_concurrency)),
                    current_value=current_concurrency,
                    mode=settings.engine.mode,
                    unlocked=settings.engine.concurrency_unlocked,
                )
            )
            conc_min, conc_max = get_concurrency_bounds(
                settings.engine.mode,
                settings.engine.concurrency_unlocked,
            )
            st.session_state[_conc_value_key] = max(conc_min, min(conc_max, current_concurrency))
            st.session_state[_conc_input_key] = normalized_concurrency_text

            input_col1, input_col2 = st.columns(2, gap="small")
            with input_col1:
                batch_field = render_field_group(
                    input_col1,
                    key="sidebar-batch-size",
                    label="批次大小",
                    hint=f"{bs_min} - {bs_max}",
                )
                with batch_field:
                    st.number_input(
                        "批次大小输入",
                        min_value=bs_min,
                        max_value=bs_max,
                        step=1,
                        format="%d",
                        disabled=is_running,
                        label_visibility="collapsed",
                        key=_batch_key,
                    )
            with input_col2:
                concurrency_field = render_field_group(
                    input_col2,
                    key="sidebar-concurrency",
                    label="并发数",
                    hint=f"{conc_min} - {conc_max}",
                )
                with concurrency_field:
                    st.text_input(
                        "并发数输入",
                        disabled=is_running,
                        label_visibility="collapsed",
                        key=_conc_input_key,
                    )

        # 同步写回 settings，确保持久化值始终在合法区间内
        settings.engine.batch_size = int(st.session_state[_batch_key])
        current_concurrency_value = int(st.session_state[_conc_value_key])
        if settings.engine.mode == "cloud":
            settings.engine.concurrency = current_concurrency_value
        else:
            settings.engine.ollama_concurrency = current_concurrency_value

    return settings, new_page
