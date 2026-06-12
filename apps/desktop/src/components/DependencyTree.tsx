import { useEffect, useState } from "react";
import { api } from "../api";

type DependencyStatus = "preinstalled" | "installed" | "missing" | "broken";

type DependencyItem = {
  packageName: string;
  version?: string;
  status: DependencyStatus;
  purpose?: string;
  error?: string;
  dependsOn?: string[];
  children?: DependencyItem[];
};

type DependencyInstallKind = "knowledge" | "funasr" | "local";

type DependencyTreeProps = {
  title: string;
  items: DependencyItem[];
  runtimeChannel: string;
  installKind?: DependencyInstallKind;
  provider?: string;
  onStatusChange: () => Promise<void> | void;
};

type DependencyNodeProps = {
  item: DependencyItem;
  depth: number;
  runtimeChannel: string;
  installKind: DependencyInstallKind;
  provider?: string;
  onStatusChange: () => Promise<void> | void;
};

function DependencyTreeNode({ item, depth, runtimeChannel, installKind, provider, onStatusChange }: DependencyNodeProps) {
  const [installing, setInstalling] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [dependencies, setDependencies] = useState<string[]>([]);
  const [operationStatus, setOperationStatus] = useState<string>("");
  const [installLog, setInstallLog] = useState<string>("");
  const [optimisticStatus, setOptimisticStatus] = useState<DependencyStatus | null>(null);

  useEffect(() => {
    setOptimisticStatus(null);
  }, [item.status, item.version]);

  const hasChildren = item.children && item.children.length > 0;
  const hasMeta = Boolean(item.purpose || item.error || (item.dependsOn && item.dependsOn.length > 0));

  async function handleBulkInstall(reinstall: boolean) {
    setInstalling(true);
    setOperationStatus("安装中...");
    setInstallLog("");
    const sessionId = `${installKind}-${Date.now()}`;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    try {
      pollTimer = setInterval(async () => {
        try {
          const log = await api.getInstallLog(sessionId);
          if (log.log) setInstallLog(log.log);
          if (log.done && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
        } catch {
          // Polling is best-effort; the install request still returns a tail.
        }
      }, 1500);

      const response = installKind === "funasr"
        ? await api.installFunAsr({ reinstall, installSessionId: sessionId })
        : installKind === "local"
          ? await api.installLocalAsr({ reinstall, installSessionId: sessionId })
          : await api.installKnowledgeDependencies({ runtime_channel: runtimeChannel, provider, reinstall, installSessionId: sessionId });
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      try {
        const log = await api.getInstallLog(sessionId);
        setInstallLog(log.log || response.stdoutTail || "");
      } catch {
        setInstallLog(response.stdoutTail || "");
      }
      setOptimisticStatus(null);
      setOperationStatus(response.installed ? "安装成功，正在刷新状态..." : "安装后仍未就绪，正在刷新状态...");
      await onStatusChange();
      setOperationStatus(response.installed ? "安装成功" : "安装后仍未就绪");
      setTimeout(() => setOperationStatus(""), 2000);
    } catch (error) {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      setOperationStatus("安装失败");
      setInstallLog(error instanceof Error ? error.message : "安装失败");
      console.error("Install failed:", error);
      setTimeout(() => setOperationStatus(""), 3000);
    } finally {
      setInstalling(false);
    }
  }

  async function handleUninstallClick() {
    try {
      const result = await api.getPackageDependencies(item.packageName);
      setDependencies(result.dependencies);
      setUninstallDialogOpen(true);
    } catch (error) {
      console.error("Failed to check dependencies:", error);
    }
  }

  async function handleUninstallConfirm() {
    setInstalling(true);
    setOperationStatus("卸载中...");
    setInstallLog("");
    try {
      const result = await api.uninstallPackages({ packages: [item.packageName], runtime_channel: runtimeChannel });
      setUninstallDialogOpen(false);
      setOptimisticStatus("missing");
      setOperationStatus("卸载成功，正在刷新状态...");
      setInstallLog([result.stdout, result.stderr].filter(Boolean).join("\n"));
      await onStatusChange();
      setOperationStatus("卸载成功");
      setTimeout(() => setOperationStatus(""), 2000);
    } catch (error) {
      setOperationStatus("卸载失败");
      setInstallLog(error instanceof Error ? error.message : "卸载失败");
      console.error("Uninstall failed:", error);
      setTimeout(() => setOperationStatus(""), 3000);
    } finally {
      setInstalling(false);
    }
  }

  let statusIcon: string;
  let statusClass: string;
  let statusLabel: string;

  const effectiveStatus = optimisticStatus || item.status;
  const effectiveVersion = optimisticStatus === "missing" ? undefined : item.version;

  if (effectiveStatus === "installed") {
    statusIcon = "\u2713";
    statusClass = "status-success";
    statusLabel = "已安装";
  } else if (effectiveStatus === "preinstalled") {
    statusIcon = "\u2713";
    statusClass = "status-info";
    statusLabel = "已预装";
  } else if (effectiveStatus === "broken") {
    statusIcon = "";
    statusClass = "status-error";
    statusLabel = "损坏";
  } else {
    statusIcon = "";
    statusClass = "status-warning";
    statusLabel = "缺失";
  }

  return (
    <>
      <div className={`dependency-tree-node depth-${Math.min(depth, 3)}`}>
        <div className={`dependency-tree-content ${effectiveStatus === "broken" ? "is-broken" : ""}`}>
          <div className="dependency-tree-main">
            <div className="dependency-tree-info">
              {hasChildren ? (
                <button
                  className="dependency-tree-toggle"
                  onClick={() => setExpanded(!expanded)}
                  aria-label={expanded ? "收起" : "展开"}
                  type="button"
                >
                  {expanded ? "\u25BC" : "\u25B6"}
                </button>
              ) : (
                <span className="dependency-tree-toggle is-placeholder" aria-hidden="true" />
              )}
              <div className="dependency-tree-package">
                <div className="dependency-tree-title-row">
                  <span className="dependency-name">{item.packageName}</span>
                  {effectiveVersion && <span className="dependency-version">{effectiveVersion}</span>}
                  <span className={`helper-chip dependency-status-chip ${statusClass}`}>
                    {statusIcon ? <span className="dependency-status-icon">{statusIcon}</span> : <span className="dependency-status-dot" aria-hidden="true" />}
                    {statusLabel}
                  </span>
                </div>
                {hasMeta && (
                  <div className="dependency-tree-meta">
                    {item.purpose && <span className="dependency-purpose">用途：{item.purpose}</span>}
                    {item.dependsOn && item.dependsOn.length > 0 && (
                      <span className="dependency-depends">依赖于：{item.dependsOn.join(", ")}</span>
                    )}
                    {item.error && <span className="dependency-error">错误：{item.error}</span>}
                  </div>
                )}
              </div>
            </div>
            <div className="dependency-actions">
              {(effectiveStatus === "missing" || effectiveStatus === "broken") && (
                <button
                  className="primary-button install-button"
                  type="button"
                  disabled={installing}
                  onClick={() => handleBulkInstall(effectiveStatus === "broken")}
                >
                  {installing ? "安装中..." : effectiveStatus === "broken" ? "重新安装" : "安装"}
                </button>
              )}
              {effectiveStatus === "installed" && (
                <button className="secondary-button danger-button" type="button" disabled={installing} onClick={handleUninstallClick}>
                  卸载
                </button>
              )}
              {operationStatus && (
                <span className={`dependency-operation-status ${operationStatus.includes("成功") ? "success" : operationStatus.includes("失败") ? "error" : ""}`} aria-live="polite">
                  {operationStatus}
                </span>
              )}
            </div>
          </div>
          {installLog && (
            <div className="dependency-log-viewer">
              <textarea className="textarea-field log-viewer dependency-log-textarea" rows={6} readOnly value={installLog}></textarea>
            </div>
          )}
        </div>
        {expanded && hasChildren && (
          <div className="dependency-tree-children">
            {item.children!.map((child) => (
              <DependencyTreeNode
                key={child.packageName}
                item={child}
                depth={depth + 1}
                runtimeChannel={runtimeChannel}
                installKind={installKind}
                provider={provider}
                onStatusChange={onStatusChange}
              />
            ))}
          </div>
        )}
      </div>

      {uninstallDialogOpen && (
        <div className="modal-overlay" onClick={() => setUninstallDialogOpen(false)}>
          <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
            <h3>确认卸载 {item.packageName}？</h3>
            {dependencies.length > 0 ? (
              <>
                <p>以下功能依赖此包：</p>
                <ul>
                  {dependencies.map((dep) => (
                    <li key={dep}>{dep}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p>确定要卸载此包吗？</p>
            )}
            <div className="modal-actions">
              <button className="secondary-button" type="button" onClick={() => setUninstallDialogOpen(false)}>
                取消
              </button>
              <button className="primary-button danger-button" type="button" disabled={installing} onClick={handleUninstallConfirm}>
                {installing ? "卸载中..." : "确认卸载"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function DependencyTree({ title, items, runtimeChannel, installKind = "knowledge", provider, onStatusChange }: DependencyTreeProps) {
  return (
    <div className="dependency-tree">
      <div className="dependency-tree-title">{title}</div>
      {items.map((item) => (
        <DependencyTreeNode
          key={item.packageName}
          item={item}
          depth={0}
          runtimeChannel={runtimeChannel}
          installKind={installKind}
          provider={provider}
          onStatusChange={onStatusChange}
        />
      ))}
    </div>
  );
}
