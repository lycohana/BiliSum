type CookieHelpDialogProps = {
  isOpen: boolean;
  onClose(): void;
  onOpenTutorial(): void;
  onOpenSettings(): void;
  onCaptureLoginCookies?: () => void | Promise<void>;
};

export function CookieHelpDialog({
  isOpen,
  onClose,
  onOpenTutorial,
  onOpenSettings,
  onCaptureLoginCookies,
}: CookieHelpDialogProps) {
  if (!isOpen) {
    return null;
  }

  return (
    <div className="update-dialog-overlay" onClick={onClose}>
      <div className="update-dialog cookie-help-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="update-dialog-header">
          <h2>B 站需要登录态</h2>
          <button className="close-button" type="button" onClick={onClose} aria-label="关闭">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="update-dialog-body cookie-help-body">
          <div className="cookie-help-hero">
            <span className="section-kicker">HTTP 412</span>
            <strong>当前请求可能被 B 站风控拦截</strong>
            <p>打开 VidMind 自带的 B 站登录窗口，登录完成后会自动捕获登录态并保存为 yt-dlp 可用的 cookies.txt。</p>
          </div>

          <div className="cookie-help-summary">
            <strong>处理方法汇总</strong>
            <p>1. 点击“打开 B 站登录窗口”，在新窗口完成扫码或账号登录。</p>
            <p>2. 登录成功后应用会自动导出 B 站 cookies.txt，并写入设置。</p>
            <p>3. 重新提交任务；如果还是 412，切换网络/IP 或稍后重试。</p>
            <p>4. 自动捕获失败时，再按教程手动导出 Netscape/Mozilla 格式 cookies.txt。</p>
          </div>

          <div className="cookie-help-steps">
            <div className="cookie-help-step">
              <span>1</span>
              <div>
                <strong>先在浏览器登录 B 站</strong>
                <p>使用 VidMind 打开的登录窗口，不再读取 Chrome/Edge/Firefox 的本机数据库。</p>
              </div>
            </div>
            <div className="cookie-help-step">
              <span>2</span>
              <div>
                <strong>自动捕获 cookies.txt</strong>
                <p>登录完成后应用会捕获 bilibili.com 登录态并保存成 Netscape cookies 文件。</p>
              </div>
            </div>
            <div className="cookie-help-step">
              <span>3</span>
              <div>
                <strong>重新提交任务</strong>
                <p>保存成功后重新提交视频链接；如果仍然 412，换网络或稍后重试。</p>
              </div>
            </div>
          </div>
        </div>

        <div className="update-dialog-footer">
          {onCaptureLoginCookies ? <button className="primary-button" type="button" onClick={onCaptureLoginCookies}>打开 B 站登录窗口</button> : null}
          <button className="secondary-button" type="button" onClick={onOpenTutorial}>打开导出教程</button>
          <button className="secondary-button" type="button" onClick={onOpenSettings}>前往 Cookies 设置</button>
        </div>
      </div>
    </div>
  );
}
