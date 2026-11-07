export function TaskScheduleControl({ value, error, onChange }: {
  value: string;
  error: string;
  onChange(value: string): void;
}) {
  return <div className="task-schedule-control">
    <label className="summary-preference-label" htmlFor="task-scheduled-at">定时设置</label>
    <div className="task-schedule-row">
      <input id="task-scheduled-at" type="datetime-local" value={value} aria-label="本次定时开始时间"
        aria-invalid={Boolean(error)} onChange={(event) => onChange(event.target.value)} />
      <button type="submit" name="schedule-start" disabled={!value} aria-label="按定时时间开始">开始</button>
    </div>
    {error ? <small role="alert">{error}</small> : null}
    <small>仅本次提交生效 · 本地时间 · 需保持服务运行</small>
  </div>;
}
