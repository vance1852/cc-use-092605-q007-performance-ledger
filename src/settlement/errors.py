"""经营结算服务向 API 和 CLI 暴露的稳定错误。"""


class SettlementError(RuntimeError):
    code = "settlement_error"
    status = 400


class NotFound(SettlementError):
    code = "not_found"
    status = 404


class Conflict(SettlementError):
    code = "conflict"
    status = 409


class Forbidden(SettlementError):
    code = "forbidden"
    status = 403


class InvalidState(SettlementError):
    code = "invalid_state"
    status = 409


class ValidationFailed(SettlementError):
    code = "validation_failed"
    status = 422
