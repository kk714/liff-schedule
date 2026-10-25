# TODOS

## 外部行事曆匯入（v1.2 延後）

來源：2026-10-24 `/plan-ceo-review`，使用者決策「直接 LINE 對話，經過 NOVARK LLM 變成行程，先把 Google、Apple、Microsoft Outlook 改為 TODO」。完整設計保留在 `docs/LINE_LIFF_Schedule_System_Design_v1.2.md` 附錄 A。

### TODO-1：Google Calendar 讀取匯入
- **What:** 透過 OAuth 讀取使用者 Google 行事曆，預覽、逐筆勾選後匯入。
- **Why:** 已在用 Google 的成員不必重打行程。
- **Pros:** 降低登錄成本，團隊週曆更完整。
- **Cons:** `calendar.events.readonly` 是敏感範圍，對外部使用者需 OAuth 驗證（數週）；LIFF WebView 不能直接授權，需外部瀏覽器流程；快照匯入會漂移。
- **Context:** 若公司用 Google Workspace，可設為 Internal 應用免驗證。先評估「即時 free/busy 讀取」是否比快照更好（審查 P1）。
- **Effort:** human: L / CC: M
- **Priority:** P2
- **Depends on:** v1 上線並取得對話建檔使用率；共用匯入流程（TODO-4）。

### TODO-2：Microsoft Outlook（Graph）讀取匯入
- **What:** 透過 Microsoft identity OAuth＋Graph calendarView 讀取匯入。
- **Why:** 公司若用 M365，成員行程大多在 Outlook。
- **Pros:** 同 TODO-1。
- **Cons:** 公司租戶政策可能禁止使用者同意；需 Entra 應用註冊與管理員同意。
- **Context:** 權限從 `Calendars.ReadBasic` 起步。
- **Effort:** human: L / CC: M
- **Priority:** P2
- **Depends on:** TODO-4；IT 確認租戶同意政策。

### TODO-3：Apple iCloud（CalDAV）讀取匯入
- **What:** 後端 CalDAV 連接器，以 App 專用密碼讀取。
- **Why:** iPhone 使用者的 iCloud 行事曆。
- **Pros:** 涵蓋 Apple 使用者。
- **Cons:** App 專用密碼權限遠超過讀行事曆，是全系統最高風險憑證；iCloud 相容性未經 PoC 驗證。
- **Context:** 實作前先做 PoC；建議絕不保存密碼（審查 P3）。
- **Effort:** human: XL / CC: L
- **Priority:** P3
- **Depends on:** TODO-4；PoC 通過；資安同意。

### TODO-4：共用匯入流程＋ICS 檔案匯入
- **What:** 匯入批次、預覽、勾選、防重、來源連結資料表與 ICS 解析。
- **Why:** TODO-1～3 的共同基礎；ICS 也是政策阻擋時的替代路徑。
- **Pros:** 一套流程服務所有來源。
- **Cons:** 三方比對（前次快照／現況／新來源）複雜度高，建議先簡化為「顯示差異 → 覆蓋或略過」。
- **Context:** 附錄 A §17.2、§17.5–17.8。
- **Effort:** human: L / CC: M
- **Priority:** P2
- **Depends on:** v1 上線。
