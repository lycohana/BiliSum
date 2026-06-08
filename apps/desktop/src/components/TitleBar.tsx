import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import type { ConfigHealth, UpdateState } from "../appModel";

export function TitleBar({
  darkMode,
  onToggleTheme,
  serviceOnline,
  backendRunning,
  runtimeDeviceLabel,
  runtimeReady,
  runtimeStatusText,
  version,
  updateState,
  configHealth,
  onOpenSettings,
  onOpenUpdateDialog,
}: {
  darkMode: boolean;
  onToggleTheme(): void;
  serviceOnline: boolean;
  backendRunning?: boolean;
  runtimeDeviceLabel: string;
  runtimeReady: boolean;
  runtimeStatusText: string;
  version: string;
  updateState: UpdateState;
  configHealth: ConfigHealth;
  onOpenSettings(issueKey?: string): void;
  onOpenUpdateDialog(): void;
}) {
  const [isMaximized, setIsMaximized] = useState(false);
  const [statusPopoverOpen, setStatusPopoverOpen] = useState(false);
  const statusPopoverRef = useRef<HTMLDivElement | null>(null);
  const isDesktopWindowControlsAvailable = Boolean(window.desktop?.window);

  useEffect(() => {
    async function checkMaximized() {
      if (window.desktop) {
        const maximized = await window.desktop.window.isMaximized();
        setIsMaximized(maximized);
      }
    }
    void checkMaximized();
  }, []);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!statusPopoverRef.current?.contains(event.target as Node)) {
        setStatusPopoverOpen(false);
      }
    }

    window.addEventListener("mousedown", handlePointerDown);
    return () => window.removeEventListener("mousedown", handlePointerDown);
  }, []);

  function handleStatusPopoverKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape") {
      return;
    }
    setStatusPopoverOpen(false);
  }

  const handleMaximize = async () => {
    if (window.desktop) {
      await window.desktop.window.maximize();
      const maximized = await window.desktop.window.isMaximized();
      setIsMaximized(maximized);
    }
  };

  const handleMinimize = async () => {
    if (window.desktop) {
      await window.desktop.window.minimize();
    }
  };

  const handleClose = async () => {
    if (window.desktop) {
      await window.desktop.window.close();
    }
  };

  const serviceStatusText = serviceOnline ? "在线" : backendRunning ? "启动中" : "离线";
  const hasNewVersion = ["available", "downloading", "downloaded", "installing"].includes(updateState.status);
  const hasConfigProblem = configHealth.checked && !configHealth.isConfigured;
  const runtimePending = serviceOnline && !runtimeReady;
  const statusPillText = hasConfigProblem ? "配置缺失" : hasNewVersion ? "有新版本" : runtimePending ? "运行环境准备中" : "运行状态";
  const updateStatusText = hasNewVersion
    ? `v${updateState.version || "-"}`
    : updateState.status === "checking"
      ? "检查中"
      : updateState.status === "error"
        ? "检查失败"
        : "已是最新";
  const configStatusText = !configHealth.checked
    ? "检测中"
    : configHealth.hasBlockingIssues
      ? "需立即补全"
      : configHealth.isConfigured
        ? "已就绪"
        : "建议补全";

  function handleOpenUpdateDialog() {
    setStatusPopoverOpen(false);
    onOpenUpdateDialog();
  }

  return (
    <div className="title-bar">
      <div className="title-bar-sidebar-brand">
        <div className="title-bar-brand-mark">
          <img src="/static/assets/icons/icon.svg" alt="" />
        </div>
        <span className="title-bar-brand-text">VidMind</span>
      </div>
      <div className="title-bar-drag-region" />
      <div className="title-bar-controls">
        <div
          className={`title-bar-status-wrap ${statusPopoverOpen ? "is-open" : ""}`}
          ref={statusPopoverRef}
          onKeyDown={handleStatusPopoverKeyDown}
        >
          <button
            className={`title-bar-status-pill ${
              hasConfigProblem ? "is-danger" : hasNewVersion ? "is-update" : serviceOnline ? (runtimePending ? "is-pending" : "is-success") : ""
            }`}
            type="button"
            onClick={() => {
              setStatusPopoverOpen((current) => !current);
            }}
            aria-label="查看运行状态"
            aria-expanded={statusPopoverOpen}
            aria-haspopup="dialog"
            title="运行状态"
          >
            <span
              className={`title-bar-status-dot ${
                hasConfigProblem ? "is-danger" : hasNewVersion ? "is-update" : serviceOnline ? (runtimePending ? "is-pending" : "is-success") : backendRunning ? "is-pending" : ""
              }`}
            />
            <span>{statusPillText}</span>
          </button>

          <div
            className={`title-bar-status-popover ${statusPopoverOpen ? "is-visible" : ""}`}
            role="dialog"
            aria-label="运行状态详情"
          >
            <div className="title-bar-status-header">
              <h2>运行状态</h2>
            </div>
            <div className="title-bar-status-stack">
              <div className={`sidebar-status-item ${serviceOnline ? "is-success" : ""}`}>
                <span>服务</span>
                <strong>{serviceStatusText}</strong>
              </div>
              <div className={`sidebar-status-item ${runtimeReady ? "is-success" : serviceOnline ? "is-warning" : ""}`}>
                <span>运行环境</span>
                <strong>{runtimeStatusText}</strong>
              </div>
              <div className={`sidebar-status-item ${configHealth.hasBlockingIssues ? "is-danger" : !configHealth.isConfigured ? "is-warning" : "is-success"}`}>
                <span>配置</span>
                <strong>{configStatusText}</strong>
              </div>
              <button
                className={`sidebar-status-item sidebar-status-item-button ${hasNewVersion ? "is-update" : ""}`}
                type="button"
                onClick={handleOpenUpdateDialog}
              >
                <span>更新</span>
                <strong>{updateStatusText}</strong>
              </button>
              <div className="sidebar-status-item">
                <span>设备</span>
                <strong>{runtimeDeviceLabel}</strong>
              </div>
              <button
                className="sidebar-status-item sidebar-status-item-button"
                type="button"
                onClick={handleOpenUpdateDialog}
              >
                <span>版本</span>
                <strong>{version || "-"}</strong>
              </button>
            </div>
            {hasConfigProblem ? (
              <div className="title-bar-status-actions">
                <p>{configHealth.summary}</p>
                <button
                  className="secondary-button title-bar-status-action"
                  type="button"
                  onClick={() => {
                    setStatusPopoverOpen(false);
                    onOpenSettings(configHealth.blockingIssues[0]?.key || configHealth.issues[0]?.key);
                  }}
                >
                  {configHealth.actionText}
                </button>
              </div>
            ) : null}
          </div>
        </div>
        <button
          className="title-bar-button theme-toggle"
          type="button"
          onClick={onToggleTheme}
          aria-label={darkMode ? "切换到浅色模式" : "切换到深色模式"}
          title={darkMode ? "浅色模式" : "深色模式"}
        >
          {darkMode ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="5" />
              <path d="M12 1.5v2M12 20.5v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1.5 12h2M20.5 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
          )}
        </button>
        {isDesktopWindowControlsAvailable ? (
          <>
            <button
              className="title-bar-button"
              type="button"
              onClick={handleMinimize}
              aria-label="最小化"
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M2 6H10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
            <button
              className="title-bar-button"
              type="button"
              onClick={handleMaximize}
              aria-label={isMaximized ? "还原" : "最大化"}
            >
              {isMaximized ? (
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <rect x="1.5" y="3.5" width="7" height="7" stroke="currentColor" strokeWidth="1.5" />
                  <path d="M3.5 3.5V1.5H9.5V3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              ) : (
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <rect x="1.5" y="1.5" width="9" height="9" stroke="currentColor" strokeWidth="1.5" />
                </svg>
              )}
            </button>
            <button
              className="title-bar-button close"
              type="button"
              onClick={handleClose}
              aria-label="关闭"
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M2 2L10 10M10 2L2 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </>
        ) : null}
      </div>
    </div>
  );
}
