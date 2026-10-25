"""服務層錯誤；API 層依 http_status 對應回應（設計文件 §9.2）。"""

from __future__ import annotations


class ServiceError(Exception):
    http_status = 400
    code = "service_error"


class NotFound(ServiceError):
    """不存在，或不對外揭露（例如他人的草稿）。"""

    http_status = 404
    code = "not_found"


class MemberInactive(ServiceError):
    http_status = 403
    code = "member_inactive"


class DraftVersionConflict(ServiceError):
    """確認卡或表單的 draft_version 已過時。"""

    http_status = 409
    code = "draft_version_conflict"


class EventVersionConflict(ServiceError):
    """目標行程在草稿建立後被修改或取消。"""

    http_status = 409
    code = "event_version_conflict"


class DraftGone(ServiceError):
    """草稿已過期或已放棄。"""

    http_status = 410
    code = "draft_gone"


class InvalidDraft(ServiceError):
    """欄位不完整或不合法，無法確認。"""

    http_status = 422
    code = "invalid_draft"


class RangeTooLarge(ServiceError):
    http_status = 422
    code = "range_too_large"
