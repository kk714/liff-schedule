// 載入中／錯誤／空狀態（每個畫面都要有，避免白畫面）。
export function Status({
  kind,
  title,
  message,
  action,
}: {
  kind: "loading" | "error" | "empty";
  title?: string;
  message?: string;
  action?: React.ReactNode;
}) {
  if (kind === "loading") {
    return (
      <div className="status" role="status" aria-live="polite">
        <div className="spinner" aria-hidden />
        <p>{message ?? "載入中…"}</p>
      </div>
    );
  }
  return (
    <div className={`status status-${kind}`} role={kind === "error" ? "alert" : undefined}>
      {title && <h2>{title}</h2>}
      {message && <p>{message}</p>}
      {action}
    </div>
  );
}
