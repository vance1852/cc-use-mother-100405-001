"""原始创新研究组合审议领域服务。

在基础服务（主体、角色、幂等、SQLite 事务、哈希审计链）之上，组织：

- 科学问题（含同义术语归并）与问题间依赖图；
- 可证伪主张（建议）、前置发现（带版本的先导证据）、研究路线与资源需求；
- 申请团队、成员与评审人合作关系（利益冲突）；
- 截止后冻结、不可被补交材料改写的评审快照；
- 追加式决定历史（撤回、路线合并、条件立项、复议均保留原决定与依据）；
- 获批组合对预算与稀缺设施的原子占用，以及每轮唯一生效版本；
- 可跨服务重启恢复的多签会签流程。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ResourceContention,
    ReviewQuorumError,
    SubmissionClosed,
    ValidationError,
    WorkflowStateError,
)
from .models import (
    Assignment,
    CountersignStatus,
    DecisionRecord,
    EvidenceVersionInfo,
    Facility,
    Portfolio,
    Proposal,
    Question,
    Round,
    Snapshot,
    Team,
)
from .service import DomainService

APPROVED_OUTCOMES = frozenset({"explore", "validate", "conditional"})
DECISION_OUTCOMES = frozenset({"explore", "validate", "waitlist", "reject", "conditional"})


class ReviewService(DomainService):
    """协调依赖图、快照、回避、决定留痕与组合原子占用。"""

    # ------------------------------------------------------------------ 基础登记

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      name: str, organization_id: str,
                      member_ids: list[str] | None = None) -> Any:
        member_ids = member_ids or []
        payload = {"team_id": team_id, "name": name, "organization_id": organization_id,
                   "member_ids": sorted(member_ids)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            team_id = self._identifier(team_id, "team_id")
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            members = []
            for member_id in member_ids:
                member_id = self._identifier(member_id, "member_id")
                row = connection.execute("SELECT 1 FROM actors WHERE actor_id=? AND active=1",
                                         (member_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"团队成员不存在或已停用: {member_id}")
                members.append(member_id)
            if len(set(members)) != len(members):
                raise ValidationError("团队成员不能重复")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO teams(team_id,name,organization_id,created_at) VALUES(?,?,?,?)",
                        (team_id, name, organization_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                for member_id in members:
                    connection.execute(
                        "INSERT INTO team_members(team_id,actor_id) VALUES(?,?)",
                        (team_id, member_id),
                    )
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"name": name, "organization_id": organization_id,
                                     "member_ids": members}, occurred_at=self._now())
                return "team", team_id, {"team_id": team_id}

            return self._idempotent(connection, request_id=request_id, action="register_team",
                                    payload=payload, create=create)

    def declare_collaboration(self, *, request_id: str, actor_id: str,
                              reviewer_id: str, team_id: str, note: str = "") -> Any:
        """登记评审人与团队的合作关系；既有在途分配将被自动标记为冲突。"""

        payload = {"reviewer_id": reviewer_id, "team_id": team_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reviewer = self._actor(connection, reviewer_id)
            if reviewer.role != "reviewer" and reviewer.role != "admin":
                raise ValidationError("合作关系只能登记在评审身份人员名下")
            if connection.execute("SELECT 1 FROM teams WHERE team_id=?", (team_id,)).fetchone() is None:
                raise NotFoundError("团队不存在")
            note = str(note or "")[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM reviewer_collaborations WHERE reviewer_id=? AND team_id=?",
                    (reviewer_id, team_id),
                ).fetchone()
                if existing:
                    return "collaboration", f"{reviewer_id}:{team_id}", {"reviewer_id": reviewer_id, "team_id": team_id}
                connection.execute(
                    "INSERT INTO reviewer_collaborations(reviewer_id,team_id,note,created_at) VALUES(?,?,?,?)",
                    (reviewer_id, team_id, note, self._now()),
                )
                flipped = []
                rows = connection.execute(
                    "SELECT ra.proposal_id FROM review_assignments ra "
                    "JOIN proposals p ON p.proposal_id = ra.proposal_id "
                    "WHERE ra.reviewer_id=? AND p.team_id=? AND ra.state='assigned'",
                    (reviewer_id, team_id),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "UPDATE review_assignments SET state='conflicted', reason=? "
                        "WHERE proposal_id=? AND reviewer_id=?",
                        ("合作关系事后披露，自动回避", row["proposal_id"], reviewer_id),
                    )
                    flipped.append(row["proposal_id"])
                append_event(connection, actor_id=actor_id, action="collaboration.declared",
                             resource_type="collaboration", resource_id=f"{reviewer_id}:{team_id}",
                             detail={"reviewer_id": reviewer_id, "team_id": team_id,
                                     "auto_conflicted": flipped}, occurred_at=self._now())
                return "collaboration", f"{reviewer_id}:{team_id}", {"reviewer_id": reviewer_id, "team_id": team_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_collaboration", payload=payload, create=create)

    def register_facility(self, *, request_id: str, actor_id: str,
                          facility_id: str, name: str) -> Any:
        payload = {"facility_id": facility_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            facility_id = self._identifier(facility_id, "facility_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO facilities(facility_id,name,created_at) VALUES(?,?,?)",
                        (facility_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设施编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="facility.registered",
                             resource_type="facility", resource_id=facility_id,
                             detail={"name": name}, occurred_at=self._now())
                return "facility", facility_id, {"facility_id": facility_id}

            return self._idempotent(connection, request_id=request_id, action="register_facility",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 证据版本

    def register_evidence(self, *, request_id: str, actor_id: str, evidence_id: str,
                          title: str, data: dict[str, Any], published: bool = False) -> Any:
        if not isinstance(data, dict) or not data:
            raise ValidationError("data 必须是非空对象")
        payload = {"evidence_id": evidence_id, "title": title, "data": data, "published": published}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            evidence_id = self._identifier(evidence_id, "evidence_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM evidence_versions WHERE evidence_id=?",
                                      (evidence_id,)).fetchone():
                    raise ConflictError("证据已存在，新增内容必须使用新版本接口")
                self._insert_evidence_version(connection, evidence_id=evidence_id, version=1,
                                              title=title, data=data, published=published,
                                              actor_id=actor_id)
                return "evidence", f"{evidence_id}:1", {"evidence_id": evidence_id, "version": 1}

            return self._idempotent(connection, request_id=request_id, action="register_evidence",
                                    payload=payload, create=create)

    def new_evidence_version(self, *, request_id: str, actor_id: str, evidence_id: str,
                             title: str, data: dict[str, Any], published: bool = False) -> Any:
        if not isinstance(data, dict) or not data:
            raise ValidationError("data 必须是非空对象")
        payload = {"evidence_id": evidence_id, "title": title, "data": data, "published": published}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            evidence_id = self._identifier(evidence_id, "evidence_id")
            title = self._text(title, "title")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM evidence_versions WHERE evidence_id=?",
                    (evidence_id,),
                ).fetchone()
                if row["version"] is None:
                    raise NotFoundError("证据不存在，需先登记首个版本")
                version = row["version"] + 1
                self._insert_evidence_version(connection, evidence_id=evidence_id, version=version,
                                              title=title, data=data, published=published,
                                              actor_id=actor_id)
                return "evidence", f"{evidence_id}:{version}", {"evidence_id": evidence_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="new_evidence_version", payload=payload, create=create)

    def _insert_evidence_version(self, connection, *, evidence_id: str, version: int,
                                 title: str, data: dict[str, Any], published: bool,
                                 actor_id: str) -> None:
        payload_hash = digest(data)
        connection.execute(
            "INSERT INTO evidence_versions(evidence_id,version,title,published,payload_json,"
            "payload_hash,registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?)",
            (evidence_id, version, title, 1 if published else 0, canonical_json(data),
             payload_hash, actor_id, self._now()),
        )
        append_event(connection, actor_id=actor_id, action="evidence.versioned",
                     resource_type="evidence", resource_id=f"{evidence_id}:{version}",
                     detail={"evidence_id": evidence_id, "version": version,
                             "published": bool(published), "payload_hash": payload_hash},
                     occurred_at=self._now())

    # ------------------------------------------------------------------ 问题依赖图

    def register_question(self, *, request_id: str, actor_id: str, question_id: str,
                          title: str, frontier: str, terms: list[str] | None = None,
                          depends_on: list[str] | None = None) -> Any:
        terms = terms or []
        depends_on = depends_on or []
        payload = {"question_id": question_id, "title": title, "frontier": frontier,
                   "terms": sorted(terms), "depends_on": sorted(depends_on)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            question_id = self._identifier(question_id, "question_id")
            title = self._text(title, "title")
            frontier = self._text(frontier, "frontier")
            norm_terms = [self._text(term, "term", 120) for term in terms]
            norm_deps = [self._identifier(dep, "depends_on") for dep in depends_on]
            if question_id in norm_deps:
                raise ValidationError("科学问题不能依赖自身")
            if len(set(norm_terms)) != len(norm_terms):
                raise ValidationError("同义术语不能重复")

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute("SELECT 1 FROM questions WHERE question_id=?",
                                         (question_id,)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO questions(question_id,title,frontier,created_at) VALUES(?,?,?,?)",
                        (question_id, title, frontier, self._now()),
                    )
                added_terms: list[str] = []
                for term in norm_terms:
                    owner = connection.execute("SELECT question_id FROM question_terms WHERE term=?",
                                               (term,)).fetchone()
                    if owner is not None and owner["question_id"] != question_id:
                        raise ConflictError(f"术语已归并到另一个科学问题: {term}")
                    if owner is None:
                        connection.execute(
                            "INSERT INTO question_terms(term,question_id) VALUES(?,?)",
                            (term, question_id),
                        )
                        added_terms.append(term)
                added_deps: list[str] = []
                for dep in norm_deps:
                    if connection.execute("SELECT 1 FROM questions WHERE question_id=?",
                                          (dep,)).fetchone() is None:
                        raise NotFoundError(f"被依赖的科学问题不存在: {dep}")
                    edge = connection.execute(
                        "SELECT 1 FROM question_dependencies WHERE question_id=? AND depends_on=?",
                        (question_id, dep),
                    ).fetchone()
                    if edge is None:
                        connection.execute(
                            "INSERT INTO question_dependencies(question_id,depends_on) VALUES(?,?)",
                            (question_id, dep),
                        )
                        added_deps.append(dep)
                self._assert_acyclic(connection, question_id)
                append_event(connection, actor_id=actor_id, action="question.registered",
                             resource_type="question", resource_id=question_id,
                             detail={"title": title, "frontier": frontier,
                                     "added_terms": added_terms, "added_dependencies": added_deps},
                             occurred_at=self._now())
                return "question", question_id, {"question_id": question_id}

            return self._idempotent(connection, request_id=request_id, action="register_question",
                                    payload=payload, create=create)

    def _assert_acyclic(self, connection, seed: str) -> None:
        pending = [seed]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == seed and seen:
                raise ValidationError("科学问题依赖图不允许出现环")
            if current in seen:
                continue
            seen.add(current)
            rows = connection.execute(
                "SELECT depends_on FROM question_dependencies WHERE question_id=?", (current,)
            ).fetchall()
            pending.extend(row["depends_on"] for row in rows)

    # ------------------------------------------------------------------ 征集轮次

    def open_round(self, *, request_id: str, actor_id: str, round_id: str, title: str,
                   deadline: str, budget_total: float, min_reviewers: int = 2) -> Any:
        payload = {"round_id": round_id, "title": title, "deadline": deadline,
                   "budget_total": budget_total, "min_reviewers": min_reviewers}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            round_id = self._identifier(round_id, "round_id")
            title = self._text(title, "title")
            deadline = self._text(deadline, "deadline", 80)
            if not isinstance(budget_total, (int, float)) or budget_total < 0:
                raise ValidationError("预算总额必须是非负数字")
            if not isinstance(min_reviewers, int) or min_reviewers < 1:
                raise ValidationError("最低独立评审人数必须是不小于 1 的整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO rounds(round_id,title,deadline,min_reviewers,state,created_at) "
                        "VALUES(?,?,?,?,'open',?)",
                        (round_id, title, deadline, min_reviewers, self._now()),
                    )
                    connection.execute(
                        "INSERT INTO resource_pools(round_id,budget_total) VALUES(?,?)",
                        (round_id, float(budget_total)),
                    )
                except Exception as exc:
                    raise ConflictError("轮次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="round.opened",
                             resource_type="round", resource_id=round_id,
                             detail={"title": title, "deadline": deadline,
                                     "budget_total": float(budget_total),
                                     "min_reviewers": min_reviewers}, occurred_at=self._now())
                return "round", round_id, {"round_id": round_id}

            return self._idempotent(connection, request_id=request_id, action="open_round",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 建议与路线

    def submit_proposal(self, *, request_id: str, actor_id: str, round_id: str,
                        proposal_id: str, team_id: str, question_id: str, title: str,
                        test_criterion: str, budget: float,
                        facility_id: str | None = None,
                        evidence_refs: list[list[str]] | None = None) -> Any:
        evidence_refs = evidence_refs or []
        payload = {"round_id": round_id, "proposal_id": proposal_id, "team_id": team_id,
                   "question_id": question_id, "title": title, "test_criterion": test_criterion,
                   "budget": budget, "facility_id": facility_id,
                   "evidence_refs": sorted([list(ref) for ref in evidence_refs])}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            round_row = self._round_row(connection, round_id)
            if round_row["state"] != "open":
                raise SubmissionClosed("征集已截止，评审快照不得被补交材料改写")
            team_row = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if team_row is None:
                raise NotFoundError("团队不存在")
            if actor.role not in ("admin", "operator"):
                membership = connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id=? AND actor_id=?",
                    (team_id, actor_id),
                ).fetchone()
                if membership is None:
                    raise PermissionDenied("只能由团队成员或项目主管提交建议")
            if connection.execute("SELECT 1 FROM questions WHERE question_id=?",
                                  (question_id,)).fetchone() is None:
                raise NotFoundError("科学问题不存在")
            if facility_id and connection.execute("SELECT 1 FROM facilities WHERE facility_id=?",
                                                   (facility_id,)).fetchone() is None:
                raise NotFoundError("设施不存在")
            proposal_id = self._identifier(proposal_id, "proposal_id")
            title = self._text(title, "title")
            test_criterion = self._text(test_criterion, "test_criterion", 1000)
            if not isinstance(budget, (int, float)) or budget < 0:
                raise ValidationError("预算需求必须是非负数字")
            norm_refs: list[tuple[str, int]] = []
            for ref in evidence_refs:
                if not isinstance(ref, (list, tuple)) or len(ref) != 2:
                    raise ValidationError("证据引用必须是 [evidence_id, version] 形式")
                ref_id = self._identifier(ref[0], "evidence_id")
                try:
                    ref_version = int(ref[1])
                except (TypeError, ValueError) as exc:
                    raise ValidationError("证据版本必须是整数") from exc
                evidence_row = connection.execute(
                    "SELECT published FROM evidence_versions WHERE evidence_id=? AND version=?",
                    (ref_id, ref_version),
                ).fetchone()
                if evidence_row is None:
                    raise NotFoundError(f"证据版本不存在: {ref_id}:{ref_version}")
                norm_refs.append((ref_id, ref_version))
            if len(set(norm_refs)) != len(norm_refs):
                raise ValidationError("同一证据版本不能重复引用")
            content_hash = digest({
                "team_id": team_id, "question_id": question_id, "title": title,
                "test_criterion": test_criterion, "budget": float(budget),
                "facility_id": facility_id, "evidence_refs": [list(ref) for ref in norm_refs],
            })

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO proposals(proposal_id,round_id,team_id,question_id,title,"
                        "test_criterion,budget,facility_id,version,payload_hash,current_outcome,"
                        "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                        (proposal_id, round_id, team_id, question_id, title, test_criterion,
                         float(budget), facility_id, 1, content_hash, actor_id, self._now()),
                    )
                    self._replace_evidence_refs(connection, proposal_id, norm_refs)
                    version = 1
                    changed = True
                else:
                    if existing["round_id"] != round_id:
                        raise ConflictError("建议编号已用于其他轮次")
                    if existing["payload_hash"] == content_hash:
                        return "proposal", proposal_id, {"proposal_id": proposal_id,
                                                         "version": existing["version"], "changed": False}
                    if existing["current_outcome"] == "withdrawn":
                        raise WorkflowStateError("建议已撤回，不能修改")
                    version = existing["version"] + 1
                    connection.execute(
                        "UPDATE proposals SET team_id=?,question_id=?,title=?,test_criterion=?,"
                        "budget=?,facility_id=?,version=?,payload_hash=?,submitted_by=?,submitted_at=? "
                        "WHERE proposal_id=?",
                        (team_id, question_id, title, test_criterion, float(budget), facility_id,
                         version, content_hash, actor_id, self._now(), proposal_id),
                    )
                    self._replace_evidence_refs(connection, proposal_id, norm_refs)
                    changed = True
                append_event(connection, actor_id=actor_id,
                             action="proposal.submitted" if version == 1 and changed
                             else "proposal.amended",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"round_id": round_id, "version": version,
                                     "payload_hash": content_hash,
                                     "unpublished_evidence": self._unpublished_refs(connection, norm_refs)},
                             occurred_at=self._now())
                return "proposal", proposal_id, {"proposal_id": proposal_id,
                                                 "version": version, "changed": changed}

            return self._idempotent(connection, request_id=request_id, action="submit_proposal",
                                    payload=payload, create=create)

    def _replace_evidence_refs(self, connection, proposal_id: str,
                               refs: list[tuple[str, int]]) -> None:
        connection.execute("DELETE FROM proposal_evidence_refs WHERE proposal_id=?", (proposal_id,))
        for position, (evidence_id, version) in enumerate(sorted(refs)):
            connection.execute(
                "INSERT INTO proposal_evidence_refs(proposal_id,evidence_id,evidence_version,position) "
                "VALUES(?,?,?,?)",
                (proposal_id, evidence_id, version, position),
            )

    def _unpublished_refs(self, connection, refs: list[tuple[str, int]]) -> list[list[Any]]:
        result = []
        for evidence_id, version in refs:
            row = connection.execute(
                "SELECT published FROM evidence_versions WHERE evidence_id=? AND version=?",
                (evidence_id, version),
            ).fetchone()
            if row is not None and not row["published"]:
                result.append([evidence_id, version])
        return result

    def add_route(self, *, request_id: str, actor_id: str, proposal_id: str,
                  route_id: str, summary: str) -> Any:
        payload = {"proposal_id": proposal_id, "route_id": route_id, "summary": summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            proposal = self._proposal_row(connection, proposal_id)
            if actor.role not in ("admin", "operator"):
                membership = connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id=? AND actor_id=?",
                    (proposal["team_id"], actor_id),
                ).fetchone()
                if membership is None:
                    raise PermissionDenied("只能由申请团队或项目主管登记研究路线")
            route_id = self._identifier(route_id, "route_id")
            summary = self._text(summary, "summary", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO routes(route_id,proposal_id,summary,merged_from_json,created_at) "
                        "VALUES(?,?,?,'[]',?)",
                        (route_id, proposal_id, summary, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("路线编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="route.added",
                             resource_type="route", resource_id=route_id,
                             detail={"proposal_id": proposal_id}, occurred_at=self._now())
                return "route", route_id, {"route_id": route_id}

            return self._idempotent(connection, request_id=request_id, action="add_route",
                                    payload=payload, create=create)

    def withdraw_proposal(self, *, request_id: str, actor_id: str,
                          proposal_id: str, reason: str) -> Any:
        payload = {"proposal_id": proposal_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            proposal = self._proposal_row(connection, proposal_id)
            if actor.role not in ("admin", "operator"):
                membership = connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id=? AND actor_id=?",
                    (proposal["team_id"], actor_id),
                ).fetchone()
                if membership is None:
                    raise PermissionDenied("只能由申请团队或项目主管撤回建议")
            reason = self._text(reason, "reason", 1000)
            self._assert_not_allocated(connection, proposal_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if proposal["current_outcome"] == "withdrawn":
                    raise WorkflowStateError("建议已经撤回")
                snapshot_id = self._snapshot_id_for(connection, proposal["round_id"])
                decision_id = self._append_decision(
                    connection, actor_id=actor_id, proposal_id=proposal_id,
                    snapshot_id=snapshot_id, outcome="withdrawn",
                    basis={"reason": reason, "previous_outcome": proposal["current_outcome"]},
                )
                connection.execute(
                    "UPDATE proposals SET current_outcome='withdrawn' WHERE proposal_id=?",
                    (proposal_id,),
                )
                return "decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id, action="withdraw_proposal",
                                    payload=payload, create=create)

    def merge_routes(self, *, request_id: str, actor_id: str,
                     source_proposal_id: str, target_proposal_id: str, reason: str) -> Any:
        """把源建议的研究路线并入目标建议；原决定仍在历史中保留。"""

        payload = {"source_proposal_id": source_proposal_id,
                   "target_proposal_id": target_proposal_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            source = self._proposal_row(connection, source_proposal_id)
            target = self._proposal_row(connection, target_proposal_id)
            if source["round_id"] != target["round_id"]:
                raise ValidationError("只能合并同一轮次内的研究路线")
            if source["current_outcome"] == "merged":
                raise WorkflowStateError("源建议已经并入其他路线")
            self._assert_not_allocated(connection, source_proposal_id)
            reason = self._text(reason, "reason", 1000)
            source_routes = connection.execute(
                "SELECT route_id, summary FROM routes WHERE proposal_id=? AND merged_into IS NULL",
                (source_proposal_id,),
            ).fetchall()
            if not source_routes:
                raise ValidationError("源建议没有可合并的研究路线")

            def create() -> tuple[str, str, dict[str, Any]]:
                merged_ids = [row["route_id"] for row in source_routes]
                connection.execute(
                    "UPDATE routes SET merged_into=? WHERE proposal_id=? AND merged_into IS NULL",
                    (target_proposal_id, source_proposal_id),
                )
                target_existing = connection.execute(
                    "SELECT merged_from_json FROM routes WHERE proposal_id=? LIMIT 1",
                    (target_proposal_id,),
                ).fetchone()
                merged_from = json.loads(target_existing["merged_from_json"]) if target_existing else []
                merged_from.extend(merged_ids)
                connection.execute(
                    "UPDATE routes SET merged_from_json=? WHERE proposal_id=?",
                    (canonical_json(merged_from), target_proposal_id),
                )
                snapshot_id = self._snapshot_id_for(connection, source["round_id"])
                decision_id = self._append_decision(
                    connection, actor_id=actor_id, proposal_id=source_proposal_id,
                    snapshot_id=snapshot_id, outcome="merged",
                    basis={"reason": reason, "merged_into": target_proposal_id,
                           "route_ids": merged_ids,
                           "previous_outcome": source["current_outcome"]},
                )
                connection.execute(
                    "UPDATE proposals SET current_outcome='merged' WHERE proposal_id=?",
                    (source_proposal_id,),
                )
                append_event(connection, actor_id=actor_id, action="routes.merged",
                             resource_type="proposal", resource_id=source_proposal_id,
                             detail={"target_proposal_id": target_proposal_id,
                                     "route_ids": merged_ids}, occurred_at=self._now())
                return "decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id, action="merge_routes",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 快照冻结

    def close_round(self, *, request_id: str, actor_id: str, round_id: str) -> Any:
        payload = {"round_id": round_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            round_row = self._round_row(connection, round_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if round_row["state"] != "open":
                    raise WorkflowStateError("轮次已经截止")
                items = connection.execute(
                    "SELECT proposal_id, version, payload_hash FROM proposals "
                    "WHERE round_id=? AND COALESCE(current_outcome,'')<>'withdrawn' "
                    "ORDER BY proposal_id",
                    (round_id,),
                ).fetchall()
                baseline = digest([
                    {"proposal_id": row["proposal_id"], "version": row["version"],
                     "payload_hash": row["payload_hash"]} for row in items
                ])
                snapshot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_snapshots(snapshot_id,round_id,baseline_hash,item_count,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, round_id, baseline, len(items), actor_id, self._now()),
                )
                for row in items:
                    connection.execute(
                        "INSERT INTO review_snapshot_items(snapshot_id,proposal_id,version,payload_hash) "
                        "VALUES(?,?,?,?)",
                        (snapshot_id, row["proposal_id"], row["version"], row["payload_hash"]),
                    )
                connection.execute("UPDATE rounds SET state='closed' WHERE round_id=?", (round_id,))
                append_event(connection, actor_id=actor_id, action="round.closed",
                             resource_type="snapshot", resource_id=snapshot_id,
                             detail={"round_id": round_id, "item_count": len(items),
                                     "baseline_hash": baseline}, occurred_at=self._now())
                return "snapshot", snapshot_id, {"snapshot_id": snapshot_id,
                                                 "item_count": len(items)}

            return self._idempotent(connection, request_id=request_id, action="close_round",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 评审回避

    def assign_reviewer(self, *, request_id: str, actor_id: str,
                        proposal_id: str, reviewer_id: str) -> Any:
        payload = {"proposal_id": proposal_id, "reviewer_id": reviewer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            proposal = self._proposal_row(connection, proposal_id)
            reviewer = self._actor(connection, reviewer_id)
            if reviewer.role != "reviewer":
                raise ValidationError("只能分配评审角色人员")
            conflict = connection.execute(
                "SELECT 1 FROM reviewer_collaborations WHERE reviewer_id=? AND team_id=?",
                (reviewer_id, proposal["team_id"]),
            ).fetchone()
            if not conflict:
                conflict = connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id=? AND actor_id=?",
                    (proposal["team_id"], reviewer_id),
                ).fetchone()
            if conflict:
                raise PermissionDenied("评审人与申请团队存在合作关系或同属申请团队，必须回避")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT state FROM review_assignments WHERE proposal_id=? AND reviewer_id=?",
                    (proposal_id, reviewer_id),
                ).fetchone()
                if existing is not None:
                    if existing["state"] == "assigned":
                        return "assignment", f"{proposal_id}:{reviewer_id}", {"proposal_id": proposal_id}
                    raise ConflictError("该评审人已被标记为回避或冲突，不能恢复分配")
                connection.execute(
                    "INSERT INTO review_assignments(proposal_id,reviewer_id,state,reason,assigned_at) "
                    "VALUES(?,?,'assigned','',?)",
                    (proposal_id, reviewer_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="reviewer.assigned",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"reviewer_id": reviewer_id}, occurred_at=self._now())
                return "assignment", f"{proposal_id}:{reviewer_id}", {"proposal_id": proposal_id}

            return self._idempotent(connection, request_id=request_id, action="assign_reviewer",
                                    payload=payload, create=create)

    def recuse_reviewer(self, *, request_id: str, actor_id: str,
                        proposal_id: str, reviewer_id: str, reason: str) -> Any:
        """评审人回避；决定作出前会重新核对独立评审人数。"""

        payload = {"proposal_id": proposal_id, "reviewer_id": reviewer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.actor_id != reviewer_id:
                self._require(actor, "admin", "operator")
            proposal = self._proposal_row(connection, proposal_id)
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT state FROM review_assignments WHERE proposal_id=? AND reviewer_id=?",
                    (proposal_id, reviewer_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("该评审人未被分配到本建议")
                if row["state"] == "recused":
                    return "assignment", f"{proposal_id}:{reviewer_id}", {"proposal_id": proposal_id}
                connection.execute(
                    "UPDATE review_assignments SET state='recused', reason=? "
                    "WHERE proposal_id=? AND reviewer_id=?",
                    (reason, proposal_id, reviewer_id),
                )
                remaining = self._independent_reviewer_count(connection, proposal_id)
                append_event(connection, actor_id=actor_id, action="reviewer.recused",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"reviewer_id": reviewer_id, "reason": reason,
                                     "independent_reviewers_remaining": remaining},
                             occurred_at=self._now())
                return "assignment", f"{proposal_id}:{reviewer_id}", {"proposal_id": proposal_id}

            return self._idempotent(connection, request_id=request_id, action="recuse_reviewer",
                                    payload=payload, create=create)

    def _independent_reviewer_count(self, connection, proposal_id: str) -> int:
        return connection.execute(
            "SELECT COUNT(*) AS count FROM review_assignments WHERE proposal_id=? AND state='assigned'",
            (proposal_id,),
        ).fetchone()["count"]

    def submit_score(self, *, request_id: str, actor_id: str, proposal_id: str,
                     score: int, comment: str = "") -> Any:
        payload = {"proposal_id": proposal_id, "score": score, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role != "reviewer":
                raise PermissionDenied("只有评审人可以评分")
            assignment = connection.execute(
                "SELECT state FROM review_assignments WHERE proposal_id=? AND reviewer_id=?",
                (proposal_id, actor_id),
            ).fetchone()
            if assignment is None:
                raise NotFoundError("评审人未被分配到本建议")
            if assignment["state"] != "assigned":
                raise PermissionDenied("评审人已回避，不能提交评分")
            if not isinstance(score, int) or not 0 <= score <= 100:
                raise ValidationError("评分必须是 0 到 100 的整数")
            comment = str(comment or "")[:2000]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO review_scores(proposal_id,reviewer_id,score,comment,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(proposal_id,reviewer_id) DO UPDATE SET "
                    "score=excluded.score, comment=excluded.comment, created_at=excluded.created_at",
                    (proposal_id, actor_id, score, comment, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="score.submitted",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"reviewer_id": actor_id, "score": score},
                             occurred_at=self._now())
                return "score", f"{proposal_id}:{actor_id}", {"proposal_id": proposal_id}

            return self._idempotent(connection, request_id=request_id, action="submit_score",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 决定与复议

    def decide_proposal(self, *, request_id: str, actor_id: str, proposal_id: str,
                        outcome: str, note: str = "",
                        conditions: list[str] | None = None) -> Any:
        conditions = conditions or []
        payload = {"proposal_id": proposal_id, "outcome": outcome, "note": note,
                   "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            proposal = self._proposal_row(connection, proposal_id)
            if outcome not in DECISION_OUTCOMES:
                raise ValidationError("决定结果不在允许范围内")
            if outcome == "conditional" and not conditions:
                raise ValidationError("条件立项必须给出条件")
            conditions = [self._text(condition, "condition", 500) for condition in conditions]
            note = self._text(note, "note", 2000, allow_empty=True)
            snapshot_row = connection.execute(
                "SELECT * FROM review_snapshots WHERE round_id=?", (proposal["round_id"],)
            ).fetchone()
            if snapshot_row is None:
                raise WorkflowStateError("评审快照尚未形成，不能作出决定")
            item = connection.execute(
                "SELECT 1 FROM review_snapshot_items WHERE snapshot_id=? AND proposal_id=?",
                (snapshot_row["snapshot_id"], proposal_id),
            ).fetchone()
            if item is None:
                raise NotFoundError("建议不在评审快照中（可能在截止前已撤回）")
            if proposal["current_outcome"] in ("withdrawn", "merged"):
                raise WorkflowStateError("建议已撤回或合并，不能作出新决定")
            if proposal["current_outcome"] is not None:
                raise WorkflowStateError("建议已经作出初审决定，改变结果必须走复议流程")
            independent = self._independent_reviewer_count(connection, proposal_id)
            min_reviewers = self._round_row(connection, proposal["round_id"])["min_reviewers"]
            if independent < min_reviewers:
                raise ReviewQuorumError(
                    f"回避后独立评审人数为 {independent}，未达到最低要求 "
                    f"{min_reviewers}，需重新分配")

            def create() -> tuple[str, str, dict[str, Any]]:
                basis = self._build_basis(connection, proposal=proposal,
                                          snapshot=snapshot_row, note=note, conditions=conditions)
                decision_id = self._append_decision(
                    connection, actor_id=actor_id, proposal_id=proposal_id,
                    snapshot_id=snapshot_row["snapshot_id"], outcome=outcome, basis=basis,
                )
                connection.execute(
                    "UPDATE proposals SET current_outcome=? WHERE proposal_id=?",
                    (outcome, proposal_id),
                )
                append_event(connection, actor_id=actor_id, action="proposal.decided",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"decision_id": decision_id, "outcome": outcome,
                                     "snapshot_id": snapshot_row["snapshot_id"]},
                             occurred_at=self._now())
                return "decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id, action="decide_proposal",
                                    payload=payload, create=create)

    def reconsider(self, *, request_id: str, actor_id: str, proposal_id: str,
                   outcome: str, reason: str, conditions: list[str] | None = None) -> Any:
        """复议仍以冻结快照为依据，原决定与适用依据完整保留。"""

        conditions = conditions or []
        payload = {"proposal_id": proposal_id, "outcome": outcome, "reason": reason,
                   "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            proposal = self._proposal_row(connection, proposal_id)
            if outcome not in DECISION_OUTCOMES:
                raise ValidationError("决定结果不在允许范围内")
            reason = self._text(reason, "reason", 2000)
            conditions = [self._text(condition, "condition", 500) for condition in conditions]
            previous = connection.execute(
                "SELECT * FROM decisions WHERE proposal_id=? ORDER BY sequence DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            if previous is None:
                raise WorkflowStateError("尚未作出过决定，不能复议；请先作出初审决定")
            if proposal["current_outcome"] in ("withdrawn", "merged"):
                raise WorkflowStateError("建议已撤回或合并，不能复议")
            self._assert_not_allocated(connection, proposal_id)
            snapshot_row = connection.execute(
                "SELECT * FROM review_snapshots WHERE round_id=?", (proposal["round_id"],)
            ).fetchone()
            # 复议前也可能出现新的回避，须重新满足独立评审人数
            independent = self._independent_reviewer_count(connection, proposal_id)
            min_reviewers = self._round_row(connection, proposal["round_id"])["min_reviewers"]
            if independent < min_reviewers:
                raise ReviewQuorumError(
                    f"回避后独立评审人数为 {independent}，未达到最低要求 {min_reviewers}，"
                    "需重新分配后再复议")

            def create() -> tuple[str, str, dict[str, Any]]:
                basis = self._build_basis(connection, proposal=proposal, snapshot=snapshot_row,
                                          note=reason, conditions=conditions, reconsideration=True)
                basis["previous_decision_id"] = previous["decision_id"]
                basis["previous_outcome"] = previous["outcome"]
                decision_id = self._append_decision(
                    connection, actor_id=actor_id, proposal_id=proposal_id,
                    snapshot_id=snapshot_row["snapshot_id"], outcome=outcome, basis=basis,
                    supersedes=previous["decision_id"],
                )
                connection.execute(
                    "UPDATE proposals SET current_outcome=? WHERE proposal_id=?",
                    (outcome, proposal_id),
                )
                append_event(connection, actor_id=actor_id, action="proposal.reconsidered",
                             resource_type="proposal", resource_id=proposal_id,
                             detail={"decision_id": decision_id, "outcome": outcome,
                                     "supersedes": previous["decision_id"],
                                     "snapshot_id": snapshot_row["snapshot_id"]},
                             occurred_at=self._now())
                return "decision", decision_id, {"decision_id": decision_id}

            return self._idempotent(connection, request_id=request_id, action="reconsider",
                                    payload=payload, create=create)

    def _build_basis(self, connection, *, proposal, snapshot, note: str,
                     conditions: list[str], reconsideration: bool = False) -> dict[str, Any]:
        score_rows = connection.execute(
            "SELECT s.reviewer_id,s.score,s.comment,ra.state AS assignment_state "
            "FROM review_scores s JOIN review_assignments ra "
            "ON ra.proposal_id=s.proposal_id AND ra.reviewer_id=s.reviewer_id "
            "WHERE s.proposal_id=? ORDER BY s.reviewer_id",
            (proposal["proposal_id"],),
        ).fetchall()
        scores = [
            {"reviewer_id": row["reviewer_id"], "score": row["score"], "comment": row["comment"]}
            for row in score_rows if row["assignment_state"] == "assigned"
        ]
        excluded_scores = [
            {"reviewer_id": row["reviewer_id"], "score": row["score"],
             "assignment_state": row["assignment_state"]}
            for row in score_rows if row["assignment_state"] != "assigned"
        ]
        refs = []
        for row in connection.execute(
                "SELECT evidence_id,evidence_version AS version FROM proposal_evidence_refs "
                "WHERE proposal_id=? ORDER BY position", (proposal["proposal_id"],)):
            published = connection.execute(
                "SELECT published FROM evidence_versions WHERE evidence_id=? AND version=?",
                (row["evidence_id"], row["version"]),
            ).fetchone()
            refs.append({"evidence_id": row["evidence_id"], "version": row["version"],
                         "published": bool(published and published["published"])})
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "baseline_hash": snapshot["baseline_hash"],
            "proposal_version": proposal["version"],
            "payload_hash": proposal["payload_hash"],
            "scores": scores,
            "excluded_conflicted_scores": excluded_scores,
            "independent_reviewers": self._independent_reviewer_count(
                connection, proposal["proposal_id"]),
            "evidence_refs": refs,
            "unpublished_evidence": [f"{ref['evidence_id']}:{ref['version']}"
                                     for ref in refs if not ref["published"]],
            "note": note,
            "conditions": conditions,
            "reconsideration": reconsideration,
        }

    def _append_decision(self, connection, *, actor_id: str, proposal_id: str,
                         snapshot_id: str | None, outcome: str, basis: dict[str, Any],
                         supersedes: str | None = None) -> str:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) AS sequence FROM decisions WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        decision_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO decisions(decision_id,sequence,proposal_id,snapshot_id,outcome,basis_json,"
            "supersedes,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision_id, row["sequence"] + 1, proposal_id, snapshot_id, outcome,
             canonical_json(basis), supersedes, actor_id, self._now()),
        )
        return decision_id

    # ------------------------------------------------------------------ 组合与会签

    def propose_portfolio(self, *, request_id: str, actor_id: str, round_id: str,
                          proposal_ids: list[str],
                          supersedes_portfolio_id: str | None = None) -> Any:
        payload = {"round_id": round_id, "proposal_ids": sorted(proposal_ids),
                   "supersedes_portfolio_id": supersedes_portfolio_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._round_row(connection, round_id)
            if supersedes_portfolio_id is not None:
                supersedes_portfolio_id = self._identifier(supersedes_portfolio_id,
                                                           "supersedes_portfolio_id")
                target = self._portfolio_row(connection, supersedes_portfolio_id)
                if target["round_id"] != round_id:
                    raise ValidationError("被取代组合与新组合不在同一轮次")
            ids = [self._identifier(pid, "proposal_id") for pid in proposal_ids]
            if len(set(ids)) != len(ids):
                raise ValidationError("组合内建议不能重复")
            if not ids:
                raise ValidationError("组合至少包含一项建议")
            total_budget = 0.0
            facilities: set[str] = set()
            for pid in ids:
                row = connection.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?", (pid,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"建议不存在: {pid}")
                if row["round_id"] != round_id:
                    raise ValidationError(f"建议不属于本轮次: {pid}")
                if row["current_outcome"] not in APPROVED_OUTCOMES:
                    raise WorkflowStateError(
                        f"建议 {pid} 当前结果为 {row['current_outcome']}，未进入探索/验证/条件立项")
                total_budget += float(row["budget"])
                if row["facility_id"]:
                    if row["facility_id"] in facilities:
                        raise ResourceContention(f"设施 {row['facility_id']} 在组合内被两项建议争抢")
                    facilities.add(row["facility_id"])
            pool = connection.execute(
                "SELECT budget_total FROM resource_pools WHERE round_id=?", (round_id,),
            ).fetchone()
            if total_budget > pool["budget_total"] + 1e-9:
                raise ResourceContention(
                    f"组合预算 {total_budget} 超出本轮预算总额 {pool['budget_total']}")

            def create() -> tuple[str, str, dict[str, Any]]:
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM portfolios WHERE round_id=?",
                    (round_id,),
                ).fetchone()
                version = version_row["version"] + 1
                portfolio_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO portfolios(portfolio_id,round_id,version,state,"
                    "supersedes_portfolio_id,created_by,created_at) "
                    "VALUES(?,?,?,'draft',?,?,?)",
                    (portfolio_id, round_id, version, supersedes_portfolio_id,
                     actor_id, self._now()),
                )
                for pid in ids:
                    connection.execute(
                        "INSERT INTO portfolio_items(portfolio_id,proposal_id) VALUES(?,?)",
                        (portfolio_id, pid),
                    )
                append_event(connection, actor_id=actor_id, action="portfolio.proposed",
                             resource_type="portfolio", resource_id=portfolio_id,
                             detail={"round_id": round_id, "version": version,
                                     "proposal_ids": ids, "budget": total_budget,
                                     "facilities": sorted(facilities),
                                     "supersedes_portfolio_id": supersedes_portfolio_id},
                             occurred_at=self._now())
                return "portfolio", portfolio_id, {"portfolio_id": portfolio_id, "version": version}

            return self._idempotent(connection, request_id=request_id, action="propose_portfolio",
                                    payload=payload, create=create)

    def start_countersign(self, *, request_id: str, actor_id: str,
                          portfolio_id: str, signer_ids: list[str]) -> Any:
        payload = {"portfolio_id": portfolio_id, "signer_ids": sorted(signer_ids)}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            portfolio = self._portfolio_row(connection, portfolio_id)
            if portfolio["state"] != "draft":
                raise WorkflowStateError("只有草稿组合可以发起会签")
            signers = [self._identifier(sid, "signer_id") for sid in signer_ids]
            if not signers or len(set(signers)) != len(signers):
                raise ValidationError("会签人不能为空且不能重复")
            for sid in signers:
                signer = connection.execute("SELECT 1 FROM actors WHERE actor_id=? AND active=1",
                                            (sid,)).fetchone()
                if signer is None:
                    raise NotFoundError(f"会签人不存在或已停用: {sid}")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE portfolios SET state='countersigning' WHERE portfolio_id=?",
                    (portfolio_id,),
                )
                connection.execute(
                    "INSERT INTO countersign_workflows(portfolio_id,state,created_by,created_at) "
                    "VALUES(?, 'pending', ?, ?)",
                    (portfolio_id, actor_id, self._now()),
                )
                for position, sid in enumerate(signers):
                    connection.execute(
                        "INSERT INTO countersign_signatures(portfolio_id,signer_id,position,state) "
                        "VALUES(?,?,?,'pending')",
                        (portfolio_id, sid, position),
                    )
                append_event(connection, actor_id=actor_id, action="countersign.started",
                             resource_type="portfolio", resource_id=portfolio_id,
                             detail={"signer_ids": signers}, occurred_at=self._now())
                return "countersign", portfolio_id, {"portfolio_id": portfolio_id}

            return self._idempotent(connection, request_id=request_id, action="start_countersign",
                                    payload=payload, create=create)

    def countersign(self, *, request_id: str, actor_id: str, portfolio_id: str,
                    approve: bool, comment: str = "") -> Any:
        payload = {"portfolio_id": portfolio_id, "approve": approve, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            portfolio = self._portfolio_row(connection, portfolio_id)
            signature = connection.execute(
                "SELECT * FROM countersign_signatures WHERE portfolio_id=? AND signer_id=?",
                (portfolio_id, actor_id),
            ).fetchone()
            if signature is None:
                raise NotFoundError("当前操作者不是本组合的会签人")
            comment = str(comment or "")[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                workflow = connection.execute(
                    "SELECT state FROM countersign_workflows WHERE portfolio_id=?", (portfolio_id,),
                ).fetchone()
                if workflow["state"] != "pending":
                    raise WorkflowStateError("会签流程已经结束")
                if signature["state"] != "pending":
                    raise WorkflowStateError("当前会签人已经签署")
                now = self._now()
                if approve:
                    connection.execute(
                        "UPDATE countersign_signatures SET state='signed', request_id=?, "
                        "comment=?, signed_at=? WHERE portfolio_id=? AND signer_id=?",
                        (request_id, comment, now, portfolio_id, actor_id),
                    )
                    pending = connection.execute(
                        "SELECT COUNT(*) AS count FROM countersign_signatures "
                        "WHERE portfolio_id=? AND state='pending'", (portfolio_id,),
                    ).fetchone()["count"]
                    became_effective = False
                    if pending == 0:
                        self._finalize_effective(connection, portfolio=portfolio, actor_id=actor_id)
                        connection.execute(
                            "UPDATE countersign_workflows SET state='signed', completed_at=? "
                            "WHERE portfolio_id=?", (now, portfolio_id),
                        )
                        became_effective = True
                    append_event(connection, actor_id=actor_id, action="countersign.signed",
                                 resource_type="portfolio", resource_id=portfolio_id,
                                 detail={"signer_id": actor_id, "became_effective": became_effective},
                                 occurred_at=now)
                    return "countersign", portfolio_id, {"portfolio_id": portfolio_id,
                                                         "effective": became_effective}
                connection.execute(
                    "UPDATE countersign_signatures SET state='rejected', request_id=?, "
                    "comment=?, signed_at=? WHERE portfolio_id=? AND signer_id=?",
                    (request_id, comment, now, portfolio_id, actor_id),
                )
                connection.execute(
                    "UPDATE countersign_workflows SET state='rejected', completed_at=? "
                    "WHERE portfolio_id=?", (now, portfolio_id),
                )
                connection.execute(
                    "UPDATE portfolios SET state='rejected' WHERE portfolio_id=?",
                    (portfolio_id,),
                )
                append_event(connection, actor_id=actor_id, action="countersign.rejected",
                             resource_type="portfolio", resource_id=portfolio_id,
                             detail={"signer_id": actor_id, "comment": comment}, occurred_at=now)
                return "countersign", portfolio_id, {"portfolio_id": portfolio_id, "effective": False}

            return self._idempotent(connection, request_id=request_id, action="countersign",
                                    payload=payload, create=create)

    def _finalize_effective(self, connection, *, portfolio, actor_id: str) -> None:
        """在同一事务内原子取得预算/设施并让组合唯一生效。

        - 若该轮次已有生效组合，只有在组合提案时显式声明 ``supersedes_portfolio_id``
          指向它，才允许在本事务内释放旧占用并取而代之；否则竞争审批被拒绝，
          保证任何时刻都只有一个生效版本。
        - 释放与取得处于同一事务：随后的预算/设施校验若失败，整体回滚，
          旧组合的占用随之恢复。
        """

        round_id = portfolio["round_id"]
        portfolio_id = portfolio["portfolio_id"]
        declared_target = portfolio["supersedes_portfolio_id"]
        items = connection.execute(
            "SELECT p.proposal_id,p.budget,p.facility_id FROM portfolio_items pi "
            "JOIN proposals p ON p.proposal_id = pi.proposal_id "
            "WHERE pi.portfolio_id=? ORDER BY p.proposal_id", (portfolio_id,),
        ).fetchall()
        total_budget = sum(float(row["budget"]) for row in items)
        pool = connection.execute(
            "SELECT budget_total FROM resource_pools WHERE round_id=?", (round_id,),
        ).fetchone()
        previous_effective = connection.execute(
            "SELECT portfolio_id FROM portfolios WHERE round_id=? AND state='effective'",
            (round_id,),
        ).fetchone()
        superseded_id = None
        if previous_effective:
            current_id = previous_effective["portfolio_id"]
            if declared_target != current_id:
                raise ResourceContention(
                    "本轮已有生效组合，资源被其原子占用；竞争审批不能产生第二个生效版本，"
                    "请以组合调整形式显式声明取代目标")
            superseded_id = current_id
            connection.execute(
                "UPDATE resource_allocations SET released=1 WHERE portfolio_id=? AND released=0",
                (superseded_id,),
            )
            connection.execute(
                "UPDATE portfolios SET state='superseded' WHERE portfolio_id=?",
                (superseded_id,),
            )
        reserved = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS amount FROM resource_allocations "
            "WHERE pool_round_id=? AND released=0", (round_id,),
        ).fetchone()["amount"]
        if reserved + total_budget > pool["budget_total"] + 1e-9:
            raise ResourceContention(
                f"预算已被生效组合占用 {reserved}，本组合需要 {total_budget}，"
                f"超过总额 {pool['budget_total']}")
        facility_ids = [row["facility_id"] for row in items if row["facility_id"]]
        if facility_ids:
            placeholders = ",".join("?" for _ in facility_ids)
            clash = connection.execute(
                f"SELECT facility_id FROM resource_allocations WHERE released=0 "
                f"AND facility_id IN ({placeholders}) LIMIT 1", facility_ids,
            ).fetchone()
            if clash:
                raise ResourceContention(f"设施 {clash['facility_id']} 已被另一个生效组合原子占用")
        proposal_clash = connection.execute(
            "SELECT proposal_id FROM resource_allocations WHERE released=0 AND proposal_id IN "
            "(SELECT proposal_id FROM portfolio_items WHERE portfolio_id=?) LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        if proposal_clash:
            raise ResourceContention(f"建议 {proposal_clash['proposal_id']} 已在生效组合中占用资源")
        for row in items:
            connection.execute(
                "INSERT INTO resource_allocations(allocation_id,portfolio_id,proposal_id,"
                "pool_round_id,amount,facility_id,released,created_at) VALUES(?,?,?,?,?,?,0,?)",
                (uuid.uuid4().hex, portfolio_id, row["proposal_id"], round_id,
                 float(row["budget"]), row["facility_id"], self._now()),
            )
        connection.execute(
            "UPDATE portfolios SET state='effective' WHERE portfolio_id=?", (portfolio_id,),
        )
        append_event(connection, actor_id=actor_id, action="portfolio.effective",
                     resource_type="portfolio", resource_id=portfolio_id,
                     detail={"round_id": round_id, "budget": total_budget,
                             "superseded": superseded_id,
                             "proposal_ids": [row["proposal_id"] for row in items]},
                     occurred_at=self._now())

    # ------------------------------------------------------------------ 查询

    def _round_row(self, connection, round_id: str):
        row = connection.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("轮次不存在")
        return row

    def _proposal_row(self, connection, proposal_id: str):
        row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                 (proposal_id,)).fetchone()
        if row is None:
            raise NotFoundError("建议不存在")
        return row

    def _portfolio_row(self, connection, portfolio_id: str):
        row = connection.execute("SELECT * FROM portfolios WHERE portfolio_id=?",
                                 (portfolio_id,)).fetchone()
        if row is None:
            raise NotFoundError("组合不存在")
        return row

    def _snapshot_id_for(self, connection, round_id: str) -> str | None:
        row = connection.execute(
            "SELECT snapshot_id FROM review_snapshots WHERE round_id=?", (round_id,)
        ).fetchone()
        return row["snapshot_id"] if row else None

    def _assert_not_allocated(self, connection, proposal_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM resource_allocations WHERE proposal_id=? AND released=0 LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if row:
            raise WorkflowStateError("建议的资源已被生效组合占用，须先调整组合后再操作")

    def _text(self, value: str, field: str, limit: int = 200, allow_empty: bool = False) -> str:
        value = str(value if value is not None else "").strip()
        if not allow_empty and not value:
            raise ValidationError(f"{field} 不能为空")
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def get_team(self, team_id: str) -> Team:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFoundError("团队不存在")
        members = tuple(r["actor_id"] for r in connection.execute(
            "SELECT actor_id FROM team_members WHERE team_id=? ORDER BY actor_id", (team_id,)))
        return Team(row["team_id"], row["name"], row["organization_id"], members)

    def get_evidence(self, evidence_id: str) -> list[EvidenceVersionInfo]:
        rows = self.database.connection.execute(
            "SELECT * FROM evidence_versions WHERE evidence_id=? ORDER BY version",
            (evidence_id,),
        ).fetchall()
        if not rows:
            raise NotFoundError("证据不存在")
        return [EvidenceVersionInfo(row["evidence_id"], row["version"], row["title"],
                                    bool(row["published"]), json.loads(row["payload_json"]),
                                    row["registered_by"], row["registered_at"]) for row in rows]

    def get_question(self, question_id: str) -> Question:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM questions WHERE question_id=?",
                                 (question_id,)).fetchone()
        if row is None:
            raise NotFoundError("科学问题不存在")
        terms = tuple(r["term"] for r in connection.execute(
            "SELECT term FROM question_terms WHERE question_id=? ORDER BY term", (question_id,)))
        deps = tuple(r["depends_on"] for r in connection.execute(
            "SELECT depends_on FROM question_dependencies WHERE question_id=? ORDER BY depends_on",
            (question_id,)))
        return Question(row["question_id"], row["title"], row["frontier"], terms, deps)

    def get_facility(self, facility_id: str) -> Facility:
        row = self.database.connection.execute(
            "SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return Facility(row["facility_id"], row["name"])

    def get_round(self, round_id: str) -> Round:
        row = self._round_row(self.database.connection, round_id)
        return Round(row["round_id"], row["title"], row["deadline"],
                     row["min_reviewers"], row["state"])

    def get_proposal(self, proposal_id: str) -> Proposal:
        connection = self.database.connection
        row = self._proposal_row(connection, proposal_id)
        refs = tuple((r["evidence_id"], r["evidence_version"]) for r in connection.execute(
            "SELECT evidence_id,evidence_version FROM proposal_evidence_refs "
            "WHERE proposal_id=? ORDER BY position", (proposal_id,)))
        return Proposal(row["proposal_id"], row["round_id"], row["team_id"],
                        row["question_id"], row["title"], row["test_criterion"],
                        float(row["budget"]), row["facility_id"], row["version"], refs,
                        row["current_outcome"], row["submitted_by"], row["submitted_at"])

    def get_snapshot(self, round_id: str) -> Snapshot:
        connection = self.database.connection
        row = connection.execute("SELECT * FROM review_snapshots WHERE round_id=?",
                                 (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("该轮次尚未形成评审快照")
        items = tuple(
            {"proposal_id": item["proposal_id"], "version": item["version"],
             "payload_hash": item["payload_hash"]}
            for item in connection.execute(
                "SELECT proposal_id,version,payload_hash FROM review_snapshot_items "
                "WHERE snapshot_id=? ORDER BY proposal_id", (row["snapshot_id"],)))
        return Snapshot(row["snapshot_id"], row["round_id"], row["baseline_hash"],
                        row["item_count"], row["created_by"], row["created_at"], items)

    def list_assignments(self, proposal_id: str) -> list[Assignment]:
        self._proposal_row(self.database.connection, proposal_id)
        return [Assignment(row["proposal_id"], row["reviewer_id"], row["state"], row["reason"])
                for row in self.database.connection.execute(
                    "SELECT * FROM review_assignments WHERE proposal_id=? ORDER BY reviewer_id",
                    (proposal_id,))]

    def list_decisions(self, proposal_id: str) -> list[DecisionRecord]:
        self._proposal_row(self.database.connection, proposal_id)
        return [DecisionRecord(row["decision_id"], row["sequence"], row["proposal_id"],
                               row["snapshot_id"], row["outcome"], json.loads(row["basis_json"]),
                               row["supersedes"], row["decided_by"], row["decided_at"])
                for row in self.database.connection.execute(
                    "SELECT * FROM decisions WHERE proposal_id=? ORDER BY sequence",
                    (proposal_id,))]

    def get_portfolio(self, portfolio_id: str) -> Portfolio:
        connection = self.database.connection
        row = self._portfolio_row(connection, portfolio_id)
        ids = tuple(r["proposal_id"] for r in connection.execute(
            "SELECT proposal_id FROM portfolio_items WHERE portfolio_id=? ORDER BY proposal_id",
            (portfolio_id,)))
        return Portfolio(row["portfolio_id"], row["round_id"], row["version"], row["state"],
                         row["created_by"], row["created_at"], ids)

    def get_countersign(self, portfolio_id: str) -> CountersignStatus:
        """返回会签流程状态；服务重启后据此继续未结束的会签。"""

        connection = self.database.connection
        self._portfolio_row(connection, portfolio_id)
        workflow = connection.execute(
            "SELECT state FROM countersign_workflows WHERE portfolio_id=?", (portfolio_id,),
        ).fetchone()
        if workflow is None:
            raise NotFoundError("组合尚未发起会签")
        signers = [
            {"signer_id": row["signer_id"], "position": row["position"], "state": row["state"],
             "comment": row["comment"], "signed_at": row["signed_at"]}
            for row in connection.execute(
                "SELECT * FROM countersign_signatures WHERE portfolio_id=? ORDER BY position",
                (portfolio_id,))
        ]
        return CountersignStatus(portfolio_id, workflow["state"], tuple(signers))

    def explain_decision(self, proposal_id: str) -> dict[str, Any]:
        """解释建议为何进入探索、验证、候补或拒绝等状态。"""

        connection = self.database.connection
        proposal = self._proposal_row(connection, proposal_id)
        question = self.get_question(proposal["question_id"])
        decisions = self.list_decisions(proposal_id)
        assignments = self.list_assignments(proposal_id)
        snapshot_row = connection.execute(
            "SELECT * FROM review_snapshots WHERE round_id=?", (proposal["round_id"],)
        ).fetchone()
        snapshot_fresh = None
        if snapshot_row:
            item = connection.execute(
                "SELECT payload_hash FROM review_snapshot_items WHERE snapshot_id=? AND proposal_id=?",
                (snapshot_row["snapshot_id"], proposal_id),
            ).fetchone()
            snapshot_fresh = bool(item and item["payload_hash"] == proposal["payload_hash"])
        latest = decisions[-1] if decisions else None
        reasons: list[str] = []
        if latest:
            basis = latest.basis
            scores = basis.get("scores", [])
            if scores:
                average = round(sum(s["score"] for s in scores) / len(scores), 1)
                reasons.append(f"独立评审平均分 {average}（{basis.get('independent_reviewers')} 人）")
            if basis.get("unpublished_evidence"):
                reasons.append("依据引用了尚未公开的先导数据版本: "
                               + ", ".join(basis["unpublished_evidence"]))
            if basis.get("conditions"):
                reasons.append("附带条件: " + "；".join(basis["conditions"]))
            if basis.get("note"):
                reasons.append(f"审议说明: {basis['note']}")
            if basis.get("reconsideration"):
                reasons.append(f"复议决定，推翻 {basis.get('previous_outcome')} "
                               f"（原决定 {basis.get('previous_decision_id')} 仍保留）")
        if question.depends_on:
            reasons.append("依赖的前置科学问题: " + ", ".join(question.depends_on))
        return {
            "proposal_id": proposal_id,
            "title": proposal["title"],
            "current_outcome": proposal["current_outcome"],
            "question": {"question_id": question.question_id, "title": question.title,
                         "frontier": question.frontier, "terms": list(question.terms),
                         "depends_on": list(question.depends_on)},
            "snapshot": None if snapshot_row is None else {
                "snapshot_id": snapshot_row["snapshot_id"],
                "baseline_hash": snapshot_row["baseline_hash"],
                "matches_current_version": snapshot_fresh,
            },
            "assignments": {"assigned": [a.reviewer_id for a in assignments if a.state == "assigned"],
                            "recused": [a.reviewer_id for a in assignments if a.state == "recused"],
                            "conflicted": [a.reviewer_id for a in assignments
                                           if a.state == "conflicted"]},
            "decision_history": [
                {"sequence": decision.sequence, "decision_id": decision.decision_id,
                 "outcome": decision.outcome, "snapshot_id": decision.snapshot_id,
                 "supersedes": decision.supersedes, "basis": decision.basis,
                 "decided_by": decision.decided_by, "decided_at": decision.decided_at}
                for decision in decisions
            ],
            "reasons": reasons,
        }

    def dependency_graph(self, round_id: str | None = None) -> dict[str, Any]:
        """输出科学问题依赖图与术语归并、建议映射。"""

        connection = self.database.connection
        if round_id:
            self._round_row(connection, round_id)
        questions = []
        for row in connection.execute(
                "SELECT q.* FROM questions q ORDER BY q.question_id"):
            terms = [r["term"] for r in connection.execute(
                "SELECT term FROM question_terms WHERE question_id=? ORDER BY term",
                (row["question_id"],))]
            deps = [r["depends_on"] for r in connection.execute(
                "SELECT depends_on FROM question_dependencies WHERE question_id=? ORDER BY depends_on",
                (row["question_id"],))]
            proposal_query = (
                "SELECT proposal_id,team_id,current_outcome FROM proposals WHERE question_id=?")
            params: list[Any] = [row["question_id"]]
            if round_id:
                proposal_query += " AND round_id=?"
                params.append(round_id)
            proposal_query += " ORDER BY proposal_id"
            proposals = [dict(p) for p in connection.execute(proposal_query, params)]
            questions.append({"question_id": row["question_id"], "title": row["title"],
                              "frontier": row["frontier"], "terms": terms,
                              "depends_on": deps, "proposals": proposals})
        return {"round_id": round_id, "questions": questions}

    def compare_portfolios(self, portfolio_id_a: str, portfolio_id_b: str) -> dict[str, Any]:
        """比较两个组合方案对前沿覆盖与关键依赖的影响。"""

        connection = self.database.connection
        a = self.get_portfolio(portfolio_id_a)
        b = self.get_portfolio(portfolio_id_b)
        if a.round_id != b.round_id:
            raise ValidationError("只能比较同一轮次的组合方案")

        def describe(portfolio: Portfolio) -> dict[str, Any]:
            rows = connection.execute(
                "SELECT p.proposal_id,p.question_id,p.budget,p.facility_id,p.current_outcome,"
                "q.frontier FROM portfolio_items pi "
                "JOIN proposals p ON p.proposal_id = pi.proposal_id "
                "JOIN questions q ON q.question_id = p.question_id "
                "WHERE pi.portfolio_id=?", (portfolio.portfolio_id,)).fetchall()
            question_ids = {row["question_id"] for row in rows}
            frontiers = {row["frontier"] for row in rows}
            dependency_edges = connection.execute(
                "SELECT question_id,depends_on FROM question_dependencies "
                "WHERE question_id IN (%s)" % ",".join("?" for _ in question_ids),
                tuple(question_ids),
            ).fetchall() if question_ids else []
            covered_deps = {(row["question_id"], row["depends_on"]) for row in dependency_edges
                            if row["depends_on"] in question_ids}
            duplicated_questions = sorted(
                qid for qid in question_ids
                if sum(1 for row in rows if row["question_id"] == qid) > 1)
            return {
                "portfolio_id": portfolio.portfolio_id,
                "version": portfolio.version,
                "state": portfolio.state,
                "proposal_ids": sorted(portfolio.proposal_ids),
                "budget": round(sum(float(row["budget"]) for row in rows), 6),
                "facilities": sorted({row["facility_id"] for row in rows if row["facility_id"]}),
                "questions_covered": sorted(question_ids),
                "frontiers_covered": sorted(frontiers),
                "frontier_count": len(frontiers),
                "dependency_edges_total": len(dependency_edges),
                "dependency_edges_covered": len(covered_deps),
                "duplicated_question_investments": duplicated_questions,
                "outcome_mix": {outcome: sum(1 for row in rows
                                             if row["current_outcome"] == outcome)
                                for outcome in sorted(APPROVED_OUTCOMES)},
            }

        left, right = describe(a), describe(b)
        return {
            "round_id": a.round_id,
            "a": left,
            "b": right,
            "frontier_delta": {
                "gained": sorted(set(right["frontiers_covered"]) - set(left["frontiers_covered"])),
                "lost": sorted(set(left["frontiers_covered"]) - set(right["frontiers_covered"])),
            },
            "question_delta": {
                "gained": sorted(set(right["questions_covered"]) - set(left["questions_covered"])),
                "lost": sorted(set(left["questions_covered"]) - set(right["questions_covered"])),
            },
            "budget_delta": round(right["budget"] - left["budget"], 6),
            "dependency_coverage_delta": (
                right["dependency_edges_covered"] - left["dependency_edges_covered"]),
            "shared_proposals": sorted(set(left["proposal_ids"]) & set(right["proposal_ids"])),
        }
