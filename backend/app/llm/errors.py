"""NOVARK 呼叫錯誤分類，對應設計文件 §3.1.3。

每個錯誤帶有本系統錯誤碼 `code`（寫入 llm_call_logs）與是否可重試。
呼叫端只依 `code`／類別分支，不比對訊息字串。
"""

from __future__ import annotations


class LLMError(Exception):
    code: str = "llm_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        trace_id: str | None = None,
        novark_error_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.trace_id = trace_id
        self.novark_error_code = novark_error_code
        self.http_status = http_status


class LLMMalformedOutput(LLMError):
    """JSON 解析或 Schema 驗證失敗（修復一次後仍失敗）。"""

    code = "llm_malformed_output"


class LLMEmptyOutput(LLMError):
    """模型回覆為空白（重試一次後仍為空）。"""

    code = "llm_empty_output"


class LLMGuardrailBlocked(LLMError):
    """平台入口閘門攔下輸入；不重試、不寫入。"""

    code = "llm_guardrail_blocked"

    def __init__(self, message: str, *, category: str | None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.category = category


class LLMProviderUnavailable(LLMError):
    code = "llm_provider_unavailable"


class LLMTokenBudget(LLMError):
    """單次 token 上限（422）；原樣重送會再失敗。"""

    code = "llm_token_budget"


class LLMRateLimited(LLMError):
    code = "llm_rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class LLMUpstream(LLMError):
    """網路錯誤、逾時或暫時性 5xx。"""

    code = "llm_upstream"

    def __init__(self, message: str, *, retryable: bool = True, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retryable = retryable


class LLMConfigError(LLMError):
    """400／401／402／403／404：設定或授權問題，需立即告警。"""

    code = "llm_config_error"


class LLMBudgetExhausted(LLMError):
    """呼叫端給的時間預算（deadline）已用完，未完成解析。

    聊天路徑據此改以 Reply 回覆「處理較久」（設計文件 §10-4）。
    """

    code = "llm_budget_exhausted"
