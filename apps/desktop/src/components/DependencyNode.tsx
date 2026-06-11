import { useState } from "react";
import { api } from "../api";

type DependencyStatus = "preinstalled" | "installed" | "missing";

type DependencyNodeProps = {
  packageName: string;
  version?: string;
  status: DependencyStatus;
  isRequired: boolean;
  runtimeChannel: string;
  onStatusChange: () => void;
};

export function DependencyNode({ packageName, version, status, isRequired, runtimeChannel, onStatusChange }: DependencyNodeProps) {
  const [installing, setInstalling] = useState(false);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [dependencies, setDependencies] = useState<string[]>([]);

  async function handleInstall() {
    setInstalling(true);
    try {
      await api.installKnowledgeDependencies({ runtime_channel: runtimeChannel, reinstall: false });
      onStatusChange();
    } catch (error) {
      console.error("Install failed:", error);
    } finally {
      setInstalling(false);
    }
  }

  async function handleUninstallClick() {
    try {
      const result = await api.getPackageDependencies(packageName);
      setDependencies(result.dependencies);
      setUninstallDialogOpen(true);
    } catch (error) {
      console.error("Failed to check dependencies:", error);
    }
  }

  async function handleUninstallConfirm() {
    setInstalling(true);
    try {
      await api.uninstallPackages({ packages: [packageName], runtime_channel: runtimeChannel });
      setUninstallDialogOpen(false);
      onStatusChange();
    } catch (error) {
      console.error("Uninstall failed:", error);
    } finally {
      setInstalling(false);
    }
  }

  const statusClass = status === "installed" ? "status-success" : status === "preinstalled" ? "status-info" : "status-warning";
  const statusLabel = status === "installed" ? "已安装" : status === "preinstalled" ? "已预装" : "缺失";

  return (
    <>
      <div className="dependency-node">
        <div className="dependency-info">
          <span className="dependency-name">{packageName}</span>
          {version && <span className="dependency-version">{version}</span>}
          <span className={`helper-chip ${statusClass}`}>{statusLabel}</span>
          {isRequired && <span className="helper-chip">required</span>}
          {!isRequired && <span className="helper-chip status-muted">optional</span>}
        </div>
        <div className="dependency-actions">
          {status === "missing" && (
            <button className="secondary-button" type="button" disabled={installing} onClick={handleInstall}>
              {installing ? "安装中..." : "安装"}
            </button>
          )}
          {(status === "installed") && (
            <button className="secondary-button danger-button" type="button" disabled={installing} onClick={handleUninstallClick}>
              卸载
            </button>
          )}
        </div>
      </div>

      {uninstallDialogOpen && (
        <div className="modal-overlay" onClick={() => setUninstallDialogOpen(false)}>
          <div className="modal-dialog" onClick={(e) => e.stopPropagation()}>
            <h3>确认卸载 {packageName}？</h3>
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
