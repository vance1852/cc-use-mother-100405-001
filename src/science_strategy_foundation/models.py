"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示科研创新机构下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Team:
    """申请团队及其成员。"""

    team_id: str
    name: str
    organization_id: str
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceVersionInfo:
    """先导证据的一个不可变版本。"""

    evidence_id: str
    version: int
    title: str
    published: bool
    payload: dict[str, Any]
    registered_by: str
    registered_at: str


@dataclass(frozen=True)
class Question:
    """归并了同义术语的科学问题节点。"""

    question_id: str
    title: str
    frontier: str
    terms: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class Facility:
    """稀缺设施。"""

    facility_id: str
    name: str


@dataclass(frozen=True)
class Round:
    """一次征集轮次。"""

    round_id: str
    title: str
    deadline: str
    min_reviewers: int
    state: str


@dataclass(frozen=True)
class Proposal:
    """一份研究建议在当前版本下的完整内容。"""

    proposal_id: str
    round_id: str
    team_id: str
    question_id: str
    title: str
    test_criterion: str
    budget: float
    facility_id: str | None
    version: int
    evidence_refs: tuple[tuple[str, int], ...]
    current_outcome: str | None
    submitted_by: str
    submitted_at: str


@dataclass(frozen=True)
class Snapshot:
    """征集截止后冻结的评审快照。"""

    snapshot_id: str
    round_id: str
    baseline_hash: str
    item_count: int
    created_by: str
    created_at: str
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Assignment:
    """评审人对一份建议的分配状态。"""

    proposal_id: str
    reviewer_id: str
    state: str
    reason: str


@dataclass(frozen=True)
class DecisionRecord:
    """对一份建议作出的一项历史决定。"""

    decision_id: str
    sequence: int
    proposal_id: str
    snapshot_id: str | None
    outcome: str
    basis: dict[str, Any]
    supersedes: str | None
    decided_by: str
    decided_at: str


@dataclass(frozen=True)
class Portfolio:
    """一个获批组合及其建议条目。"""

    portfolio_id: str
    round_id: str
    version: int
    state: str
    created_by: str
    created_at: str
    proposal_ids: tuple[str, ...]


@dataclass(frozen=True)
class CountersignStatus:
    """会签流程的持久化状态，可跨重启恢复。"""

    portfolio_id: str
    state: str
    signers: tuple[dict[str, Any], ...]
