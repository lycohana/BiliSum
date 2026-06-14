import os
import re
import threading

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from video_sum_core.models.tasks import TaskStatus
from video_sum_infra.prompt_library import (
    BUILTIN_PRESETS,
    DEFAULT_PRESET_ID,
    PromptPreset,
    delete_custom_preset,
    load_presets,
    match_preset,
    save_custom_preset,
)
from video_sum_infra.runtime import (
    activate_runtime_pythonpath,
    bootstrap_managed_runtime,
    log_dir,
    prepend_runtime_path,
    service_log_path,
)

from video_sum_service.auth import (
    describe_token_source,
    extract_bearer_token,
    request_is_authorized,
    set_session_cookie,
)
from video_sum_service.bilibili_cookies import (
    capture_bilibili_cookies_from_browser,
    create_bilibili_login_qrcode,
    poll_bilibili_login_qrcode,
)
from video_sum_service.context import access_token_manager, app_info, logger, settings_manager
from video_sum_service.integrations import (
    extract_http_error_detail,
    probe_asr_connection,
    probe_llm_connection,
    read_log_tail,
)
from video_sum_service.runtime_startup import get_runtime_startup_state, mark_runtime_worker_ready
from video_sum_service.runtime_support import (
    _load_cached_environment_probe,
    build_worker,
    check_package_dependencies,
    clear_environment_probe_cache,
    detect_environment,
    download_embedding_model,
    ensure_runtime_channel,
    get_embedding_model_presets,
    get_knowledge_requirements,
    inspect_runtime_channels,
    install_cuda_support,
    install_funasr,
    install_knowledge_dependencies,
    install_local_asr,
    read_install_log,
    normalize_runtime_channel,
    replace_task_worker,
    serialize_settings,
    sync_all_runtime_channels,
    sync_runtime_channel,
    uninstall_packages,
    verify_embedding_model,
)
from video_sum_service.schemas import (
    PromptMatchRequest,
    PromptMatchResponse,
    PromptPresetCreateRequest,
    PromptPresetResponse,
)
from video_sum_service.settings_manager import SettingsUpdatePayload

router = APIRouter(prefix="/api/v1")
LATEST_RELEASE_URL = "https://api.github.com/repos/lycohana/BiliSum/releases/latest"
BUILTIN_PROMPT_PRESET_IDS = {preset.id for preset in BUILTIN_PRESETS}


def _clear_knowledge_service_cache(app_state) -> None:
    for key in (
        "knowledge_tag_service",
        "knowledge_index_service",
        "knowledge_rag_service",
        "knowledge_settings_signature",
    ):
        if hasattr(app_state, key):
            delattr(app_state, key)


def _rebuild_worker_after_save(
    app_state,
    current_settings: "ServiceSettings",
    active_runtime_channel: str,
) -> None:
    """Rebuild task worker in background after settings save."""
    environment = detect_environment(active_runtime_channel)
    replace_task_worker(
        app_state,
        build_worker(
            app_state.task_repository,
            current_settings,
            environment_info=environment,
        ),
    )
    mark_runtime_worker_ready(
        app_state,
        environment,
        "Runtime worker refreshed after settings update.",
    )


def _load_cached_environment_snapshot(runtime_channel: str) -> dict[str, object]:
    """Return a cached environment probe without triggering a fresh subprocess run.

    When the cache is cold, returns a minimal stub so serialize_settings
    doesn't fall back to detect_environment() and block the response.
    The background task will rebuild the worker with real env data shortly.
    """
    cached = _load_cached_environment_probe(runtime_channel)
    if cached is not None:
        return dict(cached)
    return {"cudaAvailable": False}


def _prompt_preset_response(preset: PromptPreset) -> PromptPresetResponse:
    return PromptPresetResponse(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        category=preset.category,
        system_prompt=preset.system_prompt,
        user_prompt_template=preset.user_prompt_template,
        auto_match_keywords=preset.auto_match_keywords,
        is_builtin=preset.id in BUILTIN_PROMPT_PRESET_IDS,
    )


def _load_prompt_presets() -> list[PromptPreset]:
    current_settings = settings_manager.current
    return load_presets(
        data_dir=current_settings.data_dir,
        prompt_presets_path=current_settings.prompt_presets_path,
    )


def _normalize_custom_prompt_preset_id(name: str) -> str:
    normalized_id = re.sub(
        r"[^\w]+",
        "_",
        str(name or "").strip().lower(),
        flags=re.UNICODE,
    ).strip("_")
    if not normalized_id:
        raise HTTPException(status_code=400, detail="预设名称不能为空。")
    return normalized_id


def _prompt_match_metadata(title: str, preset: PromptPreset) -> tuple[str, float]:
    normalized_title = str(title or "").strip().lower()
    if not normalized_title or preset.id == DEFAULT_PRESET_ID:
        return "fallback", 0.4
    for keyword in preset.auto_match_keywords:
        normalized_keyword = str(keyword or "").strip().lower()
        if normalized_keyword and normalized_keyword in normalized_title:
            return "keyword", 0.9
    return "fallback", 0.4


def _normalize_version(value: str | None) -> str:
    return str(value or "").strip().removeprefix("v").removeprefix("V")


def _version_key(value: str | None) -> tuple[int | str, ...]:
    normalized = _normalize_version(value)
    if not normalized:
        return (0,)

    parts: list[int | str] = []
    for chunk in re.split(r"[.\-+_]", normalized):
        if not chunk:
            continue
        parts.append(int(chunk) if chunk.isdigit() else chunk.lower())
    return tuple(parts) or (0,)


@router.get("/prompts/presets", response_model=list[PromptPresetResponse])
def list_prompt_presets() -> list[PromptPresetResponse]:
    return [_prompt_preset_response(preset) for preset in _load_prompt_presets()]


@router.post("/prompts/presets", response_model=PromptPresetResponse)
def save_prompt_preset(payload: PromptPresetCreateRequest) -> PromptPresetResponse:
    preset_id = _normalize_custom_prompt_preset_id(payload.name)
    if preset_id in BUILTIN_PROMPT_PRESET_IDS:
        raise HTTPException(status_code=400, detail="内置预设不能被覆盖。")

    preset = PromptPreset(
        id=preset_id,
        name=payload.name.strip(),
        description=str(payload.description or "").strip(),
        category=str(payload.category or "custom").strip() or "custom",
        system_prompt=payload.system_prompt.strip(),
        user_prompt_template=payload.user_prompt_template.strip(),
        auto_match_keywords=payload.auto_match_keywords,
    )
    if not preset.system_prompt or not preset.user_prompt_template:
        raise HTTPException(status_code=400, detail="System prompt 和 User prompt 不能为空。")

    current_settings = settings_manager.current
    save_custom_preset(
        preset,
        data_dir=current_settings.data_dir,
        prompt_presets_path=current_settings.prompt_presets_path,
    )
    return _prompt_preset_response(preset)


@router.delete("/prompts/presets/{preset_id}")
def delete_prompt_preset(preset_id: str) -> dict[str, object]:
    normalized_id = str(preset_id or "").strip()
    if normalized_id in BUILTIN_PROMPT_PRESET_IDS:
        raise HTTPException(status_code=400, detail="内置预设不能删除。")

    current_settings = settings_manager.current
    delete_custom_preset(
        normalized_id,
        data_dir=current_settings.data_dir,
        prompt_presets_path=current_settings.prompt_presets_path,
    )
    return {"deleted": True, "preset_id": normalized_id}


@router.post("/prompts/match", response_model=PromptMatchResponse)
def match_prompt_preset(payload: PromptMatchRequest) -> PromptMatchResponse:
    presets = _load_prompt_presets()
    preset = match_preset(payload.title, presets=presets)
    match_type, confidence = _prompt_match_metadata(payload.title, preset)
    return PromptMatchResponse(
        preset=_prompt_preset_response(preset),
        match_type=match_type,
        confidence=confidence,
    )


@router.get("/system/info")
def system_info(request: Request, runtime_channel: str | None = None, refresh: bool = False) -> dict[str, object]:
    current_settings = settings_manager.current
    active_channel = normalize_runtime_channel(
        runtime_channel or current_settings.runtime_channel,
        allow_unknown_gpu=True,
    )
    runtime_startup = get_runtime_startup_state(request.app.state)
    should_probe_environment = refresh or runtime_channel is not None
    if refresh:
        clear_environment_probe_cache(active_channel)
    environment = (
        detect_environment(active_channel)
        if should_probe_environment
        else dict(runtime_startup.get("environment") or {})
    )
    runtime_settings = current_settings.with_resolved_runtime(
        cuda_available=bool(environment.get("cudaAvailable"))
    )
    worker = getattr(request.app.state, "task_worker", None)
    worker_snapshot = worker.snapshot() if hasattr(worker, "snapshot") else None
    return {
        "application": {"name": app_info.name, "version": app_info.version},
        "service": {
            "host": current_settings.host,
            "port": current_settings.port,
            "data_dir": str(current_settings.data_dir),
            "cache_dir": str(current_settings.cache_dir),
            "tasks_dir": str(current_settings.tasks_dir),
            "database_url": current_settings.database_url,
            "log_dir": str(log_dir()),
            "log_file": str(service_log_path()),
        },
        "runtime": {
            "runtime_channel": active_channel,
            "whisper_model": runtime_settings.whisper_model,
            "whisper_device": runtime_settings.whisper_device,
            "whisper_compute_type": runtime_settings.whisper_compute_type,
            "llm_enabled": current_settings.llm_enabled,
            "llm_model": current_settings.llm_model,
        },
        "runtimeStartup": runtime_startup,
        "process": {
            "thread_count": threading.active_count(),
            "threads": [thread.name for thread in threading.enumerate()[:80]],
        },
        "worker": worker_snapshot,
        "taskModel": {"statuses": [status.value for status in TaskStatus]},
        "environment": environment,
    }


@router.get("/settings")
def get_settings(request: Request) -> dict[str, object]:
    runtime_startup = get_runtime_startup_state(request.app.state)
    environment = dict(runtime_startup.get("environment") or {})
    return serialize_settings(settings_manager.current, environment_info=environment)


@router.get("/auth/status")
def get_auth_status(request: Request) -> dict[str, object]:
    return {
        **describe_token_source(access_token_manager),
        "authenticated": request_is_authorized(request, access_token_manager),
    }


@router.post("/auth/session")
def create_auth_session(request: Request) -> JSONResponse:
    token = extract_bearer_token(request)
    if not access_token_manager.verify(token):
        raise HTTPException(status_code=401, detail="访问密钥无效。")
    response = JSONResponse(
        {
            "authenticated": True,
            **describe_token_source(access_token_manager),
        }
    )
    set_session_cookie(response, token or "")
    return response


@router.get("/app/update")
def get_app_update() -> dict[str, object]:
    import httpx

    current_version = app_info.version
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{app_info.name}/{current_version}",
    }

    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            response = client.get(LATEST_RELEASE_URL)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"获取最新版本信息失败：{exc}") from exc

    if response.status_code >= 400:
        detail = extract_http_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=f"获取最新版本信息失败：{detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="获取最新版本信息失败：响应格式无效。") from exc

    latest_version = _normalize_version(payload.get("tag_name") or payload.get("name") or current_version)
    if not latest_version:
        raise HTTPException(status_code=502, detail="获取最新版本信息失败：缺少版本号。")

    current_key = _version_key(current_version)
    latest_key = _version_key(latest_version)
    is_newer_available = latest_key > current_key

    return {
        "status": "available" if is_newer_available else "not-available",
        "version": latest_version if is_newer_available else _normalize_version(current_version),
        "releaseDate": payload.get("published_at") or payload.get("created_at") or "",
        "releaseNotes": str(payload.get("body") or "").strip() or None,
        "downloadProgress": 0,
        "errorMessage": None,
    }


@router.put("/settings")
def update_settings(payload: SettingsUpdatePayload, request: Request, background_tasks: BackgroundTasks) -> dict[str, object]:
    previous_settings = settings_manager.current
    requested_runtime_channel = normalize_runtime_channel(
        payload.runtime_channel or previous_settings.runtime_channel,
        allow_unknown_gpu=True,
    )
    if payload.runtime_channel is not None:
        payload = payload.model_copy(update={"runtime_channel": requested_runtime_channel})
    ensure_runtime_channel(requested_runtime_channel)

    current_settings = settings_manager.save(payload)
    active_runtime_channel = normalize_runtime_channel(current_settings.runtime_channel, allow_unknown_gpu=True)
    bootstrap_managed_runtime(active_runtime_channel)
    prepend_runtime_path(active_runtime_channel)
    activate_runtime_pythonpath(active_runtime_channel)
    current_settings.data_dir.mkdir(parents=True, exist_ok=True)
    current_settings.cache_dir.mkdir(parents=True, exist_ok=True)
    current_settings.tasks_dir.mkdir(parents=True, exist_ok=True)
    runtime_channel_changed = previous_settings.runtime_channel != current_settings.runtime_channel
    if runtime_channel_changed:
        clear_environment_probe_cache(previous_settings.runtime_channel)
        clear_environment_probe_cache(current_settings.runtime_channel)
        _clear_knowledge_service_cache(request.app.state)
    # Defer worker rebuild + env probe to background so the save response
    # returns immediately instead of blocking on subprocess / thread init.
    background_tasks.add_task(
        _rebuild_worker_after_save,
        request.app.state,
        current_settings,
        active_runtime_channel,
    )
    # Serialize with a fast environment snapshot (use cached probe if available).
    environment_snapshot = _load_cached_environment_snapshot(active_runtime_channel)
    return {
        "saved": True,
        "settings": serialize_settings(current_settings, environment_info=environment_snapshot),
        "message": "设置已保存。涉及服务监听地址的修改将在下次启动后生效。",
    }


@router.post("/llm/test")
def post_llm_test(payload: SettingsUpdatePayload | None = None) -> dict[str, object]:
    return probe_llm_connection(payload)


@router.post("/asr/test")
def post_asr_test(payload: SettingsUpdatePayload | None = None) -> dict[str, object]:
    return probe_asr_connection(payload)


@router.post("/bilibili/cookies/capture")
def post_bilibili_cookies_capture() -> dict[str, object]:
    try:
        return capture_bilibili_cookies_from_browser()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bilibili/cookies/qrcode")
def post_bilibili_cookies_qrcode() -> dict[str, object]:
    try:
        return create_bilibili_login_qrcode()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/bilibili/cookies/qrcode/{qrcode_key}")
def get_bilibili_cookies_qrcode(qrcode_key: str) -> dict[str, object]:
    try:
        return poll_bilibili_login_qrcode(qrcode_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/environment")
def get_environment(runtime_channel: str | None = None, refresh: bool = False) -> dict[str, object]:
    active_channel = normalize_runtime_channel(
        runtime_channel or settings_manager.current.runtime_channel,
        allow_unknown_gpu=True,
    )
    if refresh:
        clear_environment_probe_cache(active_channel)
    return detect_environment(active_channel)


@router.get("/runtime/status")
def get_runtime_status() -> dict[str, object]:
    return inspect_runtime_channels()


@router.post("/runtime/sync")
def post_runtime_sync(request: Request, payload: dict[str, object] | None = None) -> dict[str, object]:
    requested_raw = str((payload or {}).get("runtime_channel") or (payload or {}).get("runtimeChannel") or "").strip()
    requested_channel = normalize_runtime_channel(requested_raw, allow_unknown_gpu=True) if requested_raw else ""
    try:
        result = sync_runtime_channel(requested_channel) if requested_channel else sync_all_runtime_channels()
        _clear_knowledge_service_cache(request.app.state)

        current_settings = settings_manager.current
        environment = detect_environment(current_settings.runtime_channel)
        replace_task_worker(
            request.app.state,
            build_worker(
                request.app.state.task_repository,
                current_settings,
                environment_info=environment,
            ),
        )
        mark_runtime_worker_ready(
            request.app.state,
            environment,
            "Runtime worker refreshed after runtime sync.",
        )
        return {
            **result,
            "environment": environment,
            "runtimeStatus": inspect_runtime_channels(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("runtime sync failed requested_channel=%s error=%s", requested_channel or "all", exc)
        raise HTTPException(status_code=500, detail=f"运行环境同步失败：{exc}") from exc


@router.get("/system/logs")
def get_system_logs(lines: int = 200) -> dict[str, object]:
    line_count = max(20, min(int(lines), 1000))
    return {
        "path": str(service_log_path()),
        "lines": line_count,
        "content": read_log_tail(line_count),
    }


@router.post("/system/shutdown")
def shutdown_service(request: Request) -> dict[str, object]:
    def shutdown() -> None:
        task_worker = getattr(request.app.state, "task_worker", None)
        if task_worker is not None:
            task_worker.shutdown(wait=False)
        os._exit(0)

    threading.Timer(0.5, shutdown).start()
    return {"shuttingDown": True, "message": "服务正在关闭。"}


@router.post("/cuda/install")
def post_cuda_install(payload: dict[str, object], request: Request) -> dict[str, object]:
    requested_variant = payload.get("cuda_variant", payload.get("cudaVariant", "cu128"))
    session_id = str(payload.get("installSessionId") or "") or None
    result, worker = install_cuda_support(str(requested_variant), request.app.state.task_repository, session_id=session_id)
    _clear_knowledge_service_cache(request.app.state)
    replace_task_worker(request.app.state, worker)
    if isinstance(result.get("environment"), dict):
        mark_runtime_worker_ready(
            request.app.state,
            result["environment"],
            "Runtime worker refreshed after CUDA install.",
        )
    return result


@router.post("/asr/local/install")
def post_local_asr_install(request: Request, payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    reinstall = bool(payload.get("reinstall"))
    session_id = str(payload.get("installSessionId") or "") or None
    result, worker = install_local_asr(reinstall=reinstall, repository=request.app.state.task_repository, session_id=session_id)
    replace_task_worker(request.app.state, worker)
    if isinstance(result.get("environment"), dict):
        mark_runtime_worker_ready(
            request.app.state,
            result["environment"],
            "Runtime worker refreshed after ASR install.",
        )
    return result


@router.post("/asr/funasr/install")
def post_funasr_install(request: Request, payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    reinstall = bool(payload.get("reinstall"))
    session_id = str(payload.get("installSessionId") or "") or None
    result, worker = install_funasr(reinstall=reinstall, repository=request.app.state.task_repository, session_id=session_id)
    replace_task_worker(request.app.state, worker)
    if isinstance(result.get("environment"), dict):
        mark_runtime_worker_ready(
            request.app.state,
            result["environment"],
            "Runtime worker refreshed after FunASR install.",
        )
    return result


@router.get("/asr/install-log")
def get_install_log(session_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        raise HTTPException(status_code=400, detail="Invalid session id.")
    return read_install_log(session_id)


@router.post("/knowledge/install")
def post_knowledge_install(request: Request, payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    reinstall = bool(payload.get("reinstall"))
    runtime_channel_raw = str(payload.get("runtime_channel") or payload.get("runtimeChannel") or "").strip()
    runtime_channel = normalize_runtime_channel(runtime_channel_raw, allow_unknown_gpu=True) if runtime_channel_raw else None
    provider = str(payload.get("provider") or "").strip() or None
    session_id = str(payload.get("installSessionId") or "") or None
    result, worker = install_knowledge_dependencies(
        reinstall=reinstall,
        repository=request.app.state.task_repository,
        runtime_channel=runtime_channel,
        provider=provider,
        session_id=session_id,
    )
    if worker is not None:
        _clear_knowledge_service_cache(request.app.state)
        replace_task_worker(request.app.state, worker)
    if worker is not None and isinstance(result.get("environment"), dict):
        mark_runtime_worker_ready(
            request.app.state,
            result["environment"],
            "Runtime worker refreshed after knowledge install.",
        )
    return result


@router.post("/knowledge/embedding/download")
def post_embedding_download(payload: dict[str, object]) -> dict[str, object]:
    provider = str(payload.get("provider") or "local_huggingface")
    model_name = str(payload.get("model") or "BAAI/bge-small-zh-v1.5")
    hf_endpoint = str(payload.get("hf_endpoint") or "")
    session_id = str(payload.get("installSessionId") or "") or None
    return download_embedding_model(
        None,  # type: ignore
        provider=provider,
        model_name=model_name,
        hf_endpoint=hf_endpoint,
        session_id=session_id,
    )


@router.post("/knowledge/embedding/test")
def post_embedding_test(payload: dict[str, object], request: Request) -> dict[str, object]:
    provider = str(payload.get("provider") or "local_huggingface")
    model_name = str(payload.get("model") or "BAAI/bge-small-zh-v1.5")
    hf_endpoint = str(payload.get("hf_endpoint") or "")
    api_key = str(payload.get("api_key") or "")
    base_url = str(payload.get("base_url") or "")
    return verify_embedding_model(
        request.app.state.task_repository,
        provider=provider,
        model_name=model_name,
        hf_endpoint=hf_endpoint,
        api_key=api_key,
        base_url=base_url,
    )


@router.get("/knowledge/embedding/presets")
def get_embedding_presets() -> dict[str, object]:
    return {"presets": get_embedding_model_presets()}


@router.get("/runtime/knowledge-requirements")
def get_runtime_knowledge_requirements(provider: str = "local_huggingface") -> dict[str, object]:
    """Get required packages based on embedding provider."""
    return get_knowledge_requirements(provider)


@router.get("/runtime/package-dependencies")
def get_runtime_package_dependencies(package: str) -> dict[str, object]:
    """Check what features depend on this package."""
    return {"package": package, "dependencies": check_package_dependencies(package)}


@router.post("/runtime/uninstall-packages")
def post_runtime_uninstall_packages(payload: dict[str, object]) -> dict[str, object]:
    """Uninstall specified packages from runtime."""
    packages = payload.get("packages", [])
    if not isinstance(packages, list) or not packages:
        raise HTTPException(status_code=400, detail="packages 参数必须是非空列表")

    runtime_channel_raw = str(payload.get("runtime_channel") or payload.get("runtimeChannel") or "").strip()
    runtime_channel = (
        normalize_runtime_channel(runtime_channel_raw, allow_unknown_gpu=True)
        if runtime_channel_raw
        else None
    )
    return uninstall_packages(packages, runtime_channel)
