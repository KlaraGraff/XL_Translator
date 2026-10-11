"""Export and import of the model configuration and the document configuration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException

from core.document_config import (
    apply_document_config_import,
    build_document_config_export_payload,
    parse_document_config_import,
    summarize_document_config_import,
)
from core.config_crypto import (
    DEFAULT_VALID_DAYS,
    UNSEAL_CORRUPT,
    UNSEAL_PLAINTEXT,
    count_api_keys,
    seal_model_config_document,
    unseal_model_config_document,
)
from core.model_config import (
    API_KEY_EXPORT_INCLUDED,
    API_KEY_EXPORT_KIND_CONNECTION,
    API_KEY_EXPORT_KIND_PROVIDER_MEMORY,
    API_KEY_EXPORT_MISSING,
    API_KEY_EXPORT_WITHHELD_IMPORTED,
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
)
from settings import (
    carry_settings_baseline,
    load_settings,
    save_settings,
)


def register(app: FastAPI) -> None:
    """Register the config transfer routes on ``app``."""

    @app.get("/api/model-config/export")
    def export_model_config(
        include_api_key: bool = False,
        include_api_keys: bool | None = None,
        confirm_sensitive: bool = False,
        valid_days: int = DEFAULT_VALID_DAYS,
    ) -> dict[str, Any]:
        """``valid_days`` 传 0 表示长期有效；只在导出含 Key 时有意义。"""
        if include_api_keys is not None:
            include_api_key = bool(include_api_keys)
        if include_api_key and not confirm_sensitive:
            raise HTTPException(422, "导出 API 密钥前，请先确认要一起导出这些敏感信息。")
        if valid_days < 0 or valid_days > 3650:
            raise HTTPException(422, "文件有效期只能在 0（长期有效）到 3650 天之间。")
        # 回执和导出文档分成两个字段：``document`` 是原样写盘的那份，格式一个字节都
        # 没变（导入侧不受影响）；``api_key_report`` 只给界面看，绝不能混进文件里，
        # 否则收到文件的人还附送一份本机连接清单。
        report_rows: list[dict[str, str]] = []
        document = build_model_config_export_payload(
            load_settings(),
            include_api_key=include_api_key,
            api_key_report=report_rows if include_api_key else None,
        )
        # 回执分两拨：``connections`` 是界面上看得见、能一条条对照的连接；
        # ``provider_memories`` 是角色记住的其他服务商配置，只在密钥被扣下时才有行，
        # 否则界面会出现「文件里少了一把 Key，可回执里一个字都没提」。
        connection_rows = [
            row
            for row in report_rows
            if row.get("kind", API_KEY_EXPORT_KIND_CONNECTION) == API_KEY_EXPORT_KIND_CONNECTION
        ]
        provider_memory_rows = [
            row
            for row in report_rows
            if row.get("kind") == API_KEY_EXPORT_KIND_PROVIDER_MEMORY
        ]
        # 加密只在这一层做，不进 ``build_model_config_export_payload``：那个函数同时被
        # 原生 Qt 界面和测试当作「配置的可移植表示」在用，一旦它自己返回密文，任何
        # 想检查导出内容的调用方都得先解密。封装是写盘前的最后一道工序，就放在这里。
        sealed_document = (
            seal_model_config_document(
                document, valid_days=valid_days if valid_days > 0 else None
            )
            if include_api_key
            else document
        )
        seal_info = sealed_document.get("sealed_keys") or {}
        return {
            "document": sealed_document,
            "sealed": bool(seal_info),
            "expires_at": seal_info.get("expires_at"),
            # 回执上「带走了几把密钥」必须和收件人导入时看到的作用域数一致，所以在
            # 密封前数原文，而不是数回执里的连接行——那样会漏掉角色记住的服务商配置。
            "sealed_key_count": count_api_keys(document) if include_api_key else 0,
            "api_key_report": {
                "include_api_key": include_api_key,
                "connections": connection_rows,
                "provider_memories": provider_memory_rows,
                "exported_count": sum(
                    1
                    for row in connection_rows
                    if row["status"] == API_KEY_EXPORT_INCLUDED
                ),
                "withheld_count": sum(
                    1
                    for row in connection_rows
                    if row["status"] == API_KEY_EXPORT_WITHHELD_IMPORTED
                ),
                "missing_count": sum(
                    1
                    for row in connection_rows
                    if row["status"] == API_KEY_EXPORT_MISSING
                ),
                "withheld_provider_memory_count": len(provider_memory_rows),
            },
        }

    @app.post("/api/model-config/import/preview")
    def preview_model_config_import(payload: dict[str, Any]) -> dict[str, Any]:
        unsealed = _unseal_or_422(payload)
        try:
            imported = parse_model_config_import(unsealed.document)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        role_names = {
            "engine": ROLE_TRANSLATION,
            "cleaner_model_role": ROLE_CLEANER,
            "image_model_role": ROLE_IMAGE,
            "pdf_review_model_role": ROLE_PDF_REVIEW,
        }
        return {
            "version": 3,
            "roles": [
                {
                    "role": role_names.get(setting_key, setting_key),
                    "fields": sorted(values),
                }
                for setting_key, values in imported.model_config.items()
            ],
            "throughput_profile_count": len(imported.profile_throughputs)
            + len(imported.throughput_profiles),
            "api_key_count": len(imported.api_keys) + len(imported.scoped_api_keys),
            **_seal_state_payload(unsealed),
        }

    @app.post("/api/model-config/import")
    def import_model_config(payload: dict[str, Any]) -> dict[str, Any]:
        throughput_errors: list[str] = []
        key_writes: list[Callable[[], None]] = []
        before_settings = load_settings()
        unsealed = _unseal_or_422(payload)
        try:
            imported = parse_model_config_import(unsealed.document)
            settings = apply_model_config_import(
                before_settings,
                imported,
                throughput_errors=throughput_errors,
                defer_key_writes=key_writes,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # ``apply_model_config_import`` returns a rebuilt model; keep the
        # load-time snapshot so the save stays a merge, not an overwrite.
        carry_settings_baseline(before_settings, settings)
        save_settings(settings)
        # 密钥留到设置落盘之后再写：两者是两个文件。先写密钥的话，设置一旦存不下去
        # （磁盘满、权限不对、被别的进程占着），界面报的是「导入失败」，而密钥文件
        # 里已经躺着一份没有任何配置指向的凭据——下次谁用到那个作用域就用错账号。
        for commit_keys in key_writes:
            commit_keys()
        return {
            "settings": settings.model_dump(mode="json"),
            "imported_key_count": len(imported.api_keys) + len(imported.scoped_api_keys),
            # Roles whose imported batch_size/concurrency could not be applied;
            # the rest of the import still succeeded.
            "skipped_throughput_roles": throughput_errors,
            **_seal_state_payload(unsealed),
        }

    @app.get("/api/document-config/export")
    def export_document_config() -> dict[str, Any]:
        """Export every document-translation setting as one bundle.

        There is no with-keys variant and no per-page variant: this bundle
        carries no secrets, and splitting it per page is exactly the friction
        the two-bundle design removes.
        """
        return build_document_config_export_payload(load_settings())

    @app.post("/api/document-config/import/preview")
    def preview_document_config_import(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            imported = parse_document_config_import(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "version": payload.get("version"),
            "app_version": str(payload.get("app_version") or ""),
            "areas": summarize_document_config_import(imported),
        }

    @app.post("/api/document-config/import")
    def import_document_config(payload: dict[str, Any]) -> dict[str, Any]:
        before_settings = load_settings()
        try:
            imported = parse_document_config_import(payload)
            settings = apply_document_config_import(before_settings, imported)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # ``apply_document_config_import`` rebuilds the model, so the load-time
        # snapshot has to be carried over or the save stops being a merge.
        carry_settings_baseline(before_settings, settings)
        save_settings(settings)
        return {
            "settings": settings.model_dump(mode="json"),
            "areas": summarize_document_config_import(imported),
        }


# 界面靠这句原话认出「文件被改过」这条分支（ui/src/views/settings.ts 里同名常量），
# 好弹出专门的弹窗而不是一条通用红色 toast。改动这句话必须两边一起改——
# tests/test_config_crypto.py 会比对两个文件里的字面量，改单边会红。
CORRUPT_IMPORT_DETAIL = (
    "这份文件在传输过程中损坏，或者被修改过，没有导入任何内容。请让发送方重新发一次。"
)


def _unseal_or_422(payload: dict[str, Any]):
    """解开导入文件的密钥段；只有「不可信」这一种情况在这里直接拒绝。

    过期、软件太旧、旧版明文这三种都还能往下走（配置照常导入、密钥留空），所以
    它们不在这里报错，而是把状态带给界面去说人话。校验失败不一样：AAD 锁着配置
    正文，校验不过说明正文也不可信——这时候放行等于让对方导入一份可能被人改过
    base_url 的配置，密钥一填进去就发到别人服务器上了。
    """
    result = unseal_model_config_document(payload)
    if result.status == UNSEAL_CORRUPT:
        raise HTTPException(422, CORRUPT_IMPORT_DETAIL)
    return result


def _seal_state_payload(result) -> dict[str, Any]:
    """界面用来区分五种导入结果的那几个字段。"""
    return {
        "seal_status": result.status,
        "sealed": result.sealed,
        "expires_at": result.expires_at,
        "sealed_key_count": result.key_count,
        # 旧版明文文件要单独标出来：界面得提醒对方这份文件本身不安全、用完删掉。
        "legacy_plaintext": result.status == UNSEAL_PLAINTEXT,
    }
