"""
公共 UI 组件：进度条、实时日志区、统计卡片。
"""
import html
import json

import streamlit as st


def _checkbox_row_container_key(key: str) -> str:
    """Build a stable Streamlit container key for checkbox rows."""
    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    return f"checkbox-row-{safe_key}"


def _setting_card_container_key(key: str) -> str:
    """Build a stable Streamlit container key for setting cards."""
    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    return f"setting-card-{safe_key}"


def _field_group_container_key(key: str) -> str:
    """Build a stable Streamlit container key for framed field groups."""
    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    return f"field-group-{safe_key}"


def _merge_css_class_names(*class_groups: str) -> str:
    """Merge CSS class tokens while keeping ordering stable and duplicates out."""
    tokens: list[str] = []
    for group in class_groups:
        if not group:
            continue
        for token in str(group).split():
            if token and token not in tokens:
                tokens.append(token)
    return html.escape(" ".join(tokens), quote=True)


def build_sidebar_tooltip_html(
    label: str,
    title: str,
    summary: str,
    items: list[str],
    *,
    label_class: str = "sidebar-tooltip__label",
    trigger_class: str = "",
    card_class: str = "",
    label_html: str | None = None,
    title_meta: str | None = None,
) -> str:
    """构造侧边栏专属 tooltip HTML。"""
    title_html = html.escape(title)
    title_meta_html = html.escape(title_meta.strip()) if title_meta and title_meta.strip() else ""
    summary_html = html.escape(summary)
    item_html = "".join(
        f"<li>{html.escape(item)}</li>" for item in items if item.strip()
    )
    trigger_cls = _merge_css_class_names("sidebar-tooltip", trigger_class)
    label_cls = _merge_css_class_names("sidebar-tooltip__label", label_class)
    card_cls = _merge_css_class_names("sidebar-tooltip-card", card_class)
    label_content = label_html if label_html is not None else html.escape(label)
    if title_meta_html:
        title_markup = (
            '<div class="sidebar-tooltip-card__title-inline">'
            f'<span class="sidebar-tooltip-card__title">{title_html}</span>'
            '<span class="sidebar-tooltip-card__title-separator" aria-hidden="true">｜</span>'
            f'<span class="sidebar-tooltip-card__title-meta">{title_meta_html}</span>'
            "</div>"
        )
    else:
        title_markup = f'<div class="sidebar-tooltip-card__title">{title_html}</div>'
    return (
        f'<div class="{trigger_cls}" tabindex="0">'
        f'<span class="{label_cls}">{label_content}</span>'
        f'<div class="{card_cls}" role="tooltip">'
        f"{title_markup}"
        f'<div class="sidebar-tooltip-card__summary">{summary_html}</div>'
        f'<ul class="sidebar-tooltip-card__list">{item_html}</ul>'
        '</div>'
        '</div>'
    )


def render_sidebar_tooltip(
    label: str,
    title: str,
    summary: str,
    items: list[str],
    *,
    trigger_class: str = "",
    card_class: str = "",
) -> None:
    """渲染侧边栏专属 tooltip 触发器与卡片。"""
    st.markdown(
        build_sidebar_tooltip_html(
            label,
            title,
            summary,
            items,
            trigger_class=trigger_class,
            card_class=card_class,
        ),
        unsafe_allow_html=True,
    )


def build_tooltip_label_html(
    label: str,
    title: str,
    summary: str,
    items: list[str],
    *,
    width: str = "330px",
    trigger_class: str = "",
    card_class: str = "",
) -> str:
    """Build the shared tooltip-trigger label HTML for main-area field labels."""
    title_html = html.escape(title)
    summary_html = html.escape(summary)
    item_html = "".join(
        f"<li>{html.escape(item)}</li>" for item in items if item.strip()
    )
    trigger_cls = f"ui-tooltip-label {trigger_class}".strip()
    text_cls = "ui-tooltip-text"
    card_cls = f"ui-tooltip-card {card_class}".strip()
    class_tokens = set(card_class.split())
    default_x = "left" if {"tooltip-align-right", "tooltip-main-right"} & class_tokens else "right"
    default_y = "up" if "tooltip-placement-up" in class_tokens else "down"
    width_attr = html.escape(width, quote=True)
    return (
        f'<div class="{trigger_cls}" '
        'data-tooltip-auto="1" '
        f'data-tooltip-default-x="{default_x}" '
        f'data-tooltip-default-y="{default_y}" '
        f'data-tooltip-x="{default_x}" '
        f'data-tooltip-y="{default_y}" '
        'tabindex="0">'
        f'<span class="{text_cls}">{html.escape(label)}</span>'
        f'<div class="{card_cls}" data-ui-tooltip-card="1" data-tooltip-base-width="{width_attr}" '
        f'style="max-width:min({width}, calc(100vw - 32px));">'
        f'<div class="ui-tooltip-title">{title_html}</div>'
        f'<div class="ui-tooltip-summary">{summary_html}</div>'
        f'<ul class="ui-tooltip-list">{item_html}</ul>'
        "</div>"
        "</div>"
    )


def _render_main_tooltip_autoposition_script() -> None:
    """Inject a tiny parent-page script that auto-flips main-area tooltips."""
    script = """
    <script>
    (function() {
      const parentWindow = window.parent;
      const parentDoc = parentWindow.document;
      const stateKey = "__xl_translate_tooltip_state__";
      const viewportMargin = 16;
      const gap = 8;

      const state = parentWindow[stateKey] = parentWindow[stateKey] || {
        observerBound: false,
        resizeBound: false,
        bindScheduled: false,
        repositionScheduled: false,
      };

      const resetCardSize = (card) => {
        const baseWidth = card.dataset.tooltipBaseWidth || "330px";
        card.style.maxWidth = `min(${baseWidth}, calc(100vw - 32px))`;
        card.style.maxHeight = "";
        card.style.overflowY = "";
      };

      const measureCard = (card) => {
        const rect = card.getBoundingClientRect();
        return {
          width: Math.ceil(rect.width),
          height: Math.ceil(rect.height),
        };
      };

      const positionTooltip = (trigger) => {
        const card = trigger.querySelector(".ui-tooltip-card[data-ui-tooltip-card='1']");
        if (!card) {
          return;
        }

        resetCardSize(card);

        const triggerRect = trigger.getBoundingClientRect();
        const viewportWidth = parentWindow.innerWidth || parentDoc.documentElement.clientWidth || 0;
        const viewportHeight = parentWindow.innerHeight || parentDoc.documentElement.clientHeight || 0;
        const initialCard = measureCard(card);

        const spaceRight = Math.max(viewportWidth - triggerRect.left - viewportMargin, 180);
        const spaceLeft = Math.max(triggerRect.right - viewportMargin, 180);
        const defaultX = trigger.dataset.tooltipDefaultX || "right";

        let horizontal = defaultX;
        if (defaultX === "right" && initialCard.width > spaceRight && spaceLeft > spaceRight) {
          horizontal = "left";
        } else if (defaultX === "left" && initialCard.width > spaceLeft && spaceRight > spaceLeft) {
          horizontal = "right";
        }

        trigger.dataset.tooltipX = horizontal;

        const availableWidth = Math.max(
          (horizontal === "right" ? spaceRight : spaceLeft) - 2,
          180
        );
        const baseWidth = card.dataset.tooltipBaseWidth || "330px";
        card.style.maxWidth = `min(${baseWidth}, ${Math.floor(availableWidth)}px)`;

        const resizedCard = measureCard(card);
        const spaceDown = Math.max(viewportHeight - triggerRect.bottom - gap - viewportMargin, 140);
        const spaceUp = Math.max(triggerRect.top - gap - viewportMargin, 140);
        const defaultY = trigger.dataset.tooltipDefaultY || "down";

        let vertical = defaultY;
        if (defaultY === "down" && resizedCard.height > spaceDown && spaceUp > spaceDown) {
          vertical = "up";
        } else if (defaultY === "up" && resizedCard.height > spaceUp && spaceDown > spaceUp) {
          vertical = "down";
        }

        trigger.dataset.tooltipY = vertical;

        const availableHeight = Math.max(
          (vertical === "down" ? spaceDown : spaceUp) - 2,
          140
        );
        if (resizedCard.height > availableHeight) {
          card.style.maxHeight = `${Math.floor(availableHeight)}px`;
          card.style.overflowY = "auto";
        }
      };

      const bindTrigger = (trigger) => {
        if (!trigger || trigger.dataset.tooltipAutoBound === "1") {
          return;
        }

        const updatePosition = () => {
          parentWindow.requestAnimationFrame(() => positionTooltip(trigger));
        };

        trigger.addEventListener("mouseenter", updatePosition, { passive: true });
        trigger.addEventListener("focusin", updatePosition, { passive: true });
        trigger.dataset.tooltipAutoBound = "1";
      };

      const bindAllTriggers = () => {
        parentDoc
          .querySelectorAll(".ui-tooltip-label[data-tooltip-auto='1']")
          .forEach(bindTrigger);
      };

      const scheduleBindAllTriggers = () => {
        if (state.bindScheduled) {
          return;
        }
        state.bindScheduled = true;
        parentWindow.requestAnimationFrame(() => {
          state.bindScheduled = false;
          bindAllTriggers();
        });
      };

      const repositionVisibleTriggers = () => {
        parentDoc
          .querySelectorAll(".ui-tooltip-label[data-tooltip-auto='1']")
          .forEach((trigger) => {
            if (
              trigger.matches(":hover") ||
              trigger.matches(":focus-within") ||
              trigger === parentDoc.activeElement
            ) {
              positionTooltip(trigger);
            }
          });
      };

      const scheduleRepositionVisibleTriggers = () => {
        if (state.repositionScheduled) {
          return;
        }
        state.repositionScheduled = true;
        parentWindow.requestAnimationFrame(() => {
          state.repositionScheduled = false;
          repositionVisibleTriggers();
        });
      };

      bindAllTriggers();

      if (!state.observerBound && parentWindow.MutationObserver) {
        const observer = new parentWindow.MutationObserver(() => scheduleBindAllTriggers());
        observer.observe(parentDoc.body, { childList: true, subtree: true });
        state.observerBound = true;
      }

      if (!state.resizeBound) {
        parentWindow.addEventListener("resize", scheduleRepositionVisibleTriggers, { passive: true });
        parentWindow.addEventListener("scroll", scheduleRepositionVisibleTriggers, { passive: true, capture: true });
        state.resizeBound = true;
      }
    })();
    </script>
    """
    st.iframe(script, height=1, tab_index=-1)


def render_main_tooltip_support() -> None:
    """Inject the shared main-area tooltip support script once per page render."""
    _render_main_tooltip_autoposition_script()


def render_tooltip_label(
    label: str,
    title: str,
    summary: str,
    items: list[str],
    *,
    width: str = "330px",
    trigger_class: str = "",
    card_class: str = "",
) -> None:
    """渲染可悬停标签文字触发的统一说明卡片。"""
    html_block = build_tooltip_label_html(
        label,
        title,
        summary,
        items,
        width=width,
        trigger_class=trigger_class,
        card_class=card_class,
    )
    st.markdown(html_block, unsafe_allow_html=True)


def render_radio_option_tooltips(
    *,
    container_key: str,
    option_markup: dict[str, str],
) -> None:
    """Attach shared tooltip markup to the visible labels of a Streamlit radio group."""
    if not option_markup:
        return

    safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in container_key)
    option_markup_json = json.dumps(option_markup, ensure_ascii=False)
    script = f"""
    <script>
    (function() {{
      const parentWindow = window.parent;
      const parentDoc = parentWindow.document;
      const stateKey = "__xl_translate_radio_option_tooltips__";
      const state = parentWindow[stateKey] = parentWindow[stateKey] || {{
        observerBound: false,
        registry: {{}},
        bindScheduled: false,
      }};
      const containerClass = "st-key-{safe_key}";
      state.registry[containerClass] = {option_markup_json};

      const normalize = (text) => (text || "").replace(/\\s+/g, " ").trim();

      const buildTooltipNode = (markup) => {{
        const temp = parentDoc.createElement("div");
        temp.innerHTML = markup;
        const tooltipNode = temp.firstElementChild;
        if (tooltipNode) {{
          tooltipNode.dataset.radioOptionTooltip = "1";
        }}
        return tooltipNode;
      }};

      const enhanceLabel = (label, markup) => {{
        if (!label || label.querySelector(".ui-tooltip-label[data-radio-option-tooltip='1']")) {{
          return;
        }}
        const textNode = label.querySelector("p");
        if (!textNode) {{
          return;
        }}
        const optionText = normalize(textNode.textContent || label.textContent || "");
        if (optionText) {{
          label.dataset.radioTooltipOption = optionText;
        }}
        const tooltipNode = buildTooltipNode(markup);
        if (!tooltipNode) {{
          return;
        }}
        textNode.replaceWith(tooltipNode);
      }};

      const bindAll = () => {{
        Object.entries(state.registry).forEach(([className, markupMap]) => {{
          parentDoc
            .querySelectorAll(`[class*="${{className}}"]`)
            .forEach((container) => {{
              container.querySelectorAll(".stRadio label").forEach((label) => {{
                const currentTextNode =
                  label.querySelector("p")
                  || label.querySelector(".ui-tooltip-label[data-radio-option-tooltip='1'] .ui-tooltip-text");
                const optionText = label.dataset.radioTooltipOption
                  || normalize(currentTextNode?.textContent || label.textContent || "");
                if (optionText) {{
                  label.dataset.radioTooltipOption = optionText;
                }}
                const markup = markupMap[optionText];
                if (!markup) {{
                  return;
                }}
                enhanceLabel(label, markup);
              }});
            }});
        }});
      }};

      const scheduleBindAll = () => {{
        if (state.bindScheduled) {{
          return;
        }}
        state.bindScheduled = true;
        parentWindow.requestAnimationFrame(() => {{
          state.bindScheduled = false;
          bindAll();
        }});
      }};

      bindAll();
      scheduleBindAll();

      if (!state.observerBound && parentWindow.MutationObserver) {{
        const observer = new parentWindow.MutationObserver(() => scheduleBindAll());
        observer.observe(parentDoc.body, {{ childList: true, subtree: true }});
        state.observerBound = true;
      }}
    }})();
    </script>
    """
    hook_container = st.container(key=f"radio-tooltip-hook-{safe_key}")
    with hook_container:
        st.iframe(script, height=1, tab_index=-1)


def render_setting_card(
    container,
    *,
    key: str,
    title: str | None = None,
    caption: str | None = None,
    density: str = "default",
):
    """Render a reusable settings card shell and return its body container."""
    card_container = container.container(key=_setting_card_container_key(key))
    with card_container:
        if density != "default":
            st.markdown(
                (
                    '<span class="ui-setting-card-marker '
                    f'ui-setting-card-marker--{html.escape(density, quote=True)}"></span>'
                ),
                unsafe_allow_html=True,
            )
        if title or caption:
            title_html = (
                f'<div class="ui-setting-card-title">{html.escape(title)}</div>'
                if title else ""
            )
            caption_html = (
                f'<div class="ui-setting-card-caption">{html.escape(caption)}</div>'
                if caption else ""
            )
            st.markdown(
                '<div class="ui-setting-card-header">'
                f"{title_html}"
                f"{caption_html}"
                '</div>',
                unsafe_allow_html=True,
            )
        body_container = st.container()
    return body_container


def render_field_group(
    container,
    *,
    key: str,
    label: str | None = None,
    label_html: str | None = None,
    hint: str | None = None,
    variant: str = "default",
):
    """Render a compact framed field group and return its body container."""
    field_container = container.container(key=_field_group_container_key(key))
    with field_container:
        if variant != "default":
            st.markdown(
                (
                    '<span class="ui-field-group-marker '
                    f'ui-field-group-marker--{html.escape(variant, quote=True)}"></span>'
                ),
                unsafe_allow_html=True,
            )
        if label_html or label or hint:
            label_markup = label_html or html.escape(label or "")
            hint_markup = (
                f'<div class="ui-field-group__hint">{html.escape(hint)}</div>'
                if hint
                else ""
            )
            st.markdown(
                '<div class="ui-field-group__header">'
                f'<div class="ui-field-group__label">{label_markup}</div>'
                f"{hint_markup}"
                '</div>',
                unsafe_allow_html=True,
            )
        body_container = st.container()
    return body_container


def render_checkbox_tooltip_row(
    container,
    label: str,
    title: str,
    summary: str,
    items: list[str],
    *,
    key: str,
    value: bool | None = None,
    disabled: bool = False,
    on_change=None,
    checkbox_label: str | None = None,
    trigger_class: str = "ui-checkbox-tooltip-trigger",
    card_class: str = "tooltip-placement-down",
    row_class: str = "",
    width: str = "330px",
) -> bool:
    """Render a compact checkbox row with the shared main-area tooltip UI."""
    checkbox_kwargs = {
        "key": key,
        "disabled": disabled,
        "on_change": on_change,
        "label_visibility": "collapsed",
    }
    if value is not None and key not in st.session_state:
        checkbox_kwargs["value"] = value

    row_container = container.container(key=_checkbox_row_container_key(key))
    with row_container:
        if row_class:
            st.markdown(
                f'<span class="ui-checkbox-row-anchor {html.escape(row_class, quote=True)}"></span>',
                unsafe_allow_html=True,
            )
        checkbox_col, label_col = st.columns([0.48, 7.52], gap="small", vertical_alignment="center")
        with checkbox_col:
            st.checkbox(checkbox_label or f"{label} checkbox", **checkbox_kwargs)
        with label_col:
            render_tooltip_label(
                label,
                title,
                summary,
                items,
                width=width,
                trigger_class=trigger_class,
                card_class=card_class,
            )

    return st.session_state.get(key, value if value is not None else False)


# ── 日志区 ────────────────────────────────────────────────────────────────────

_LOG_COLOR = {
    "INFO":  "log-info",
    "OK":    "log-ok",
    "WARN":  "log-warn",
    "ERROR": "log-error",
}

_LOG_FULL_RENDER_LIMIT = 300
_LOG_WINDOW_SIZE = 200


def _slice_log_messages(messages: list[dict]) -> tuple[list[dict], int]:
    """Keep daily runs fully visible while windowing extreme log volumes."""
    if len(messages) <= _LOG_FULL_RENDER_LIMIT:
        return messages, 0
    hidden_count = max(len(messages) - _LOG_WINDOW_SIZE, 0)
    return messages[-_LOG_WINDOW_SIZE:], hidden_count


def _render_log_autofollow_script(container_id: str) -> None:
    """Inject a tiny iframe script that keeps the parent log box pinned to bottom."""
    script = f"""
    <script>
    (function() {{
      const containerId = {json.dumps(container_id)};
      const parentWindow = window.parent;
      const parentDoc = parentWindow.document;
      const stateKey = "__xl_translate_log_follow__";
      parentWindow[stateKey] = parentWindow[stateKey] || {{}};

      const el = parentDoc.getElementById(containerId);
      if (!el) {{
        return;
      }}

      const state = parentWindow[stateKey][containerId] || {{
        autoFollow: true,
        lastLineCount: 0,
        lastScrollHeight: 0,
        bottomGap: 0,
      }};

      if (!el.dataset.autoFollowBound) {{
        const updateAutoFollow = () => {{
          const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
          state.autoFollow = distanceFromBottom <= 24;
          state.bottomGap = el.scrollHeight - el.scrollTop;
          parentWindow[stateKey][containerId] = state;
        }};

        el.addEventListener("scroll", updateAutoFollow, {{ passive: true }});
        el.dataset.autoFollowBound = "1";
      }}

      const lineCount = Number(el.dataset.lineCount || "0");
      const contentChanged = (
        lineCount !== state.lastLineCount ||
        el.scrollHeight !== state.lastScrollHeight
      );

      if (state.autoFollow || !contentChanged) {{
        el.scrollTop = el.scrollHeight;
      }} else if (typeof state.bottomGap === "number") {{
        el.scrollTop = Math.max(el.scrollHeight - state.bottomGap, 0);
      }}

      state.lastLineCount = lineCount;
      state.lastScrollHeight = el.scrollHeight;
      state.bottomGap = el.scrollHeight - el.scrollTop;
      parentWindow[stateKey][containerId] = state;
    }})();
    </script>
    """
    st.iframe(script, height=1, tab_index=-1)


def render_log_area(
    messages: list[dict],
    *,
    container_id: str = "translate-log-container",
    auto_follow: bool = True,
) -> None:
    """
    渲染终端风格日志区。
    messages: [{"level": "INFO", "message": "...", "ts": "14:32:01"}, ...]
    """
    if not messages:
        return

    visible_messages, hidden_count = _slice_log_messages(messages)
    lines_html = []
    for m in visible_messages:
        level = str(m.get("level", "INFO"))
        cls = _LOG_COLOR.get(level, "log-info")
        ts = html.escape(str(m.get("ts", "")))
        text = html.escape(str(m.get("message", ""))).replace("\n", "<br>")
        lines_html.append(
            '<div class="log-line">'
            f'<span class="log-ts">{ts}</span>'
            f'<span class="{cls}">[{html.escape(level)}]</span> {text}'
            '</div>'
        )

    notice_html = ""
    if hidden_count:
        notice_html = (
            '<div class="log-truncate-note">'
            f'日志较多，已折叠前 {hidden_count} 条，仅显示最新 {len(visible_messages)} 条。'
            '</div>'
        )

    container_id_html = html.escape(container_id, quote=True)
    html_block = (
        f'<div id="{container_id_html}" class="log-container" '
        f'data-line-count="{len(visible_messages)}">'
        f'{notice_html}'
        f'{"".join(lines_html)}'
        '<div class="log-bottom-anchor" aria-hidden="true"></div>'
        '</div>'
    )
    st.markdown(html_block, unsafe_allow_html=True)

    if auto_follow:
        _render_log_autofollow_script(container_id)


# ── 进度展示 ──────────────────────────────────────────────────────────────────

def render_progress(
    file_index: int,
    file_total: int,
    batch_done: int,
    batch_total: int,
    current_file: str,
) -> None:
    """渲染总进度条与当前文件进度条。"""
    if file_total <= 0:
        return

    overall_pct = (file_index + (batch_done / max(batch_total, 1))) / file_total
    file_pct    = batch_done / max(batch_total, 1)

    st.markdown(f"**正在处理：{current_file}（{file_index + 1} / {file_total}）**")
    st.progress(min(overall_pct, 1.0), text=f"总进度 {int(overall_pct * 100)}%")
    st.progress(min(file_pct, 1.0),    text=f"当前文件 {int(file_pct * 100)}%")


# ── 统计卡片 ──────────────────────────────────────────────────────────────────

def render_stat_cards(
    elapsed_sec: float,
    file_count: int,
    tm_hit: int,
    api_call: int,
) -> None:
    """渲染任务完成后的四格统计卡片。"""
    minutes = int(elapsed_sec // 60)
    seconds = int(elapsed_sec % 60)
    time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"

    data = [
        ("⏱️ 耗时",    time_str),
        ("📁 文件数",   f"{file_count} 个"),
        ("🧠 TM 命中",  f"{tm_hit:,} 条词条"),
        ("⚡ API 翻译", f"{api_call:,} 条词条"),
    ]

    st.markdown('<div class="stat-grid-anchor"></div>', unsafe_allow_html=True)
    cols = st.columns(4, gap="small")
    for col, (label, value) in zip(cols, data):
        with col:
            st.markdown(
                f'<div class="stat-card">'
                f'<div class="stat-label">{html.escape(label)}</div>'
                f'<div class="stat-number">{html.escape(value)}</div>'
                '</div>',
                unsafe_allow_html=True,
            )


# ── 文件列表表格 ──────────────────────────────────────────────────────────────

_COL_W = [0.7, 4.5, 1.2, 0.8]   # 勾选 | 文件名 | 大小 | 分表数

_CTR = '<div class="file-table-center">{}</div>'   # 居中模板

# 修复表头复选框与同行文本垂直不对齐问题
_TABLE_HEADER_CSS = """
<style>
/* 表头行复选框垂直居中 */
div[data-testid="stCheckbox"] {
    display: flex !important;
    align-items: center !important;
}
div[data-testid="stCheckbox"] label {
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
    min-height: 32px !important;
}
</style>
"""


def render_file_table(file_items: list) -> None:
    """渲染扫描结果文件列表（含勾选列，表头在容器内）。"""
    if not file_items:
        st.info("未发现 Excel 文件，请检查文件夹路径。")
        return

    st.markdown(_TABLE_HEADER_CSS, unsafe_allow_html=True)

    n = len(file_items)
    st.session_state["_file_count"] = n

    def _sync_all():
        val = st.session_state.get("file_check_all", True)
        for idx in range(st.session_state.get("_file_count", 0)):
            st.session_state[f"file_check_{idx}"] = val

    st.markdown('<span class="file-table-header-anchor"></span>', unsafe_allow_html=True)
    hcols = st.columns(_COL_W, vertical_alignment="center")
    hcols[0].checkbox(
        "全选文件", key="file_check_all", on_change=_sync_all, label_visibility="collapsed",
    )
    hcols[1].markdown('<div class="file-table-head">文件名</div>', unsafe_allow_html=True)
    hcols[2].markdown(_CTR.format('<span class="file-table-head">大小</span>'), unsafe_allow_html=True)
    hcols[3].markdown(_CTR.format('<span class="file-table-head">分表数</span>'), unsafe_allow_html=True)

    # CSS 锚点：供 CSS 通过相邻兄弟选择器定位数据行容器
    st.markdown('<span class="file-rows-anchor" style="display:none"></span>',
                unsafe_allow_html=True)

    # ── 数据行（CSS 控制最大高度与内滚动）──
    with st.container():
        for i, f in enumerate(file_items):
            st.markdown('<span class="file-table-row-anchor"></span>', unsafe_allow_html=True)
            rcols = st.columns(_COL_W, vertical_alignment="center")
            rcols[0].checkbox(f"选择文件 {i + 1}", key=f"file_check_{i}", label_visibility="collapsed")
            rcols[1].markdown(
                f'<div class="file-cell file-cell--name" title="{html.escape(f.name, quote=True)}">{html.escape(f.name)}</div>',
                unsafe_allow_html=True,
            )
            rcols[2].markdown(
                _CTR.format(f'<span class="file-cell file-cell--metric">{f.size_kb:.1f} KB</span>'),
                unsafe_allow_html=True,
            )
            rcols[3].markdown(
                _CTR.format(f'<span class="file-cell file-cell--metric">{len(f.sheets)}</span>'),
                unsafe_allow_html=True,
            )


# ── 确认弹窗 ──────────────────────────────────────────────────────────────────

def confirm_dialog(key: str, message: str, confirm_label: str = "确认") -> bool:
    """
    二次确认组件（用于不可逆操作）。
    返回 True 表示用户已点击确认。
    使用 session_state[key] 控制状态。
    """
    confirm_key = f"_confirm_{key}"
    if st.button(confirm_label, key=f"_btn_{key}", type="secondary"):
        st.session_state[confirm_key] = True

    if st.session_state.get(confirm_key):
        st.warning(message)
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✓ 确认执行", key=f"_yes_{key}", type="secondary"):
                st.session_state[confirm_key] = False
                return True
        with col2:
            if st.button("✕ 取消", key=f"_no_{key}"):
                st.session_state[confirm_key] = False

    return False
