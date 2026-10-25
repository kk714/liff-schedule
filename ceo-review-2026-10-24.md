# CEO 審查：LINE 團隊行程助理系統設計

- 審查對象：`docs/LINE_LIFF_Schedule_System_Design_v1.2.md`（由 `Downloads/LINE_LIFF_Schedule_System_Design_v1.0.md`〔內文 v1.1〕修訂而來；原檔未修改）
- 審查日期：2026-10-24｜工具：`/plan-ceo-review`（第 2 輪）
- 模式：**SCOPE REDUCTION**（使用者明確指示）
- Codex 外部審查：依全域 CLAUDE.md 暫停，未執行。

## 審查歷程

| 輪次 | 模式 | 結果 |
|---|---|---|
| 第 1 輪 | HOLD SCOPE（使用者拒絕模式提問） | 10 項待決（P1–P10），2 個 CRITICAL GAP |
| 第 2 輪 | SCOPE REDUCTION（使用者指示） | P2 核准、P3 隨之延後；v1.2 文件與 TODOS.md 產出 |

---

## Step 0 摘要

| 項目 | 結論 |
|---|---|
| 系統稽核 | `D:\Git\liff-schedule` 全新專案、非 git repo；本輪新增 `docs/`、`TODOS.md`。 |
| 0A 前提 | v1 核心假設收斂為「團隊願意在 LINE 用一句話登錄行程」。這是可以直接驗證的假設；第一輪提出的「第二本行事曆」風險暫時縮小（不再承諾匯入），但若日後要匯入仍需先決定 P1。 |
| 0B 可重用 | 無程式碼。 |
| 0C 理想 | 見下圖；v1 先驗證入口與使用意願，再決定資料來源策略。 |
| 規模 | 移除 5 張資料表、13 支 API、2 個 LIFF 畫面、4 個連接器；預估仍 >15 檔，但核心流程已是最小可驗證範圍。 |

```
  現況                         v1.2 計畫                          12 個月理想
  各自行事曆／LINE 群組   ─▶  LINE 一句話 → NOVARK → 確認卡  ─▶  LINE 一句話查「誰何時忙」，
  口頭問同事行程                → 行程庫＋LIFF 週曆／查詢            與既有行事曆一致、零維護
                                （外部匯入 → TODOS.md）
```

### 0G 範圍縮減處置

| 項目 | 處置 | 依據 |
|---|---|---|
| Google Calendar 讀取匯入 | **DEFERRED** → TODOS.md TODO-1 | 使用者指示 |
| Microsoft Outlook 讀取匯入 | **DEFERRED** → TODO-2 | 使用者指示 |
| Apple iCloud 讀取匯入 | **DEFERRED** → TODO-3 | 使用者指示 |
| 共用匯入流程＋ICS | **DEFERRED** → TODO-4 | 隨上列一併延後：ICS 與三個來源共用同一套批次／預覽／防重流程，單獨保留等於保留整套匯入架構。若你要 v1 保留 ICS，可再調回。 |
| LIFF 週曆、查詢、表單、權限 | **KEEP**（未變動） | 你未提及；縮減不擴及這些項目 |

## 決策帳本

| ID／負責節 | 合約與證據 | 現行 | 提議 | 狀態 | 核准與範圍 |
|---|---|---|---|---|---|
| P1／Step 0 | 附錄 A：快照匯入 | 匯入已延後 | 重排匯入前先評估即時 free/busy | deferred | 隨 P2 延後；重排匯入時必須先決定 |
| P2／Step 0 | 原 §17 | **§17 移為附錄 A，v1 不實作** | — | **approved** | 使用者 2026-10-24 第 2 輪指令：「先把 Google Apple Microsoft Outlook 改為 todo」；ICS 隨同延後（見 0G） |
| P3／Sec 3 | iCloud App 專用密碼 | 隨 TODO-3 延後 | 實作時絕不保存密碼 | deferred | 隨 P2 延後 |
| P4／Step 0 | 無成功指標 | 不寫入 | — | **declined** | 使用者 2026-10-25「P4：C」：先不寫 |
| P5／Sec 1 | §10.5 | **Reply 優先＋loading 動畫＋LLM 時間預算；80% 告警；額度用盡改 Reply 短句＋LIFF 連結** | — | **approved** | 「P5：A」；寫入 §10-5、§12、A17 |
| P6／Sec 4 | §7.1 | **追溯可見並寫明；停權成員從團隊查詢隱藏** | — | **approved** | 「P6：A」；寫入 §7.1、A19 |
| P7／Sec 4 | §6 | **已過日期且明年同日在 6 個月內 → 推定明年並標示** | — | **approved** | 「P7：A」；寫入 §6、A18 |
| P8／Sec 3 | §8 | **audit_logs 只存 changed_fields；僅維運可讀** | — | **approved** | 「P8：A」；寫入 §8、A20 |
| P9／Sec 6 | §14 | **≥150 句評測集；意圖 ≥95%、欄位 ≥90%；錄放 CI** | — | **approved** | 「P9：A」；寫入 §14.1、A22 |
| P10／Sec 3 | §4.3 | **兌換顯示姓名確認；管理員撤銷重綁；72 小時單次** | — | **approved** | 「P10：A」；寫入 §4.3、A21 |

Approval readiness: PASS — P2（第 2 輪指令）；P5–P10（使用者 2026-10-25 回覆，各自僅限上列範圍）；P4 declined；P1、P3 隨 P2 延後。

---

## Section 1：架構審查（縮減後）

```
 iOS/Android
   ├──▶ LINE OA ──▶ /api/line/webhook ─(驗簽+去重寫DB)─┐
   └──▶ LIFF ──▶ FastAPI API ──(session/CSRF)──────────▼
                                              async_jobs (PostgreSQL) ◀── SPOF①
                                                     │
                                                 Worker ── 依使用者序列化
                                         ┌───────────┴───────────┐
                                         ▼                       ▼
                                 NOVARK Adapter ─▶ NOVARK ◀─SPOF②   message_outbox ─▶ LINE Reply/Push ◀─ 額度 SPOF③
```

| 發現 | 嚴重度 |
|---|---|
| **NOVARK 契約未知**：縮減後 NOVARK 變成 v1 唯一的核心外部依賴，阻塞程度更高。開工第一週做 PoC。 | **CRITICAL GAP** |
| **Push 額度**（P5）：額度用完時聊天室完全沒回應。 | **WARNING** |
| 縮減移除外部瀏覽器 OAuth 交接、CalDAV SSRF 面、Secret Store 中的第三方憑證；架構明顯變簡單。 | OK |
| 回滾路徑未寫（Sec 9）。 | WARNING |

資料流與草稿狀態機同第 1 輪（未受縮減影響）：NIL 路徑（貼圖／圖片）未定義 ← GAP。

## Section 2：錯誤與救援地圖

保留第 1 輪除匯入以外的全部列（Webhook、NOVARK Adapter、Draft.confirm、Outbox）。移除 Import.fetch、ICS.parse 共 5 列。

| 例外類別 | 救援? | 使用者看到 |
|---|---|---|
| LLMEmptyOutput | 未與 malformed 區分 ← GAP | 通用錯誤 |
| LLMRefusal | 未定義 ← GAP | 應回「無法處理此要求」 |
| MessageQuotaExceeded | N ← GAP | **靜默** ← BAD |
| 其他（逾時、429、非法 JSON、版本衝突、過期、停權、封鎖） | Y | 已定義 |

## Section 3：安全與威脅模型

縮減後移除：iCloud App 專用密碼（原唯一 High 影響項）、Google 敏感範圍審查、ICS 炸彈／SSRF、OAuth state 重播。

仍存在：同事行程標題被送進 LLM 摘要（建議 v1 禁止，只用模板）、邀請連結冒用（P10）、稽核紀錄洩漏私人行程（P8）。其餘（偽造 userId、跨組織 UUID、prompt injection、CSRF、XSS）計畫已緩解。

## Section 4：資料流與互動邊界

不受縮減影響的缺口仍在：週六說「這週五」、加入新團隊後可追溯看到同事的歷史行程（P6）、停權成員行程可見性、12 月輸入 1 月日期一律追問（P7）、空訊息／貼圖。草稿雙順序並行已有列鎖＋版本防護，需補測試。

## Section 5：程式品質（設計層）

縮減直接解決第 1 輪最大的過度工程問題（三方比對、22 張表 → 17 張）。`events.source` 列舉保留擴充值即可。No other issues.

## Section 6：測試審查

匯入驗收 I01–I18 隨附錄 A 延後。仍缺：NL 解析評測集（P9，現在是 v1 品質的主要關卡）、草稿雙順序並行、Webhook DB 失敗回非 2xx、Push 額度用盡、LLM 空回應／拒答、邀請重複兌換、加入新團隊的追溯可見性。

## Section 7：效能

團隊週曆注意 N+1 問題；要設定 NOVARK 並行上限。移除匯入後已沒有大批次工作。

## Section 8：可觀測性

缺產品指標（P4），也缺告警門檻與 runbook；作業事件只保存 7 天，回頭重建問題時不夠用。縮減後產品指標更重要，因為 v1 存在的目的就是驗證使用意願。

## Section 9：部署與上線

部署位置與 NOVARK 網路可達性仍是前提阻塞；回滾步驟未寫；建議 LLM 對話設開關（關閉時只走表單）。移除了 Google OAuth／Entra 的分環境設定需求。

## Section 10：長期軌跡

可逆性從 2/5 提升到 **3/5**：v1 仍是行程主檔，但沒有綁定外部同步模型，之後要改成「即時讀取既有行事曆」比較容易。技術債只剩 NOVARK Adapter 和評測集的維護。

## Section 11：設計與 UX

移除匯入流程（最大的摩擦點：跳到外部瀏覽器再回來）。仍缺：首次使用空狀態（引導使用者輸入範例句）、各畫面的 loading／error 狀態、無障礙。建議實作前跑 `/plan-design-review`。

---

## NOT in scope

| 項目 | 處置 | 理由 |
|---|---|---|
| Google Calendar 匯入 | Deferred（TODO-1） | 使用者指示 |
| Microsoft Outlook 匯入 | Deferred（TODO-2） | 使用者指示 |
| Apple iCloud 匯入 | Deferred（TODO-3） | 使用者指示 |
| 共用匯入流程＋ICS | Deferred（TODO-4） | 與上列共用架構，一併延後 |

## What already exists

無既有程式碼。

## Dream state delta

v1.2 交付後，可以用最低成本驗證「LINE 一句話建檔」的使用意願。資料來源策略（快照匯入或即時讀取）等有實際使用數據後再決定，不會被提早鎖死。

## Failure Modes Registry

```
 CODEPATH            | FAILURE MODE          | RESCUED? | TEST? | USER SEES?        | LOGGED?
 --------------------|-----------------------|----------|-------|-------------------|--------
 Outbox.send         | Push 月額度用盡        | Y(P5)    | A17   | Reply 短句＋連結   | Y      （已解決）
 NOVARK Adapter      | 契約未知、無法串接     | Y        | 實測  | —                  | Y      （已解決：契約取得＋2 次實測，§3.1.1）
 NOVARK Adapter      | 拒答／空回應／guardrail| Y(§3.1.3)| N     | 分類訊息           | Y
 NOVARK 提示詞       | 自行補地點、誤列結束時間| N        | 待評測 | 錯誤確認卡         | Y      ← WARNING（實測發現，需 system_prompt＋評測集）
 NL 解析             | 合法但錯的日期         | 確認卡    | A22   | 錯日期卡片         | N
 權限投影            | 新團隊追溯可見         | Y(P6)    | A19   | 依規則可見         | —
 稽核紀錄            | 含私人行程內容         | Y(P8)    | A20   | —                  | —
 邀請兌換            | 連結外洩被冒用         | Y(P10)   | A21   | 姓名確認／可撤銷   | Y
 Webhook             | DB 寫入失敗            | Y        | N     | 延遲回應           | Y
```

第 3 輪（2026-10-25）已寫入 v1.2：P5–P10。仍待實作規格補齊（非決策）：LLM 拒答／空回應分類、貼圖／空訊息回覆、「這週五已過」規則、回滾步驟與部署位置。

## Implementation Tasks

- [ ] **T1 (P1, human: ~3d / CC: ~1d)** — NOVARK — Adapter PoC：契約、10 句測試、延遲 p50/p99
  - Surfaced by: Section 1 — NOVARK 為 v1 唯一核心外部依賴
  - Verify: PoC 報告
- [ ] **T2 (P1, human: ~2h / CC: ~15min)** — 產品 — 補問題陳述、試用人數、成功指標（P4）
  - Verify: v1.2 文件新增目標章節
- [ ] **T3 (P1, human: ~1d / CC: ~1h)** — 訊息 — Push 額度策略（P5）
  - Verify: 額度用盡有可見行為並列入驗收
- [ ] **T4 (P1, human: ~2h / CC: ~15min)** — 權限 — 追溯可見性與停權成員可見性（P6）
  - Verify: 新增兩個驗收案例
- [ ] **T5 (P1, human: ~3d / CC: ~4h)** — 測試 — zh-TW NL golden set＋錄放框架（P9）
  - Verify: 評測報告
- [ ] **T6 (P2)** — 安全 — 稽核紀錄內容與讀取權限（P8）
- [ ] **T7 (P2)** — 身分 — 邀請兌換確認、撤銷、重綁（P10）
- [ ] **T8 (P2)** — 規則 — 「這週五已過」、跨年窗口（P7）、貼圖／空訊息
- [ ] **T9 (P2)** — 維運 — 產品指標、告警、runbook、回滾、部署位置

## Completion Summary

```
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SCOPE REDUCTION                             |
  | System Audit         | 全新專案；新增 docs/ v1.2 與 TODOS.md       |
  | Step 0               | P2 核准：外部匯入 4 項延後                  |
  | Section 1  (Arch)    | 2 issues found                              |
  | Section 2  (Errors)  | 14 error paths mapped, 3 GAPS               |
  | Section 3  (Security)| 3 issues found, 0 High severity             |
  | Section 4  (Data/UX) | 9 edge cases mapped, 5 unhandled            |
  | Section 5  (Quality) | 0 issues found（縮減已解決）                |
  | Section 6  (Tests)   | Diagram produced, 7 gaps                    |
  | Section 7  (Perf)    | 2 issues found                              |
  | Section 8  (Observ)  | 3 gaps found                                |
  | Section 9  (Deploy)  | 3 risks flagged                             |
  | Section 10 (Future)  | Reversibility: 3/5, debt items: 1           |
  | Section 11 (Design)  | 3 issues                                    |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (4 items)                           |
  | What already exists  | written                                     |
  | Dream state delta    | written                                     |
  | Error/rescue registry| 14 rows, 1 CRITICAL GAP                     |
  | Failure modes        | 9 total, 0 CRITICAL GAPS                    |
  | TODOS.md updates     | 4 items written                             |
  | Scope proposals      | 4 deferred（使用者指示）                    |
  | CEO plan             | skipped by mode                             |
  | Outside voice        | codex skipped（CLAUDE.md 暫停 Codex）       |
  | Lake Score           | N/A                                         |
  | Diagrams produced    | 2（架構、願景差距）＋沿用第 1 輪資料流／狀態機 |
  | Stale diagrams found | 0（v1.2 §2 架構圖已移除匯入節點）           |
  | Unresolved decisions | 0（P5–P10 approved，P4 declined）           |
  +====================================================================+
```

## Unresolved Decisions

無。P5–P10 已核准並寫入 v1.2；P4 使用者選擇不寫。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 4 | CLEAR | mode: SCOPE_REDUCTION, 0 critical gaps |
| Outside Review | codex (plan-review) | Independent 2nd opinion | 0 | skipped | 使用者全域 CLAUDE.md 暫停 Codex，no completed external review |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex，plan-review 階段，skipped（使用者指示暫停），無外部審查結果。
- **VERDICT:** CEO CLEARED；eng review required。

NO UNRESOLVED DECISIONS
