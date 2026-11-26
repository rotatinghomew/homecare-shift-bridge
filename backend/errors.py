"""后端统一异常。业务规则结果通常用 ``{ok: False, code, ...}`` 表达；
真正的编程/资源错误才抛出这些异常，HTTP 层负责映射状态码。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class CareError(Exception):
    code = "care_error"
    http_status = 400

    def __init__(self, message: Optional[str] = None, **details: Any):
        super().__init__(message or self.__class__.__doc__ or self.code)
        self.message = message or ""
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        payload = {"ok": False, "code": self.code, "error": self.message or self.code}
        if self.details:
            payload["details"] = self.details
        return payload


class NotFoundError(CareError):
    code = "not_found"
    http_status = 404


class AuthorizationDenied(CareError):
    code = "authorization_denied"
    http_status = 403


class ValidationFailed(CareError):
    code = "validation_failed"
    http_status = 422

    def __init__(self, message: Optional[str] = None, issues=None, **details):
        super().__init__(message, **details)
        self.issues = list(issues or [])

    def to_dict(self):
        data = super().to_dict()
        if self.issues:
            data["issues"] = self.issues
        return data


class ConflictError(CareError):
    code = "conflict"
    http_status = 409
