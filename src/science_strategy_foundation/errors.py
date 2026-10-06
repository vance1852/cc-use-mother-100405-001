"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class SubmissionClosed(ConflictError):
    """征集已截止或快照已冻结，补交材料不能静默改写评审依据。"""

    code = "submission_closed"


class ReviewQuorumError(ConflictError):
    """利益冲突回避后独立评审人数不再满足要求。"""

    code = "review_quorum"


class ResourceContention(ConflictError):
    """预算或稀缺设施被另一个生效组合占用，无法原子取得。"""

    code = "resource_contention"


class WorkflowStateError(ConflictError):
    """对象当前状态不允许该动作（重复生效、会签已结束等）。"""

    code = "workflow_state"
