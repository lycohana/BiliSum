import { useState } from "react";
import { api } from "../api";

type DependencyStatus = "preinstalled" | "installed" | "missing";

type DependencyItem = {
  packageName: string;
  version?: string;
  status: DependencyStatus;
  purpose?: string;
  dependsOn?: string[];
  children?: DependencyItem[];
};

type DependencyTreeProps = {
  title: string;
  items: DependencyItem[];
  runtimeChannel: string;
  onStatusChange: () => void;
};

type DependencyNodeProps = {
  item: DependencyItem;
  isLast: boolean;
  prefix: string;
  runtimeChannel: string;
  onStatusChange: () => void;
};

function DependencyTreeNode({ item, isLast, prefix, runtimeChannel, onStatusChange }: DependencyNodeProps) {
  const [installing, setInstalling] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [dependencies, setDependencies] = useState<string[]>([]);
  const [operationStatus, setOperationStatus] = useState<string>("");
  const [installLog, setInstallLog] = useState<string>("");

  const connector = isLast ? "└" : "├";
  const childPrefix = isLast ? "    " : "│   ";
  const hasChildren = item.children && item.children.length > 0;

  async function handleInstall() {
    setInstalling(true);
    setOperationStatus("安装中...");
    setInstallLog("");
    try {
      const response = await api.installKnowledgeDependencies({ runtime_channel: runtimeChannel, reinstall: false });
      setOperationStatus("安装成功");
      setInstallLog(response.stdoutTail || "");
      setTimeout(() => setOperationStatus(""), 2000);
      onStatusChange();
    } catch (error) {
      setOperationStatus("安装失败");
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
    try {
      await api.uninstallPackages({ packages: [item.packageName], runtime_channel: runtimeChannel });
      setUninstallDialogOpen(false);
      setOperationStatus("卸载成功");
      setTimeout(() => setOperationStatus(""), 2000);
      onStatusChange();
    } catch (error) {
      setOperationStatus("卸载失败");
      console.error("Uninstall failed:", error);
      setTimeout(() => setOperationStatus(""), 3000);
    } finally {
      setInstalling(false);
    }
  }

  const statusIcon = item.status === "installed" ? "✓" : item.status === "preinstalled" ? "✓" : "✗";
  const statusClass = item.status === "installed" ? "status-success" : item.status === "preinstalled" ? "status-info" : "status-warning";
  const statusLabel = item.status === "installed" ? "已安装" : item.status === "preinstalled" ? "已预装" : "缺失";

  return (
    <>
      <div className="dependency-tree-node">
        <div className="dependency-tree-line">
          <span className="dependency-tree-connector">{prefix}{connector} </span>
          <div className="dependency-tree-content">
            <div className="dependency-tree-info">
              {hasChildren && (
                <button
                  className="dependency-tree-toggle"
                  onClick={() => setExpanded(!expanded)}
                  aria-label={expanded ? "收起" : "展开"}
                  type="button"
                >
                  {expanded ? "▼" : "▶"}
                </button>
              )}
              <span className="dependency-name">{item.packageName}</span>
              {item.version && <span className="dependency-version">{item.version}</span>}
              <span className={`helper-chip dependency-status-chip ${statusClass}`}>
                <span className="dependency-status-icon">{statusIcon}</span>
                {statusLabel}
              </span>
            </div>
            {(item.purpose || item.dependsOn) && (
              <div className="dependency-tree-meta">
                {item.purpose && <span className="dependency-purpose">用途：{item.purpose}</span>}
                {item.dependsOn && item.dependsOn.length > 0 && (
                  <span className="dependency-depends">依赖于：{item.dependsOn.join(", ")}</span>
                )}
              </div>
            )}
            <div className="dependency-actions">
              {item.status === "missing" && (
                <button className="primary-button install-button" type="button" disabled={installing} onClick={handleInstall}>
                  {installing ? "安装中..." : "安装"}
                </button>
              )}
              {item.status === "installed" && (
                <button className="secondary-button danger-button" type="button" disabled={installing} onClick={handleUninstallClick}>
                  卸载
                </button>
              )}
              {operationStatus && (
                <span className={`dependency-operation-status ${operationStatus.includes("成功") ? "success" : operationStatus.includes("失败") ? "error" : ""}`}>
                  {operationStatus}
                </span>
              )}
            </div>
            {installLog && (
              <div className="dependency-log-viewer" style={{ marginTop: "0.5rem" }}>
                <textarea className="textarea-field log-viewer" rows={6} readOnly value={installLog} style={{ fontSize: "0.85em" }}></textarea>
              </div>
            )}
          </div>
        </div>
        {expanded && hasChildren && (
          <div className="dependency-tree-children">
            {item.children!.map((child, index) => (
              <DependencyTreeNode
                key={child.packageName}
                item={child}
                isLast={index === item.children!.length - 1}
                prefix={prefix + childPrefix}
                runtimeChannel={runtimeChannel}
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

export function DependencyTree({ title, items, runtimeChannel, onStatusChange }: DependencyTreeProps) {
  return (
    <div className="dependency-tree">
      <div className="dependency-tree-title">{title}</div>
      {items.map((item, index) => (
        <DependencyTreeNode
          key={item.packageName}
          item={item}
          isLast={index === items.length - 1}
          prefix=""
          runtimeChannel={runtimeChannel}
          onStatusChange={onStatusChange}
        />
      ))}
    </div>
  );
}
