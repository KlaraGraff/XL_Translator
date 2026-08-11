"""Portable model configuration import and export helpers.

The JSON schema is shared by the native Qt UI and the sidecar API so users can
move model profiles, scoped credentials, and throughput tuning without a UI
dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app_meta import APP_NAME, APP_VERSION
from config import CLOUD_ENGINES, normalize_cloud_base_url
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    reset_all_model_role_availability,
    resolve_effective_model_config,
    role_label,
    validate_all_model_roles,
)
from core.model_throughput import get_model_throughput, set_model_throughput
from settings import (
    KEY_ORIGIN_IMPORTED,
    AppSettings,
    delete_connection_key,
    get_connection_scoped_key,
    get_key,
    is_imported_connection_key,
    is_imported_provider_key,
    parse_api_key_scope,
    save_key,
)

MODEL_CONFIG_EXPORT_TYPE = "translator_model_config"
MODEL_CONFIG_EXPORT_VERSION = 3
MODEL_CONFIG_SETTING_KEYS = (
    "engine",
    "cleaner_model_role",
    "image_model_role",
    "pdf_review_model_role",
)
MODEL_CONFIG_CLOUD_FIELDS = (
    "cloud_provider",
    "cloud_model",
    "cloud_base_url",
    "cloud_provider_configs",
)
MODEL_CONFIG_ROLE_CLOUD_FIELDS = (
    "source_role",
    *MODEL_CONFIG_CLOUD_FIELDS,
)
MODEL_PROFILE_ROLES = (
    ("translation", ROLE_TRANSLATION),
    ("cleaner", ROLE_CLEANER),
    ("pdf_translation", ROLE_IMAGE),
    ("pdf_review", ROLE_PDF_REVIEW),
)
MODEL_PROFILE_ROLE_BY_KEY = {
    profile_key: role for profile_key, role in MODEL_PROFILE_ROLES
}
MODEL_PROFILE_SETTING_KEY_BY_ROLE = {
    ROLE_TRANSLATION: "engine",
    ROLE_CLEANER: "cleaner_model_role",
    ROLE_IMAGE: "image_model_role",
    ROLE_PDF_REVIEW: "pdf_review_model_role",
}

ApiKeyGetter = Callable[[str, str], str]
ApiKeySaver = Callable[..., None]
ImportedApiKeyCheck = Callable[[str, str], bool]
ImportedScopedApiKeyCheck = Callable[[str], bool]

# ── 导出回执里每条连接的密钥去向 ──────────────────────────
# 「导出含 Key」现在会扣下从别人配置导入来的密钥（见 ``_exportable_provider_key``），
# 界面必须能逐条说清楚哪些带走了、哪些被扣下了，只报个数字用户无从对照。
API_KEY_EXPORT_INCLUDED = "exported"
API_KEY_EXPORT_WITHHELD_IMPORTED = "withheld_imported"
API_KEY_EXPORT_MISSING = "missing"
# 回执里这一行说的是哪种东西：界面上看得见的连接，还是「这个角色记住的其他服务商配置」。
API_KEY_EXPORT_KIND_CONNECTION = "connection"
API_KEY_EXPORT_KIND_PROVIDER_MEMORY = "provider_memory"


@dataclass(frozen=True)
class ImportedModelConfig:
    """Validated model-settings and key updates extracted from JSON."""

    model_config: dict[str, dict[str, Any]]
    api_keys: dict[str, str]
    scoped_api_keys: list[dict[str, str]]
    throughput_profiles: dict[str, dict[str, Any]]
    profile_throughputs: dict[str, dict[str, Any]]


def build_model_config_export_payload(
    settings: AppSettings,
    *,
    get_api_key: ApiKeyGetter = get_key,
    get_scoped_api_key: Callable[[str], str] = get_connection_scoped_key,
    is_imported_api_key: ImportedApiKeyCheck = is_imported_provider_key,
    is_imported_scoped_api_key: ImportedScopedApiKeyCheck = is_imported_connection_key,
    include_api_key: bool = False,
    include_api_keys: bool | None = None,
    api_key_report: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the current v3 export payload, omitting secrets by default.

    传入 ``api_key_report`` 可以收一份「每条连接的密钥去向」回执（仅在导出含 Key 时
    填充）。做成出参而不是改返回值，是因为返回的这份文档就是要原样写进文件的——回执
    绝不能混进导出文件里，否则收到文件的人反而多了一份本机连接清单。
    """
    if include_api_keys is not None:
        include_api_key = bool(include_api_keys)
    return {
        "type": MODEL_CONFIG_EXPORT_TYPE,
        "version": MODEL_CONFIG_EXPORT_VERSION,
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "model_profiles": _model_profiles_for_export(
            settings,
            get_api_key=get_api_key,
            get_scoped_api_key=get_scoped_api_key,
            is_imported_api_key=is_imported_api_key,
            is_imported_scoped_api_key=is_imported_scoped_api_key,
            include_api_key=include_api_key,
            api_key_report=api_key_report,
        ),
    }


def parse_model_config_import(raw: object) -> ImportedModelConfig:
    """Parse only the current v3 model-configuration payload."""
    if not isinstance(raw, dict):
        raise ValueError("Imported configuration must be a JSON object.")
    if raw.get("type") != MODEL_CONFIG_EXPORT_TYPE:
        # 按钮上不再写版本号，所以这里必须自己说清楚“文件不对”，而不是丢一个内部代号。
        raise ValueError("这个文件不是当前版本的模型配置格式，无法导入。请使用本应用“导出（不含 Key）”或“导出含 Key”生成的配置文件。")
    try:
        version = int(raw.get("version"))
    except (TypeError, ValueError):
        raise ValueError("这个文件不是当前版本的模型配置格式，无法导入。请使用本应用“导出（不含 Key）”或“导出含 Key”生成的配置文件。") from None
    if version > MODEL_CONFIG_EXPORT_VERSION:
        # 更新的版本才是真读不了；旧版本必须读得进来，否则一次格式升级就等于把
        # 用户手里所有导出文件作废，而这些文件常常是他唯一的一份配置备份。
        raise ValueError("这个文件由更新版本的应用导出，当前版本读不了。请先把应用升级到最新版再导入。")
    if isinstance(raw.get("model_profiles"), dict):
        return _parse_model_profiles(raw)
    raise ValueError("model_profiles must be a JSON object.")


def apply_model_config_import(
    settings: AppSettings,
    imported: ImportedModelConfig,
    *,
    save_api_key: ApiKeySaver = save_key,
    delete_scoped_api_key: Callable[[str], None] = delete_connection_key,
    throughput_errors: list[str] | None = None,
    defer_key_writes: list[Callable[[], None]] | None = None,
) -> AppSettings:
    """Apply an import to a copied settings model and persist supplied keys.

    The caller owns persistence of the returned settings object. This lets the
    API and the native UI preserve their existing save timing and error UX.

    Pass ``defer_key_writes`` to receive the key mutations as one callable
    instead of having them applied here: the key store and the settings file
    are two files, and writing the secrets first means a failed settings save
    leaves credentials on disk that no stored configuration points at.  Callers
    that persist the returned settings should run the collected callables only
    after that write succeeds.
    """
    payload = settings.model_dump(mode="json")
    dropped_connection_ids: list[str] = []
    for key in MODEL_CONFIG_SETTING_KEYS:
        if key not in imported.model_config:
            continue
        current = dict(payload.get(key) or {})
        imported_fields = imported.model_config[key]
        if isinstance(imported_fields.get("connections"), list):
            imported_fields = dict(imported_fields)
            imported_fields["connections"] = _rebind_imported_connections(
                [
                    connection
                    for connection in (current.get("connections") or [])
                    if isinstance(connection, dict)
                ],
                [
                    connection
                    for connection in imported_fields["connections"]
                    if isinstance(connection, dict)
                ],
                dropped_connection_ids,
            )
        current = _merge_imported_fields(current, imported_fields)
        _synchronize_selected_provider_memory(current, imported_fields)
        if "mode" not in imported_fields:
            # Existing local mode remains local when an import does not
            # explicitly choose a mode.  A sparse v3 file must never silently
            # replace an unmentioned setting.  Every role has a mode now, so
            # this is no longer only the engine's concern.
            current.setdefault("mode", "cloud")
        payload[key] = current
    if imported.throughput_profiles:
        profiles = dict(payload.get("model_throughput_profiles") or {})
        profiles.update(imported.throughput_profiles)
        payload["model_throughput_profiles"] = profiles

    updated = AppSettings.model_validate(payload)
    # A shared connection can make an untouched follower invalid.  Verify the
    # complete effective graph before writing any imported secret, then reset
    # all role test states because no imported result is trustworthy.
    validate_all_model_roles(updated)
    reset_all_model_role_availability(updated)
    def _commit_keys() -> None:
        # 这里写的每一把 Key 都来自别人的配置文件，必须标成 imported：本机之后再
        # 「导出含 Key」时它们会被扣下，免得 A 的密钥经 B 之手传到 C。用户自己在面板上
        # 重新填一次同一个作用域的 Key，标记就会被清掉，那把新 Key 是他自己的，可以导出。
        for provider, api_key in imported.api_keys.items():
            save_api_key(provider, api_key, origin=KEY_ORIGIN_IMPORTED)
        for entry in imported.scoped_api_keys:
            save_api_key(
                entry["provider"],
                entry["api_key"],
                entry["base_url"],
                origin=KEY_ORIGIN_IMPORTED,
            )
        # 清理放在写入之后：导入的密钥全部落在 provider + Base URL 作用域下，和这里删的
        # conn::<id> 作用域互不相干，但万一上面抛错，旧密钥还留着，配置也还是旧的。
        for connection_id in dropped_connection_ids:
            if connection_id:
                delete_scoped_api_key(connection_id)

    if defer_key_writes is None:
        _commit_keys()
    else:
        defer_key_writes.append(_commit_keys)
    for role, throughput in imported.profile_throughputs.items():
        try:
            config = resolve_effective_model_config(updated, role)
            set_model_throughput(
                updated,
                config,
                batch_size=throughput.get("batch_size"),
                concurrency=throughput.get("concurrency"),
            )
        except Exception as exc:  # noqa: BLE001 - one role must not sink the import
            # The rest of the import still applies, but a silently dropped
            # batch_size/concurrency looks identical to a successful one.
            logger.warning("导入吞吐配置失败：role={} error={}", role, exc)
            if throughput_errors is not None:
                throughput_errors.append(role)
    return updated


def _connection_endpoint(connection: dict[str, Any]) -> tuple[str, str]:
    """What "the same connection" means when a pool is replaced wholesale."""
    provider = str(connection.get("provider") or "").strip()
    base_url = str(connection.get("base_url") or "").strip()
    normalized = (
        normalize_cloud_base_url(provider, base_url) if provider else base_url
    ).rstrip("/")
    return provider, normalized


def _rebind_imported_connections(
    local: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    dropped_connection_ids: list[str],
) -> list[dict[str, str]]:
    """Keep a slot's connection id when the imported entry is the same endpoint.

    连接池是整份替换的（见 ``connection_values``），而导出文件里从来不带 id——id 是
    本机的密钥作用域，换台机器就指不到东西。于是导入一次，本机每条连接都换了新 id，
    ``conn::<id>`` 下存的密钥全成了没人指得到的孤儿。最刺眼的是「导出（不含 Key）→
    原样导入回来」：配置一个字没变，密钥却被清空了，用户得把每条连接的 Key 重新粘一遍。

    所以按位置认领：同一个槽位上服务商和 Base URL 都没变，就是同一条连接，沿用本机
    原来的 id，密钥原地留着。端点变了（换了服务商或换了服务器）或者槽位在新池子里
    根本不存在了，那条 id 才真的没了主人——照旧删掉它的密钥，不然 keys.json 会随每
    次导入越堆越多，堆的还是没有任何界面能看见、能改、能删的凭据。
    """
    rebound: list[dict[str, str]] = []
    for index, connection in enumerate(incoming):
        entry = dict(connection)
        local_entry = local[index] if index < len(local) else None
        local_id = str((local_entry or {}).get("id") or "").strip()
        if (
            local_entry is not None
            and local_id
            and _connection_endpoint(local_entry) == _connection_endpoint(entry)
        ):
            entry["id"] = local_id
        elif local_id:
            dropped_connection_ids.append(local_id)
        rebound.append(entry)
    for local_entry in local[len(incoming):]:
        local_id = str(local_entry.get("id") or "").strip()
        if local_id:
            dropped_connection_ids.append(local_id)
    return rebound


def _merge_imported_fields(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge only v3 fields explicitly present in an import payload.

    Model configuration import is additive: absent role fields, provider
    entries, and per-provider fields must remain untouched.  This recursive
    merge deliberately has no deletion semantics, which also keeps an empty
    object from erasing a user's stored connection memory.
    """
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_imported_fields(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _synchronize_selected_provider_memory(
    owner: dict[str, Any], imported_fields: dict[str, Any]
) -> None:
    """Keep explicit selected connection values authoritative after a merge.

    Settings keep a remembered ``cloud_provider_configs`` entry for every
    provider.  Its normalization intentionally wins over the legacy/current
    fields, so importing a new selected model would otherwise be shadowed by
    an old entry for the same provider.  Mirror only explicitly imported
    selected values into that one provider entry; all other remembered
    connections remain untouched.
    """
    selected_fields = {
        field: imported_fields[field]
        for field in ("cloud_model", "cloud_base_url")
        if field in imported_fields
    }
    if not selected_fields:
        return
    provider = str(owner.get("cloud_provider") or "").strip()
    if not provider:
        return
    configs = dict(owner.get("cloud_provider_configs") or {})
    entry = dict(configs.get(provider) or {})
    entry.update(selected_fields)
    configs[provider] = entry
    owner["cloud_provider_configs"] = configs


def _model_profiles_for_export(
    settings: AppSettings,
    *,
    get_api_key: ApiKeyGetter,
    get_scoped_api_key: Callable[[str], str],
    is_imported_api_key: ImportedApiKeyCheck,
    is_imported_scoped_api_key: ImportedScopedApiKeyCheck,
    include_api_key: bool,
    api_key_report: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    payload = settings.model_dump(mode="json")
    profiles: dict[str, dict[str, Any]] = {}
    for profile_key, role in MODEL_PROFILE_ROLES:
        setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
        owner = dict(payload.get(setting_key) or {})
        profile: dict[str, Any] = {
            "role": role,
            "label": role_label(role),
            "cloud": _cloud_profile_for_export(
                owner,
                role=role,
                get_api_key=get_api_key,
                is_imported_api_key=is_imported_api_key,
                api_key_report=api_key_report,
                # The importer only reads keys from the cloud block, so a
                # connection-scoped key must be exported here or a with-keys
                # round-trip loses it.  这里拿到的已经是过滤后的结果：主用连接的
                # Key 若是导入来的，就当作「没有可导出的 Key」。
                scoped_api_key=_primary_key_for_export(
                    owner,
                    get_api_key=get_api_key,
                    get_scoped_api_key=get_scoped_api_key,
                    is_imported_api_key=is_imported_api_key,
                    is_imported_scoped_api_key=is_imported_scoped_api_key,
                )
                if include_api_key
                else "",
                include_api_key=include_api_key,
            ),
            "effective": _effective_profile_for_export(settings, role),
            "throughput": _throughput_profile_for_export(settings, role),
        }
        # All four roles carry mode/source_role/local now, so the export is
        # uniform; a file written before that still imports because the reader
        # defaults every missing key.
        profile["connections"] = _connections_for_export(
            owner,
            role=role,
            get_api_key=get_api_key,
            get_scoped_api_key=get_scoped_api_key,
            is_imported_api_key=is_imported_api_key,
            is_imported_scoped_api_key=is_imported_scoped_api_key,
            include_api_key=include_api_key,
            api_key_report=api_key_report,
        )
        profile["mode"] = str(owner.get("mode") or "cloud").strip() or "cloud"
        profile["source_role"] = str(
            owner.get("source_role") or SOURCE_INDEPENDENT
        ).strip()
        profile["local"] = _local_profile_for_export(owner)
        profiles[profile_key] = profile
    return profiles


def _exportable_provider_key(
    provider: str,
    base_url: str,
    *,
    get_api_key: ApiKeyGetter,
    is_imported_api_key: ImportedApiKeyCheck,
) -> tuple[str, str]:
    """返回 (可以写进导出文件的密钥, 回执状态)。

    从别人的配置导入进来的密钥一律不导出。不这么做的话密钥会连环传播：A 把配置连
    Key 一起给了 B，B 再导一份给 C，A 的 Key 就到了 C 手上，而 A 从头到尾不知情。
    连接的其余信息（服务商、Base URL、模型名）照常导出，只是不带 Key。
    """
    api_key = str(get_api_key(provider, base_url) or "").strip()
    if not api_key:
        return "", API_KEY_EXPORT_MISSING
    if is_imported_api_key(provider, base_url):
        return "", API_KEY_EXPORT_WITHHELD_IMPORTED
    return api_key, API_KEY_EXPORT_INCLUDED


def _exportable_connection_key(
    connection_id: str,
    provider: str,
    base_url: str,
    *,
    get_api_key: ApiKeyGetter,
    get_scoped_api_key: Callable[[str], str],
    is_imported_api_key: ImportedApiKeyCheck,
    is_imported_scoped_api_key: ImportedScopedApiKeyCheck,
) -> tuple[str, str]:
    """一条连接实际会导出的密钥，按它自己的解析顺序判断来源。

    连接作用域的 Key 压过 provider 作用域（和拨号时的解析顺序一致）。若连接自己那把
    Key 是导入来的，就到此为止，**不再回退**到 provider 作用域：回退等于导出一把和
    本机这条连接实际在用的不是同一把的 Key，收到文件的人拨到的会是另一个账号。
    """
    scoped_id = str(connection_id or "").strip()
    if scoped_id:
        scoped_key = str(get_scoped_api_key(scoped_id) or "").strip()
        if scoped_key:
            if is_imported_scoped_api_key(scoped_id):
                return "", API_KEY_EXPORT_WITHHELD_IMPORTED
            return scoped_key, API_KEY_EXPORT_INCLUDED
    return _exportable_provider_key(
        provider,
        base_url,
        get_api_key=get_api_key,
        is_imported_api_key=is_imported_api_key,
    )


def _connection_report_name(raw: dict[str, Any], index: int, base_url: str) -> str:
    """回执里怎么称呼这条连接：连接名 → Base URL → 序号。

    只报数字（「有 2 个密钥没导出」）用户没法对照到界面上的哪一条，所以必须给个能
    定位的名字；连接没起名是常态，用它的地址兜底，再不行才用序号。
    """
    label = str(raw.get("label") or "").strip()
    if label:
        return label
    if base_url:
        return base_url
    return f"连接 {index + 1}"


def _connections_for_export(
    owner: dict[str, Any],
    *,
    role: str = "",
    get_api_key: ApiKeyGetter,
    get_scoped_api_key: Callable[[str], str],
    is_imported_api_key: ImportedApiKeyCheck,
    is_imported_scoped_api_key: ImportedScopedApiKeyCheck,
    include_api_key: bool,
    api_key_report: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Export a role's whole connection pool, not only its primary.

    The ``cloud`` block already describes entry 0, but a role can own several
    connections for failover and parallel spreading.  A bundle that carried
    only the primary would quietly halve the recipient's setup, which is
    exactly what a one-click whole-service bundle must not do.

    Ids are deliberately not exported: they scope the local key store, and
    reusing one machine's id on another would point at a key that is not there.
    The importing side keeps its own id wherever the endpoint at that slot is
    unchanged (see ``_rebind_imported_connections``) and mints a fresh one
    otherwise, resolving keys through the provider/Base URL scope written below.
    """
    entries: list[dict[str, str]] = []
    for index, raw in enumerate(owner.get("connections") or []):
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip()
        base_url = normalize_cloud_base_url(
            provider,
            str(raw.get("base_url") or "").strip(),
        )
        entry = {
            "label": str(raw.get("label") or "").strip(),
            "provider": provider,
            "model": str(raw.get("model") or "").strip(),
            "base_url": base_url,
        }
        if include_api_key:
            api_key, status = _exportable_connection_key(
                str(raw.get("id") or "").strip(),
                provider,
                base_url,
                get_api_key=get_api_key,
                get_scoped_api_key=get_scoped_api_key,
                is_imported_api_key=is_imported_api_key,
                is_imported_scoped_api_key=is_imported_scoped_api_key,
            )
            if api_key:
                entry["api_key"] = api_key
            if api_key_report is not None:
                api_key_report.append({
                    "kind": API_KEY_EXPORT_KIND_CONNECTION,
                    "role": role,
                    "role_label": role_label(role) if role else "",
                    "connection": _connection_report_name(raw, index, base_url),
                    "provider": provider,
                    "status": status,
                })
        entries.append(entry)
    return entries


def _primary_key_for_export(
    owner: dict[str, Any],
    *,
    get_api_key: ApiKeyGetter,
    get_scoped_api_key: Callable[[str], str],
    is_imported_api_key: ImportedApiKeyCheck,
    is_imported_scoped_api_key: ImportedScopedApiKeyCheck,
) -> str:
    """主用连接真正会被导出的那把 Key（导入来的已经在这里被过滤掉）。

    导出文件的 ``cloud`` 块描述的就是连接池里的第 0 条（见 settings.py 的
    ``_sync_connection_pool``），所以它带的 Key 必须和那条连接的判定结果一致，
    否则会出现「连接列表里被扣下了、cloud 块里又漏出去」这种半拉子过滤。
    """
    connections = list(owner.get("connections") or [])
    primary = connections[0] if connections and isinstance(connections[0], dict) else {}
    provider = str(owner.get("cloud_provider") or "").strip()
    base_url = normalize_cloud_base_url(
        provider,
        str(owner.get("cloud_base_url") or "").strip(),
    )
    api_key, _status = _exportable_connection_key(
        str(primary.get("id") or "").strip(),
        provider,
        base_url,
        get_api_key=get_api_key,
        get_scoped_api_key=get_scoped_api_key,
        is_imported_api_key=is_imported_api_key,
        is_imported_scoped_api_key=is_imported_scoped_api_key,
    )
    return api_key


def _cloud_profile_for_export(
    owner: dict[str, Any],
    *,
    role: str = "",
    get_api_key: ApiKeyGetter,
    is_imported_api_key: ImportedApiKeyCheck,
    scoped_api_key: str = "",
    include_api_key: bool,
    api_key_report: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    provider = str(owner.get("cloud_provider") or "").strip()
    base_url = normalize_cloud_base_url(
        provider,
        str(owner.get("cloud_base_url") or "").strip(),
    )
    profile: dict[str, Any] = {
        "provider": provider,
        "model": str(owner.get("cloud_model") or "").strip(),
        "base_url": base_url,
        "provider_configs": _provider_configs_for_export(
            owner,
            role=role,
            get_api_key=get_api_key,
            is_imported_api_key=is_imported_api_key,
            include_api_key=include_api_key,
            api_key_report=api_key_report,
        ),
    }
    # The connection-scoped key is what the primary actually resolves, so it
    # outranks the provider-scoped store here.  ``scoped_api_key`` 已经由
    # ``_primary_key_for_export`` 做完来源过滤并含 provider 作用域的回退，
    # 这里不能再自己回退一次，否则被扣下的密钥会从这条路漏出去。
    api_key = scoped_api_key if include_api_key else ""
    if api_key:
        profile["api_key"] = api_key
    return profile


def _provider_configs_for_export(
    owner: dict[str, Any],
    *,
    role: str = "",
    get_api_key: ApiKeyGetter,
    is_imported_api_key: ImportedApiKeyCheck,
    include_api_key: bool,
    api_key_report: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    cloud_providers = set(CLOUD_ENGINES.values())
    provider_configs = dict(owner.get("cloud_provider_configs") or {})
    current_provider = str(owner.get("cloud_provider") or "").strip()
    if current_provider in cloud_providers:
        provider_configs.setdefault(
            current_provider,
            {
                "cloud_model": str(owner.get("cloud_model") or "").strip(),
                "cloud_base_url": str(owner.get("cloud_base_url") or "").strip(),
            },
        )

    profiles: dict[str, dict[str, str]] = {}
    for provider, raw_config in sorted(provider_configs.items()):
        provider = str(provider or "").strip()
        if provider not in cloud_providers or not isinstance(raw_config, dict):
            continue
        base_url = normalize_cloud_base_url(
            provider,
            str(raw_config.get("cloud_base_url") or "").strip(),
        )
        entry = {
            "model": str(raw_config.get("cloud_model") or "").strip(),
            "base_url": base_url,
        }
        # 这些是「这个角色记住的其他服务商配置」，同样要按来源过滤，否则一把导入来的
        # 密钥即使在连接列表里被扣下，也会从这条记忆里漏出去。它们不是界面上的连接，
        # 所以只在**被扣下**时进回执：全都列出来是噪音，一条都不列就成了「悄悄扣下」，
        # 而回执存在的意义就是不许有悄悄扣下的密钥。
        api_key = ""
        if include_api_key:
            api_key, status = _exportable_provider_key(
                provider,
                base_url,
                get_api_key=get_api_key,
                is_imported_api_key=is_imported_api_key,
            )
            if api_key_report is not None and status == API_KEY_EXPORT_WITHHELD_IMPORTED:
                api_key_report.append({
                    "kind": API_KEY_EXPORT_KIND_PROVIDER_MEMORY,
                    "role": role,
                    "role_label": role_label(role) if role else "",
                    "connection": base_url or provider,
                    "provider": provider,
                    "status": status,
                })
        if api_key:
            entry["api_key"] = api_key
        profiles[provider] = entry
    return profiles


def _local_profile_for_export(engine: dict[str, Any]) -> dict[str, str]:
    return {
        "provider": str(engine.get("local_provider") or "").strip(),
        "model": str(
            engine.get("local_model") or engine.get("ollama_model") or ""
        ).strip(),
        "base_url": str(engine.get("local_base_url") or "").strip(),
    }


def _throughput_profile_for_export(settings: AppSettings, role: str) -> dict[str, Any]:
    try:
        config = resolve_effective_model_config(settings, role)
        throughput = get_model_throughput(settings, config)
    except Exception:
        return {}
    profile: dict[str, Any] = {
        "profile_key": throughput.profile_key,
        "concurrency": throughput.concurrency,
    }
    if throughput.batch_size is not None:
        profile["batch_size"] = throughput.batch_size
    return profile


def _effective_profile_for_export(
    settings: AppSettings,
    role: str,
) -> dict[str, Any]:
    """Describe the resolved configuration, never carrying a secret.

    The importer only reads keys from the ``cloud`` block, so a key here
    would be plaintext that no round-trip ever reads back.
    """
    try:
        config = resolve_effective_model_config(settings, role)
    except Exception:
        return {}
    return {
        "mode": config.mode,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "source_role": config.source_role,
        "follows": config.follows,
    }


def _parse_model_profiles(raw: dict[str, Any]) -> ImportedModelConfig:
    profiles_raw = raw.get("model_profiles")
    if not isinstance(profiles_raw, dict):
        raise ValueError("model_profiles must be a JSON object.")

    model_config: dict[str, dict[str, Any]] = {}
    scoped_api_keys: list[dict[str, str]] = []
    profile_throughputs: dict[str, dict[str, Any]] = {}

    def add_key(provider: str, base_url: str, api_key: str) -> None:
        provider = str(provider or "").strip()
        if provider not in set(CLOUD_ENGINES.values()):
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            return
        scoped_api_keys.append(
            {
                "provider": provider,
                "base_url": normalize_cloud_base_url(provider, base_url),
                "api_key": api_key,
            }
        )

    def _first_present(mapping: dict[str, Any], *keys: str) -> tuple[bool, Any]:
        for key in keys:
            if key in mapping:
                return True, mapping[key]
        return False, None

    def cloud_values(profile: dict[str, Any]) -> dict[str, Any]:
        """Extract only fields that are explicitly present in ``cloud``.

        v3 configuration imports are merging imports.  In particular, a
        sparse file which names only ``source_role`` must not clear the
        recipient's remembered provider, model, Base URL, or provider history.
        """
        cloud = profile.get("cloud")
        if not isinstance(cloud, dict):
            return {}

        values: dict[str, Any] = {}
        has_provider, raw_provider = _first_present(cloud, "provider", "cloud_provider")
        provider = str(raw_provider or "").strip() if has_provider else ""
        if has_provider:
            values["cloud_provider"] = provider

        has_model, raw_model = _first_present(cloud, "model", "cloud_model")
        if has_model:
            values["cloud_model"] = str(raw_model or "").strip()

        has_base_url, raw_base_url = _first_present(cloud, "base_url", "cloud_base_url")
        base_url = str(raw_base_url or "").strip() if has_base_url else ""
        if has_base_url:
            values["cloud_base_url"] = (
                normalize_cloud_base_url(provider, base_url) if provider else base_url
            )

        has_api_key, raw_api_key = _first_present(cloud, "api_key")
        if has_api_key:
            add_key(provider, base_url, str(raw_api_key or ""))

        has_provider_configs, configs_raw = _first_present(
            cloud,
            "provider_configs",
            "cloud_provider_configs",
        )
        if has_provider_configs and isinstance(configs_raw, dict):
            provider_configs: dict[str, dict[str, str]] = {}
            for raw_provider, raw_config in configs_raw.items():
                config_provider = str(raw_provider or "").strip()
                if (
                    config_provider not in set(CLOUD_ENGINES.values())
                    or not isinstance(raw_config, dict)
                ):
                    continue
                entry: dict[str, str] = {}
                has_config_model, raw_config_model = _first_present(
                    raw_config,
                    "model",
                    "cloud_model",
                )
                if has_config_model:
                    entry["cloud_model"] = str(raw_config_model or "").strip()
                has_config_base, raw_config_base = _first_present(
                    raw_config,
                    "base_url",
                    "cloud_base_url",
                )
                config_base_url = str(raw_config_base or "").strip()
                if has_config_base:
                    entry["cloud_base_url"] = normalize_cloud_base_url(
                        config_provider,
                        config_base_url,
                    )
                has_config_key, raw_config_key = _first_present(raw_config, "api_key")
                if has_config_key:
                    add_key(config_provider, config_base_url, str(raw_config_key or ""))
                if entry:
                    provider_configs[config_provider] = entry
            if provider_configs:
                values["cloud_provider_configs"] = provider_configs
        return values

    def connection_values(profile: dict[str, Any]) -> list[dict[str, str]] | None:
        """Read a pool back, or ``None`` when the file does not describe one.

        Files written before pools were exported have no ``connections`` key at
        all; those must leave the reader's own pool alone rather than collapse
        it to a single entry.
        """
        raw_connections = profile.get("connections")
        if not isinstance(raw_connections, list):
            return None
        entries: list[dict[str, str]] = []
        for raw in raw_connections:
            if not isinstance(raw, dict):
                continue
            provider = str(raw.get("provider") or "").strip()
            base_url = str(raw.get("base_url") or "").strip()
            if "api_key" in raw:
                add_key(provider, base_url, str(raw.get("api_key") or ""))
            entries.append(
                {
                    "label": str(raw.get("label") or "").strip(),
                    "provider": provider,
                    "model": str(raw.get("model") or "").strip(),
                    "base_url": (
                        normalize_cloud_base_url(provider, base_url)
                        if provider
                        else base_url
                    ),
                }
            )
        return entries or None

    for profile_key, profile in profiles_raw.items():
        normalized_key = str(profile_key or "").strip()
        role = MODEL_PROFILE_ROLE_BY_KEY.get(normalized_key)
        if role is None and normalized_key == ROLE_IMAGE:
            role = ROLE_IMAGE
        if role is None or not isinstance(profile, dict):
            continue

        setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
        # 池里的连接先读，``cloud`` 块后读：文件不带 id，密钥只能落在
        # provider + Base URL 作用域下，同作用域后写的覆盖先写的。备用连接和主用
        # 连接指向同一台服务器时（同一家服务的两个账号），备用那把 Key 会盖掉主用
        # 的——「导出含 Key 再导入」之后主用连接拿着别人的账号去拨。``cloud`` 块
        # 描述的就是主用连接，让它最后落地。
        connections = connection_values(profile)
        values = cloud_values(profile)
        if connections is not None:
            # A pool is a list, so this replaces rather than merges: the file
            # describes a complete pool, and appending would leave the reader
            # with both machines' connections mixed together.
            values["connections"] = connections
        # All four roles export mode/source_role/local, so all four have to read
        # them back.  Reading them per role is what keeps a round-trip lossless
        # now that translation can follow and the cleaner can run locally.
        if "mode" in profile:
            mode = str(profile.get("mode") or "").strip()
            if mode not in {"cloud", "local"}:
                raise ValueError(f"{role} mode must be 'cloud' or 'local'.")
            values["mode"] = mode
        local = profile.get("local")
        if isinstance(local, dict):
            has_local_provider, raw_local_provider = _first_present(local, "provider")
            if has_local_provider:
                values["local_provider"] = str(raw_local_provider or "").strip()
            has_local_model, raw_local_model = _first_present(local, "model")
            if has_local_model:
                values["local_model"] = str(raw_local_model or "").strip()
                if role == ROLE_TRANSLATION:
                    # Only the engine carries the pre-rename Ollama field.
                    values["ollama_model"] = values["local_model"]
            has_local_base, raw_local_base = _first_present(local, "base_url")
            if has_local_base:
                values["local_base_url"] = str(raw_local_base or "").strip()
        if "source_role" in profile:
            source_role = str(profile.get("source_role") or "").strip()
            values["source_role"] = (
                ROLE_IMAGE if source_role == "pdf_translation" else source_role
            )
        if values:
            model_config[setting_key] = values

        throughput = profile.get("throughput")
        if isinstance(throughput, dict):
            profile_throughputs[role] = throughput

    if not model_config and not profile_throughputs and not scoped_api_keys:
        raise ValueError("No model configuration was found in the import.")
    return ImportedModelConfig(
        model_config=model_config,
        api_keys={},
        scoped_api_keys=scoped_api_keys,
        throughput_profiles={},
        profile_throughputs=profile_throughputs,
    )


def _parse_throughput_profiles(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = raw.get("model_throughput_profiles", {})
    if profiles in (None, ""):
        return {}
    if not isinstance(profiles, dict):
        raise ValueError("model_throughput_profiles must be a JSON object.")
    return {
        str(key): value
        for key, value in profiles.items()
        if str(key).strip() and isinstance(value, dict)
    }


def _parse_scoped_api_keys(raw: dict[str, Any]) -> list[dict[str, str]]:
    scoped_raw = raw.get("scoped_api_keys", [])
    if scoped_raw in (None, ""):
        scoped_raw = []
    cloud_providers = set(CLOUD_ENGINES.values())
    entries: list[dict[str, str]] = []

    def add_entry(provider: str, base_url: str, api_key: str) -> None:
        provider = str(provider or "").strip()
        if provider not in cloud_providers:
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            return
        entries.append(
            {
                "provider": provider,
                "base_url": normalize_cloud_base_url(provider, base_url),
                "api_key": api_key,
            }
        )

    if isinstance(scoped_raw, list):
        for entry in scoped_raw:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider") or "").strip()
            base_url = str(entry.get("base_url") or "").strip()
            if not provider:
                provider, parsed_base_url = parse_api_key_scope(entry.get("scope", ""))
                base_url = base_url or parsed_base_url
            add_entry(provider, base_url, str(entry.get("api_key") or ""))
        return entries

    if isinstance(scoped_raw, dict):
        for scope, value in scoped_raw.items():
            provider, base_url = parse_api_key_scope(scope)
            if isinstance(value, dict):
                provider = str(value.get("provider") or provider).strip()
                base_url = str(value.get("base_url") or base_url).strip()
                api_key = str(value.get("api_key") or "").strip()
            else:
                api_key = str(value or "").strip()
            add_entry(provider, base_url, api_key)
        return entries

    raise ValueError("scoped_api_keys must be an array or JSON object.")
