# Eng Review：LINE 團隊行程助理系統設計 v1.2

- 審查對象：`docs/LINE_LIFF_Schedule_System_Design_v1.2.md`（固定）
- 審查日期：2026-10-25｜工具：`/plan-eng-review`
- 報告檔：本檔（設計文件只寫入已核准的變更）
- 證據來源：設計文件、NOVARK API 實測 2 次（2026-10-25）。專案沒有程式碼、沒有 git，因此無法做歷史回顧。

## Scope Challenge

**A. 評估**
- 既有可重用：無程式碼。可用的成熟元件：FastAPI、SQLAlchemy／Alembic、httpx（NOVARK 呼叫）、`line-bot-sdk`（驗簽／Reply／Push／loading animation）、`@line/liff`。**[Layer 1]**
- 複雜度：預估 >40 檔；新服務 3 個（API、Worker、LIFF 前端）＋ NOVARK Adapter。觸發複雜度門檻。
- TODOS.md：外部匯入 TODO-1～4 不阻塞本計畫，本計畫也不需要它們。
- 部署：部署位置仍待定（§15.2），屬上線前提，不阻塞開發。

**Scope Challenge 發現**
1. `[P2] (confidence: 8/10) v1.2:80` — 「PydanticAI 可作對話協調層，但必須先確認 NOVARK 的模型介面是否相容」。實測確認 NOVARK 是自訂 Agent 端點（`POST /api/agents/llm_agent/run`，`inputs`／`output.json`），不是 OpenAI 相容的模型介面，PydanticAI 用不上，只會多一層。→ D1
2. `[P2] (confidence: 7/10) v1.2:16` — 「LINE 官方帳號一對一聊天，以及 LIFF 內對話輸入」。使用者已決定「直接 LINE 對話」；LIFF 對話輸入是第二條對話路徑，需要額外的輪詢 API（v1.2:364–365）和非同步狀態，但不帶來新能力。→ D2

## Section 1：架構

```
 LINE OA ─▶ POST /api/line/webhook ─驗簽─▶ inbound_events(去重) + async_jobs ─▶ 2xx
                                                   │
                       Worker（N 並行；同一 user 以 advisory lock 序列化）
                                                   │
             檢查剩餘 Reply 預算 ──不足──▶ Reply「處理較久」＋LIFF 連結（背景續跑）
                    │足夠
             loading animation ─▶ NOVARK（≈5.6–8.6 s）─▶ Pydantic 驗證 ─▶ 草稿 v1
                                                   │
                                     message_outbox ─▶ Reply（優先）／Push
 LIFF ─▶ /api/drafts/{id}/confirm ─▶ 交易＋列鎖 ─▶ events
```

發現：
- `[P1] (confidence: 8/10) v1.2:409–412` — 「同一使用者的對話工作依序執行」＋「總時間預算設定在 Reply Token 仍可使用的範圍內」。預算只計算 NOVARK 呼叫本身，沒有把**排隊等待時間**算進去。實測單次 NOVARK 5.6–8.6 秒；Worker 並行數不足，或同一使用者連發數則訊息時，工作出佇列時 Reply Token 可能已經快過期，NOVARK 卻照樣呼叫，結果只能改用 Push（消耗額度），或乾脆失敗。→ D3
- `[P2] (confidence: 7/10) v1.2:132` — guardrail 攔下時只回「無法處理此要求」。Guardrail 誤判正常句子（例如「忽略上次那個會，改到 3 點」）時，使用者沒有退路，系統也看不到誤判率。→ D4
- Security：LINE ID token 驗證、Postback 只帶 draft_id＋version、CSRF、跨組織 UUID 防護、external_identities 唯一鍵（防止管理員用自己已綁定的 LINE 兌換他人邀請），計畫都已涵蓋。No issues。
- SPOF：PostgreSQL 單點（佇列也在其中）；以 v1 規模可接受，已有每日備份與還原演練（v1.2:444）。

## Section 2：程式品質

- `[P2] (confidence: 9/10) v1.2:116 vs v1.2:169` — §3.3 規定 `start_local` 不帶 offset，實測模型卻回 `+08:00`；v1.2:116 寫「剝除或驗證」，兩種做法沒有選定。→ D5
- `[P2] (confidence: 8/10) v1.2:275` — 「今天／明天／下週」有定義，但「這週五」在週六說、「禮拜一」在週一晚上說都沒有規則；模型與後端可能各自猜測。→ D6
- `[P2] (confidence: 8/10) v1.2:209、§1.2` — 只說 group／room 不處理；一對一聊天中的貼圖、圖片、語音、位置等非文字訊息沒有定義回應（NIL 路徑）。→ D7
- 已直接修正（不改變行為，只修不一致）：
  - A04（v1.2:467）與 P7 跨年規則矛盾 → 改為「明年同日超過 6 個月才追問」。
  - `llm_call_logs` 欄位（v1.2:337）與 §3.1.3 不一致 → 補上 trace_id、duration_ms、token_usage、binding_snapshot_id。

## Section 3：測試

框架：文件未指定，專案無程式碼。→ D8

```
CODE PATHS                                              USER FLOWS
[+] webhook                                             [+] LINE 建檔
  ├── [GAP] 驗簽失敗 → 401                                ├── [GAP][→E2E] 一句話 → 確認卡 → 確認 → 行程
  ├── [GAP] 重送去重                                      ├── [GAP] 連點確認／舊卡確認
  └── [GAP] DB 寫入失敗 → 非 2xx                          └── [GAP] 追問後合併到同一草稿
[+] worker                                              [+] 查詢
  ├── [GAP] 同一使用者序列化（雙順序）                   ├── [GAP] 私人／忙碌投影、關鍵字不洩漏
  ├── [GAP] Reply 預算不足 → fallback（D3）              └── [GAP] 加入新團隊／停權（A19）
  └── [GAP] Push 額度用盡（A17）                         [+] 綁定
[+] NOVARK Adapter                                        ├── [GAP] 邀請過期／重複／姓名不符（A21）
  ├── [GAP] 8 種回應對應（§3.1.3 每列一個 table case）    └── [GAP] 撤銷後舊 session 失效
  ├── [GAP] guardrail 攔下（D4）                         
  └── [GAP] offset 正規化（D5）                           
[+] 日期規則（純函式）                                    
  ├── [GAP] 跨年 6 個月窗口（A18）                        
  └── [GAP] 這週X 已過（D6）                              
[+] draft confirm 交易                                    
  └── [GAP][→E2E] LINE 與 LIFF 同時確認／修改（雙順序）   
LLM: [GAP][→EVAL] §14.1 評測集 ≥150 句（意圖 ≥95%、欄位 ≥90%）

COVERAGE: 0/20 paths（尚無程式碼，全部是待寫測試）
```

Value cards（關鍵項目）：
- Value: protects=同一 draft_version 只提交一次; fails_when=列鎖或版本比對被移除; why_new=尚無測試; seam=none
- Value: protects=busy_only 不洩漏標題／地點／關鍵字命中; fails_when=查詢改成先撈全部再遮蔽; why_new=尚無測試; seam=none
- Value: protects=Reply 預算不足時仍有回應; fails_when=worker 未檢查剩餘預算; why_new=尚無測試; seam=none
- Value: protects=NOVARK 8 種回應各自正確分支; fails_when=只判斷 HTTP 狀態碼、忽略 success; why_new=尚無測試; seam=none

## Section 4：效能

- `[P1] (confidence: 7/10) v1.2:408–409` — 同 D3：NOVARK 單次 5.6–8.6 秒，Worker 若同步、單程序執行，每分鐘只能處理約 7–10 則訊息。需要非同步 httpx 加上可配置的並行數。規模：試用人數未知，以 30 人同時傳訊估算，並行數至少 8。
- 團隊週曆：先用一次查詢算出「可見擁有者集合」，再用 `owner_user_id IN (...)` 查 events，避免逐一查每位成員（N+1）。現有索引（v1.2:347）足夠。
- httpx 用單一共用 client（連線池）；timeout 依所在路徑分開設定（v1.2:100–101 已涵蓋）。

## Outside Voice

依全域 CLAUDE.md，Codex 暫停中，未執行；也沒有以原生 subagent 替代。外部審查視為缺席。

## Decision ledger

使用者 2026-10-25 回覆：「D1:A D2:A D3:A D4:A D5:A D6:A D7:A D8:B」。

| ID | 決策 | 回覆 | 狀態 | 已寫入 v1.2 |
|---|---|---|---|---|
| D1 | 移除 PydanticAI，只用 httpx＋Pydantic Adapter | A | approved | §2.1 |
| D2 | 刪除 LIFF 對話輸入與 `/api/conversations/*` | A | approved（範圍縮減） | §1.1、§2、§2.1、§3.1、§9、§9.1、§11 |
| D3 | 工作記錄 reply_deadline；預算不足先 Reply；async Worker 並行 8；advisory lock 依使用者序列化 | A | approved | §10-4 |
| D4 | guardrail 攔下附表單連結；只記分類與 trace_id；每週檢查誤判率 | A | approved | §3.1.2 |
| D5 | offset 與使用者時區相同就去掉，不同就追問 | A | approved | §3.1.1 |
| D6 | 「這週X」已過 → 追問是否指下週 | A | approved | §6 |
| D7 | 非文字訊息 → Reply 固定短句，不呼叫 NOVARK | A | approved | §6 |
| D8 | 後端測試用 SQLite（非建議選項） | B | approved（shortcut，completeness 6/10） | §14.2 |

D8 已知限制：SQLite 測不到列鎖、advisory lock、timestamptz、交易隔離。§14.2 已規定 A06、A07、使用者序列化、A13 必須在 PostgreSQL 環境驗收；並訂出升級條件：出現並行或時區缺陷，或 CI 需要自動驗證時，改用 testcontainers。

Approval readiness: PASS — D1–D8（皆引用上方使用者回覆，範圍限於各列）。

## NOT in scope

- LIFF 對話輸入（D2 刪除）。
- 外部行事曆匯入（CEO review 已延後，TODOS.md TODO-1～4）。
- PostgreSQL 自動化並行測試（D8 改用 SQLite；改為環境驗收，並訂有升級條件）。

## What already exists

無程式碼。採用的成熟元件：FastAPI、SQLAlchemy／Alembic、httpx、line-bot-sdk、@line/liff、pytest、respx、Vitest、Playwright。

## Failure modes

| 路徑 | 實際可能的失敗 | 處理 | 測試 | 使用者看到 |
|---|---|---|---|---|
| Worker 排隊 | Reply Token 過期 | D3 先 Reply | 需寫 | 「處理較久」＋連結 |
| NOVARK | 逾時／429／503 | 同 key 重試 | respx | 稍後或表單 |
| Guardrail | 誤判正常句 | D4 表單連結 | respx | 拒絕＋表單連結 |
| 模型輸出 | 時區不符 | D5 追問 | 單元 | 追問 |
| 草稿確認 | LINE 與 LIFF 同時確認 | 列鎖＋版本 | **PostgreSQL 環境驗收（D8）** | 409／已提交結果 |
| 非文字訊息 | 貼圖、圖片 | D7 固定回覆 | 單元 | 提示只支援文字 |

Critical gaps：0（每條路徑都有處理方式與使用者可見的訊息）。

## Worktree parallelization strategy

| Step | Modules touched | Depends on |
|---|---|---|
| NOVARK Adapter＋評測集 | backend/llm、evals | — |
| 資料模型＋身分綁定 | backend/models、migrations、backend/auth | — |
| 行程／草稿／權限服務 | backend/services | 資料模型 |
| Webhook＋Worker | backend/line、worker | 資料模型、Adapter 介面 |
| LIFF 前端 | frontend | 行程服務 API |

- Lane A：NOVARK Adapter＋評測集（獨立）
- Lane B：資料模型 → 行程服務 → Webhook／Worker（共用 backend/models，依序做）
- Lane C：LIFF 前端（行程 API 契約定案後開始）

執行順序：A 與 B 同時開始；B 的行程服務 API 定案後啟動 C；Adapter 介面合併後 B 才接 Worker。衝突點：backend/models（A 只讀，不改）。

## Implementation Tasks

- [ ] **T1 (P1, human: ~2d / CC: ~2h)** — llm — httpx NOVARK Adapter（§3.1.1–3.1.3 的 8 種回應、offset 正規化、guardrail 退路）
  - Surfaced by: Section 1／2 — D1、D4、D5
  - Files: backend/llm/（待定）
  - Verify: respx table-driven 測試，§3.1.3 每列一個 case
- [ ] **T2 (P1, human: ~2d / CC: ~1.5h)** — worker — reply_deadline、async 並行、advisory lock 依使用者序列化
  - Surfaced by: Section 1／4 — D3
  - Files: worker/（待定）
  - Verify: 單元測試＋PostgreSQL 環境驗收序列化
- [ ] **T3 (P1, human: ~3d / CC: ~4h)** — evals — ≥150 句評測集，含實測發現的三個提示詞問題
  - Surfaced by: Section 3 — [→EVAL]
  - Files: evals/（待定）
  - Verify: 意圖 ≥95%、欄位 ≥90%
- [ ] **T4 (P1, human: ~1d / CC: ~30min)** — qa — 撰寫 PostgreSQL 環境驗收腳本（A06、A07、序列化、A13）
  - Surfaced by: D8 shortcut 限制
  - Files: scripts/（待定）
  - Verify: 腳本在預備環境全數通過
- [ ] **T5 (P2, human: ~2h / CC: ~10min)** — rules — 「這週X」已過與非文字訊息
  - Surfaced by: Section 2 — D6、D7
  - Files: backend/rules/（待定）
  - Verify: 單元測試

JSONL artifact：專案非 git、未產生 aggregation 檔（not persisted）。

## Completion summary

- Step 0: Scope Challenge — scope reduced per recommendation（D2 刪除 LIFF 對話輸入；D1 移除 PydanticAI）
- Architecture Review: 2 issues found（D3、D4）
- Code Quality Review: 3 issues found（D5、D6、D7）＋2 處文件不一致已直接修正
- Test Review: diagram produced, 20 gaps identified（尚無程式碼）
- Performance Review: 1 issue found（Worker 並行，併入 D3）
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0 items proposed
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0
- Outside voice: codex skipped（全域 CLAUDE.md 暫停 Codex）
- Parallelization: 3 lanes, 2 parallel / 1 sequential
- Lake Score: 0/1（D8 選了 completeness 較低的選項）

## Suppressed findings

無。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 4 | CLEAR | mode: SCOPE_REDUCTION, 0 critical gaps |
| Outside Review | codex (plan-review) | Independent 2nd opinion | 0 | skipped | 使用者全域 CLAUDE.md 暫停 Codex，no completed external review |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN | 26 issues（含 20 個尚未撰寫的測試缺口），0 critical gaps；決策全部已核准 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex，plan-review 階段，skipped（使用者指示暫停），無外部審查結果。
- **VERDICT:** CEO CLEARED。Eng review 狀態為 ISSUES OPEN，原因是有待實作的工作與測試缺口，不是有未決事項；所有決策已核准並寫入 v1.2，可以開始實作（T1–T5）。eng review required（實作完成後以 `/review` 檢查程式碼）。

NO UNRESOLVED DECISIONS

