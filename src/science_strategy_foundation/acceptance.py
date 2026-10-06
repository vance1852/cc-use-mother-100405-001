"""运行基础服务与研究组合审议的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ResourceContention, ReviewQuorumError, SubmissionClosed
from .review import ReviewService
from .storage import Database


def _foundation_chain(service: ReviewService) -> dict[str, object]:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="示范科研机构")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                           display_name="项目负责人", role="operator", organization_id="org-001")
    service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                          organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
    first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                       category="institution_profile", external_key="record-001",
                                       data={"name": "基础资料", "enabled": True})
    replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                        category="institution_profile", external_key="record-001",
                                        data={"name": "基础资料", "enabled": True})
    records = service.list_domain_data("site-001")
    return {"records": len(records), "first_replayed": first.replayed,
            "second_replayed": replay.replayed}


def _review_chain(database_path: Path) -> tuple[dict[str, object], ReviewService, Database]:
    service = ReviewService(Database(database_path),
                            FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))
    for rid, aid, name, role in [
        ("req-op", "director", "项目主管", "operator"),
        ("req-rv1", "rv-a", "评审甲", "reviewer"),
        ("req-rv2", "rv-b", "评审乙", "reviewer"),
        ("req-rv3", "rv-c", "评审丙", "reviewer"),
        ("req-p1", "pi-a", "首席甲", "operator"),
        ("req-p2", "pi-b", "首席乙", "operator"),
        ("req-s1", "signer-a", "会签人甲", "admin"),
        ("req-s2", "signer-b", "会签人乙", "operator"),
    ]:
        service.register_actor(request_id=rid, actor_id="admin-001", new_actor_id=aid,
                               display_name=name, role=role, organization_id="org-001")
    service.register_team(request_id="req-team-a", actor_id="director", team_id="team-a",
                          name="拓扑催化团队", organization_id="org-001", member_ids=["pi-a"])
    service.register_team(request_id="req-team-b", actor_id="director", team_id="team-b",
                          name="原位表征团队", organization_id="org-001", member_ids=["pi-b"])
    service.register_facility(request_id="req-fac", actor_id="director",
                              facility_id="fac-light", name="先进光源")
    service.register_evidence(request_id="req-ev1", actor_id="pi-a", evidence_id="pilot-1",
                              title="共享先导数据", data={"signal": 0.42}, published=False)
    service.register_question(request_id="req-q1", actor_id="director", question_id="q-catalysis",
                              title="拓扑催化机理", frontier="量子材料",
                              terms=["拓扑催化", "topological catalysis"])
    service.register_question(request_id="req-q2", actor_id="director", question_id="q-imaging",
                              title="原位动态表征", frontier="精密测量",
                              depends_on=["q-catalysis"])
    service.open_round(request_id="req-round", actor_id="director", round_id="round-2026",
                       title="2026 基础研究轮次", deadline="2026-10-31T23:59:59Z",
                       budget_total=1000.0, min_reviewers=2)
    service.submit_proposal(request_id="req-prop-a", actor_id="pi-a", round_id="round-2026",
                            proposal_id="prop-a", team_id="team-a", question_id="q-catalysis",
                            title="拓扑催化可证伪验证", test_criterion="若改性后转化率不升则证伪",
                            budget=300.0, facility_id="fac-light",
                            evidence_refs=[["pilot-1", 1]])
    service.submit_proposal(request_id="req-prop-b", actor_id="pi-b", round_id="round-2026",
                            proposal_id="prop-b", team_id="team-b", question_id="q-imaging",
                            title="原位表征路线", test_criterion="若时间分辨率未达阈值则证伪",
                            budget=400.0)
    service.add_route(request_id="req-route-a", actor_id="pi-a", proposal_id="prop-a",
                      route_id="route-a", summary="材料改性与对照实验")
    service.add_route(request_id="req-route-b", actor_id="pi-b", proposal_id="prop-b",
                      route_id="route-b", summary="高时间分辨成像")
    # 截止前允许修订并产生新版本
    service.submit_proposal(request_id="req-prop-a2", actor_id="pi-a", round_id="round-2026",
                            proposal_id="prop-a", team_id="team-a", question_id="q-catalysis",
                            title="拓扑催化可证伪验证（修订）", test_criterion="若改性后转化率不升则证伪",
                            budget=300.0, facility_id="fac-light",
                            evidence_refs=[["pilot-1", 1]])
    service.close_round(request_id="req-close", actor_id="director", round_id="round-2026")
    late_blocked = False
    try:
        service.submit_proposal(request_id="req-late", actor_id="pi-a", round_id="round-2026",
                                proposal_id="prop-a", team_id="team-a", question_id="q-catalysis",
                                title="截止后补交", test_criterion="x", budget=1.0)
    except SubmissionClosed:
        late_blocked = True

    # 利益冲突：rv-a 与 team-a 有合作关系，禁止分配
    service.declare_collaboration(request_id="req-col", actor_id="director",
                                  reviewer_id="rv-a", team_id="team-a", note="联合发表")
    service.assign_reviewer(request_id="req-aa1", actor_id="director",
                            proposal_id="prop-a", reviewer_id="rv-b")
    service.assign_reviewer(request_id="req-aa2", actor_id="director",
                            proposal_id="prop-a", reviewer_id="rv-c")
    service.assign_reviewer(request_id="req-ab1", actor_id="director",
                            proposal_id="prop-b", reviewer_id="rv-a")
    service.assign_reviewer(request_id="req-ab2", actor_id="director",
                            proposal_id="prop-b", reviewer_id="rv-b")
    # 事后披露 rv-b 与 team-b 合作 → 既有分配自动冲突，独立评审人数不足
    service.declare_collaboration(request_id="req-col2", actor_id="director",
                                  reviewer_id="rv-b", team_id="team-b", note="共享设备")
    quorum_enforced = False
    try:
        service.decide_proposal(request_id="req-dec-b0", actor_id="director",
                                proposal_id="prop-b", outcome="explore")
    except ReviewQuorumError:
        quorum_enforced = True
    service.assign_reviewer(request_id="req-ab3", actor_id="director",
                            proposal_id="prop-b", reviewer_id="rv-c")
    service.submit_score(request_id="req-sc1", actor_id="rv-b", proposal_id="prop-a", score=88)
    service.submit_score(request_id="req-sc2", actor_id="rv-c", proposal_id="prop-a", score=72)
    service.submit_score(request_id="req-sc3", actor_id="rv-a", proposal_id="prop-b", score=81)
    service.submit_score(request_id="req-sc4", actor_id="rv-c", proposal_id="prop-b", score=69)
    service.decide_proposal(request_id="req-dec-a", actor_id="director",
                            proposal_id="prop-a", outcome="validate")
    service.decide_proposal(request_id="req-dec-b", actor_id="director",
                            proposal_id="prop-b", outcome="explore")

    portfolio = service.propose_portfolio(
        request_id="req-pf1", actor_id="director", round_id="round-2026",
        proposal_ids=["prop-a", "prop-b"])
    service.start_countersign(request_id="req-cs1", actor_id="director",
                              portfolio_id=portfolio.resource_id, signer_ids=["signer-a", "signer-b"])
    service.countersign(request_id="req-sign1", actor_id="signer-a",
                        portfolio_id=portfolio.resource_id, approve=True)
    result = {
        "late_amendment_blocked": late_blocked,
        "quorum_enforced": quorum_enforced,
        "portfolio_id": portfolio.resource_id,
    }
    database = service.database
    return result, service, database


def run() -> dict[str, object]:
    """执行基础登记与完整审议链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acceptance.sqlite3"
        database = Database(path)
        service = ReviewService(database,
                                FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        summary = _foundation_chain(service)
        database.close()

        review_summary, _, review_database = _review_chain(path)
        portfolio_id = str(review_summary.pop("portfolio_id"))
        review_database.close()

        # 服务重启：重新打开同一数据库，继续尚未结束的会签
        restarted = Database(path)
        service2 = ReviewService(restarted,
                                 FixedClock(datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)))
        status = service2.get_countersign(portfolio_id)
        resumed = status.state == "pending"
        service2.countersign(request_id="req-sign2", actor_id="signer-b",
                             portfolio_id=portfolio_id, approve=True)
        effective = service2.get_portfolio(portfolio_id).state == "effective"

        # 竞争组合：未显式声明取代目标时，即使会签通过也不能原子取得资源
        rogue = service2.propose_portfolio(
            request_id="req-pf3", actor_id="director", round_id="round-2026",
            proposal_ids=["prop-a", "prop-b"])
        service2.start_countersign(request_id="req-cs3", actor_id="director",
                                   portfolio_id=rogue.resource_id, signer_ids=["signer-a"])
        contention_seen = False
        try:
            service2.countersign(request_id="req-sign3", actor_id="signer-a",
                                 portfolio_id=rogue.resource_id, approve=True)
        except ResourceContention:
            contention_seen = True
        # 显式声明取代旧组合的调整版本，在同一事务内释放并取得资源
        alt = service2.propose_portfolio(
            request_id="req-pf2", actor_id="director", round_id="round-2026",
            proposal_ids=["prop-b"], supersedes_portfolio_id=portfolio_id)
        service2.start_countersign(request_id="req-cs2", actor_id="director",
                                   portfolio_id=alt.resource_id, signer_ids=["signer-a"])
        service2.countersign(request_id="req-sign-alt", actor_id="signer-a",
                             portfolio_id=alt.resource_id, approve=True)
        superseded = service2.get_portfolio(portfolio_id).state == "superseded"
        alt_effective = service2.get_portfolio(alt.resource_id).state == "effective"

        # 撤回保留原决定；复议保留原依据
        service2.withdraw_proposal(request_id="req-withdraw", actor_id="pi-a",
                                   proposal_id="prop-a", reason="团队主动撤回")
        decisions_a = service2.list_decisions("prop-a")
        explanation = service2.explain_decision("prop-b")
        comparison = service2.compare_portfolios(portfolio_id, alt.resource_id)
        valid, event_count = service2.verify_audit()
        restarted.close()

    return {
        "status": "ok",
        **summary,
        "audit_events": event_count,
        "audit_valid": valid,
        "snapshot_frozen": review_summary["late_amendment_blocked"],
        "quorum_enforced": review_summary["quorum_enforced"],
        "countersign_resumed_after_restart": resumed,
        "portfolio_effective": effective,
        "resource_contention_blocked": contention_seen,
        "previous_portfolio_superseded": superseded,
        "new_portfolio_effective": alt_effective,
        "withdrawn_original_decision_kept": [d.outcome for d in decisions_a] == ["validate", "withdrawn"],
        "explain_outcome": explanation["current_outcome"],
        "comparison_frontier_lost": comparison["frontier_delta"]["lost"],
    }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected_true = [
        "audit_valid", "snapshot_frozen", "quorum_enforced",
        "countersign_resumed_after_restart", "portfolio_effective",
        "resource_contention_blocked", "previous_portfolio_superseded",
        "new_portfolio_effective", "withdrawn_original_decision_kept",
    ]
    ok = result["status"] == "ok" and all(result[key] is True for key in expected_true)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
