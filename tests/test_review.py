"""研究组合审议领域的回归测试。"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import (
    ConflictError,
    PermissionDenied,
    ResourceContention,
    ReviewQuorumError,
    SubmissionClosed,
    ValidationError,
    WorkflowStateError,
)
from science_strategy_foundation.review import ReviewService
from science_strategy_foundation.storage import Database


def clock_at(day: int = 1):
    return FixedClock(datetime(2026, 10, day, tzinfo=timezone.utc))


class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ReviewService(self.database, clock_at())
        self.bootstrap_full(self.service)

    def bootstrap_full(self, svc):
        svc.register_organization(request_id="org", actor_id="bootstrap",
                                  organization_id="o1", name="国家实验室")
        svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
            ("op-r", "op", "项目主管", "operator"),
            ("r1-r", "rev1", "评审一", "reviewer"),
            ("r2-r", "rev2", "评审二", "reviewer"),
            ("r3-r", "rev3", "评审三", "reviewer"),
            ("r4-r", "rev-coi", "评审-内部成员", "reviewer"),
            ("a1-r", "app1", "申请人一", "operator"),
            ("a2-r", "app2", "申请人二", "operator"),
            ("b1-r", "boss1", "会签人一", "admin"),
            ("b2-r", "boss2", "会签人二", "operator"),
        ]:
            svc.register_actor(request_id=rid, actor_id="admin", new_actor_id=aid,
                               display_name=name, role=role, organization_id="o1")
        svc.register_team(request_id="team-a", actor_id="op", team_id="team-a",
                          name="甲队", organization_id="o1",
                          member_ids=["app1", "rev-coi"])
        svc.register_team(request_id="team-b", actor_id="op", team_id="team-b",
                          name="乙队", organization_id="o1", member_ids=["app2"])
        svc.register_facility(request_id="fac", actor_id="op",
                              facility_id="fac-x", name="极紫外光源")

    def seed_round_and_proposals(self, svc=None, *, budget_a=300.0, budget_b=400.0,
                                 facility_a="fac-x", facility_b=None, close=True):
        svc = svc or self.service
        svc.register_evidence(request_id="ev", actor_id="app1", evidence_id="ev-pilot",
                              title="先导数据", data={"measurement": 1}, published=False)
        svc.register_question(request_id="q1", actor_id="op", question_id="q-topo",
                              title="拓扑催化", frontier="量子材料",
                              terms=["拓扑催化", "topological catalysis"])
        svc.register_question(request_id="q2", actor_id="op", question_id="q-inst",
                              title="原位表征", frontier="精密测量",
                              depends_on=["q-topo"])
        svc.open_round(request_id="round", actor_id="op", round_id="round-1",
                       title="2026 秋", deadline="2026-10-31",
                       budget_total=1000.0, min_reviewers=2)
        svc.submit_proposal(request_id="pa", actor_id="app1", round_id="round-1",
                            proposal_id="prop-a", team_id="team-a", question_id="q-topo",
                            title="建议甲", test_criterion="若 X 未出现则证伪",
                            budget=budget_a, facility_id=facility_a,
                            evidence_refs=[["ev-pilot", 1]])
        svc.submit_proposal(request_id="pb", actor_id="app2", round_id="round-1",
                            proposal_id="prop-b", team_id="team-b", question_id="q-inst",
                            title="建议乙", test_criterion="若 A 未出现则证伪",
                            budget=budget_b, facility_id=facility_b)
        svc.add_route(request_id="ra", actor_id="app1", proposal_id="prop-a",
                      route_id="route-a", summary="路线甲")
        svc.add_route(request_id="rb", actor_id="app2", proposal_id="prop-b",
                      route_id="route-b", summary="路线乙")
        if close:
            svc.close_round(request_id="close", actor_id="op", round_id="round-1")

    def assign_and_score(self, svc=None):
        svc = svc or self.service
        svc.assign_reviewer(request_id="aa1", actor_id="op", proposal_id="prop-a", reviewer_id="rev2")
        svc.assign_reviewer(request_id="aa2", actor_id="op", proposal_id="prop-a", reviewer_id="rev3")
        svc.assign_reviewer(request_id="ab1", actor_id="op", proposal_id="prop-b", reviewer_id="rev1")
        svc.assign_reviewer(request_id="ab2", actor_id="op", proposal_id="prop-b", reviewer_id="rev2")
        svc.submit_score(request_id="sa1", actor_id="rev2", proposal_id="prop-a", score=88)
        svc.submit_score(request_id="sa2", actor_id="rev3", proposal_id="prop-a", score=60)
        svc.submit_score(request_id="sb1", actor_id="rev1", proposal_id="prop-b", score=70)
        svc.submit_score(request_id="sb2", actor_id="rev2", proposal_id="prop-b", score=75)

    def decide_both(self, svc=None):
        svc = svc or self.service
        self.assign_and_score(svc)
        svc.decide_proposal(request_id="da", actor_id="op",
                            proposal_id="prop-a", outcome="validate")
        svc.decide_proposal(request_id="db", actor_id="op",
                            proposal_id="prop-b", outcome="explore")

    def effective_portfolio(self, request_prefix, proposals, signers=("boss1", "boss2"),
                            svc=None, supersedes=None):
        svc = svc or self.service
        receipt = svc.propose_portfolio(
            request_id=request_prefix + "-p", actor_id="op",
            round_id="round-1", proposal_ids=proposals,
            supersedes_portfolio_id=supersedes)
        portfolio_id = receipt.resource_id
        svc.start_countersign(request_id=request_prefix + "-s", actor_id="op",
                              portfolio_id=portfolio_id, signer_ids=list(signers))
        for index, signer in enumerate(signers):
            svc.countersign(request_id=f"{request_prefix}-g{index}", actor_id=signer,
                            portfolio_id=portfolio_id, approve=True)
        return portfolio_id, svc.get_portfolio(portfolio_id)

    def tearDown(self):
        self.database.close()


class SnapshotFreezeTest(ReviewTestBase):
    def test_amendment_after_close_is_rejected_not_silently_applied(self):
        self.seed_round_and_proposals()
        with self.assertRaises(SubmissionClosed):
            self.service.submit_proposal(
                request_id="late", actor_id="app1", round_id="round-1",
                proposal_id="prop-a", team_id="team-a", question_id="q-topo",
                title="建议甲-补交", test_criterion="若 X 未出现则证伪",
                budget=999, facility_id="fac-x", evidence_refs=[["ev-pilot", 1]])
        proposal = self.service.get_proposal("prop-a")
        self.assertEqual("建议甲", proposal.title)
        self.assertEqual(300.0, proposal.budget)
        self.assertEqual(2, self.service.get_snapshot("round-1").item_count)

    def test_new_evidence_version_does_not_rewrite_pinned_ref_or_snapshot(self):
        self.seed_round_and_proposals()
        self.service.new_evidence_version(
            request_id="ev2", actor_id="app1", evidence_id="ev-pilot",
            title="先导数据-修订", data={"measurement": 2}, published=True)
        snapshot = self.service.get_snapshot("round-1")
        proposal = self.service.get_proposal("prop-a")
        self.assertEqual((("ev-pilot", 1),), proposal.evidence_refs)
        item_hash = next(i["payload_hash"] for i in snapshot.items
                         if i["proposal_id"] == "prop-a")
        stored_hash = self.database.connection.execute(
            "SELECT payload_hash FROM proposals WHERE proposal_id='prop-a'").fetchone()["payload_hash"]
        self.assertEqual(item_hash, stored_hash)

    def test_amendment_before_close_creates_new_version(self):
        self.seed_round_and_proposals(close=False)
        receipt = self.service.submit_proposal(
            request_id="pa2", actor_id="app1", round_id="round-1",
            proposal_id="prop-a", team_id="team-a", question_id="q-topo",
            title="建议甲-修订", test_criterion="若 X 未出现则证伪",
            budget=320, facility_id="fac-x", evidence_refs=[["ev-pilot", 1]])
        self.assertEqual(2, self.service.get_proposal("prop-a").version)
        self.assertFalse(receipt.replayed)


class ConflictAndQuorumTest(ReviewTestBase):
    def test_collaborator_and_team_member_cannot_be_assigned(self):
        self.seed_round_and_proposals()
        self.service.declare_collaboration(request_id="col", actor_id="op",
                                           reviewer_id="rev1", team_id="team-a")
        with self.assertRaises(PermissionDenied):
            self.service.assign_reviewer(request_id="x1", actor_id="op",
                                         proposal_id="prop-a", reviewer_id="rev1")
        with self.assertRaises(PermissionDenied):
            self.service.assign_reviewer(request_id="x2", actor_id="op",
                                         proposal_id="prop-a", reviewer_id="rev-coi")

    def test_late_collaboration_disclosure_recuses_existing_assignment_and_quorum_blocks_decision(self):
        self.seed_round_and_proposals()
        self.assign_and_score()
        self.service.declare_collaboration(request_id="col-b", actor_id="op",
                                           reviewer_id="rev2", team_id="team-b")
        assignments = {a.reviewer_id: a.state
                       for a in self.service.list_assignments("prop-b")}
        self.assertEqual("conflicted", assignments["rev2"])
        with self.assertRaises(ReviewQuorumError):
            self.service.decide_proposal(request_id="dec-b", actor_id="op",
                                         proposal_id="prop-b", outcome="explore")
        # 重新满足独立评审人数后可以作出决定
        self.service.assign_reviewer(request_id="ab3", actor_id="op",
                                     proposal_id="prop-b", reviewer_id="rev3")
        self.service.submit_score(request_id="sb3", actor_id="rev3",
                                  proposal_id="prop-b", score=80)
        self.service.decide_proposal(request_id="dec-b2", actor_id="op",
                                     proposal_id="prop-b", outcome="explore")
        self.assertEqual("explore", self.service.get_proposal("prop-b").current_outcome)

    def test_conflicted_reviewer_score_is_rejected(self):
        self.seed_round_and_proposals()
        self.service.assign_reviewer(request_id="aa1", actor_id="op",
                                     proposal_id="prop-a", reviewer_id="rev2")
        self.service.declare_collaboration(request_id="col-a", actor_id="op",
                                           reviewer_id="rev2", team_id="team-a")
        with self.assertRaises(PermissionDenied):
            self.service.submit_score(request_id="s", actor_id="rev2",
                                      proposal_id="prop-a", score=50)


class DecisionHistoryTest(ReviewTestBase):
    def test_withdraw_preserves_original_decision_and_basis(self):
        self.seed_round_and_proposals()
        self.decide_both()
        self.service.withdraw_proposal(request_id="wd", actor_id="app1",
                                       proposal_id="prop-a", reason="团队主动放弃")
        history = self.service.list_decisions("prop-a")
        self.assertEqual(["validate", "withdrawn"], [d.outcome for d in history])
        self.assertIsNone(history[0].supersedes)
        self.assertIn("baseline_hash", history[0].basis)
        self.assertEqual("withdrawn", self.service.get_proposal("prop-a").current_outcome)

    def test_merge_routes_preserves_source_decision(self):
        self.seed_round_and_proposals()
        self.decide_both()
        self.service.merge_routes(request_id="mg", actor_id="op",
                                  source_proposal_id="prop-a", target_proposal_id="prop-b",
                                  reason="两条路线并入同一验证计划")
        history = self.service.list_decisions("prop-a")
        self.assertEqual(["validate", "merged"], [d.outcome for d in history])
        self.assertEqual("route-a", history[1].basis["route_ids"][0])
        with self.assertRaises(WorkflowStateError):
            self.service.decide_proposal(request_id="again", actor_id="op",
                                         proposal_id="prop-a", outcome="explore")

    def test_reconsider_supersedes_but_keeps_chain_on_frozen_snapshot(self):
        self.seed_round_and_proposals()
        self.decide_both()
        self.service.reconsider(request_id="rc", actor_id="op", proposal_id="prop-b",
                                outcome="validate", reason="补评后达到验证门槛")
        history = self.service.list_decisions("prop-b")
        self.assertEqual(["explore", "validate"], [d.outcome for d in history])
        self.assertEqual(history[0].decision_id, history[1].supersedes)
        self.assertTrue(history[1].basis["reconsideration"])
        # 原决定与复议依据同一张冻结快照
        self.assertEqual(history[0].snapshot_id, history[1].snapshot_id)

    def test_conditional_decision_requires_conditions(self):
        self.seed_round_and_proposals()
        self.assign_and_score()
        with self.assertRaises(ValidationError):
            self.service.decide_proposal(request_id="dc", actor_id="op",
                                         proposal_id="prop-a", outcome="conditional")
        self.service.decide_proposal(request_id="dc2", actor_id="op",
                                     proposal_id="prop-a", outcome="conditional",
                                     conditions=["三个月内公开先导数据"])
        self.assertEqual("conditional",
                         self.service.get_proposal("prop-a").current_outcome)

    def test_unpublished_evidence_is_recorded_in_basis(self):
        self.seed_round_and_proposals()
        self.assign_and_score()
        self.service.decide_proposal(request_id="da", actor_id="op",
                                     proposal_id="prop-a", outcome="validate")
        latest = self.service.list_decisions("prop-a")[-1]
        self.assertEqual(["ev-pilot:1"], latest.basis["unpublished_evidence"])

    def test_conflicted_score_after_disclosure_is_excluded_from_basis(self):
        self.seed_round_and_proposals()
        self.service.assign_reviewer(request_id="aa1", actor_id="op",
                                     proposal_id="prop-a", reviewer_id="rev2")
        self.service.assign_reviewer(request_id="aa2", actor_id="op",
                                     proposal_id="prop-a", reviewer_id="rev3")
        self.service.submit_score(request_id="sa1", actor_id="rev2",
                                  proposal_id="prop-a", score=10)
        self.service.submit_score(request_id="sa2", actor_id="rev3",
                                  proposal_id="prop-a", score=90)
        # 事后披露 rev2 与 team-a 合作，其评分不得计入决定依据
        self.service.declare_collaboration(request_id="col-a", actor_id="op",
                                           reviewer_id="rev2", team_id="team-a")
        self.service.assign_reviewer(request_id="aa3", actor_id="op",
                                     proposal_id="prop-a", reviewer_id="rev1")
        self.service.submit_score(request_id="sa3", actor_id="rev1",
                                  proposal_id="prop-a", score=95)
        self.service.decide_proposal(request_id="da", actor_id="op",
                                     proposal_id="prop-a", outcome="validate")
        basis = self.service.list_decisions("prop-a")[-1].basis
        self.assertEqual({"rev1", "rev3"}, {s["reviewer_id"] for s in basis["scores"]})
        self.assertEqual([{"reviewer_id": "rev2", "score": 10,
                           "assignment_state": "conflicted"}],
                         basis["excluded_conflicted_scores"])

    def test_initial_decision_can_only_be_made_once_then_requires_reconsideration(self):
        self.seed_round_and_proposals()
        self.assign_and_score()
        self.service.decide_proposal(request_id="da", actor_id="op",
                                     proposal_id="prop-a", outcome="reject")
        with self.assertRaises(WorkflowStateError):
            self.service.decide_proposal(request_id="da2", actor_id="op",
                                         proposal_id="prop-a", outcome="validate")
        self.service.reconsider(request_id="rc", actor_id="op", proposal_id="prop-a",
                                outcome="validate", reason="复议改判")
        self.assertEqual(["reject", "validate"],
                         [d.outcome for d in self.service.list_decisions("prop-a")])


class PortfolioResourceTest(ReviewTestBase):
    def test_duplicate_facility_within_portfolio_is_rejected(self):
        # 两项建议在同一组合中争抢同一稀缺设施
        self.seed_round_and_proposals(facility_b="fac-x")
        self.decide_both()
        with self.assertRaises(ResourceContention):
            self.service.propose_portfolio(
                request_id="dup", actor_id="op", round_id="round-1",
                proposal_ids=["prop-a", "prop-b"])
        # 单一占用是允许的
        receipt = self.service.propose_portfolio(
            request_id="single", actor_id="op", round_id="round-1", proposal_ids=["prop-a"])
        self.assertTrue(receipt.resource_id)

    def test_budget_overrun_is_rejected(self):
        self.seed_round_and_proposals(budget_a=900.0, budget_b=400.0)
        self.decide_both()
        with self.assertRaises(ResourceContention):
            self.service.propose_portfolio(
                request_id="over", actor_id="op", round_id="round-1",
                proposal_ids=["prop-a", "prop-b"])

    def test_only_decided_proposals_may_enter_portfolio(self):
        self.seed_round_and_proposals()
        with self.assertRaises(WorkflowStateError):
            self.service.propose_portfolio(
                request_id="nd", actor_id="op", round_id="round-1",
                proposal_ids=["prop-a", "prop-b"])

    def test_superseding_portfolio_atomically_releases_old_resources(self):
        self.seed_round_and_proposals()
        self.decide_both()
        first, _ = self.effective_portfolio("v1", ["prop-a", "prop-b"])
        self.assertEqual("effective", self.service.get_portfolio(first).state)
        # 资源被生效组合占用的建议不能撤回或并入
        with self.assertRaises(WorkflowStateError):
            self.service.withdraw_proposal(request_id="w", actor_id="app1",
                                           proposal_id="prop-a", reason="撤")
        # 新版本显式声明取代旧组合，在同一事务内释放旧占用并唯一生效
        second, _ = self.effective_portfolio("v2", ["prop-b"], supersedes=first)
        self.assertEqual("superseded", self.service.get_portfolio(first).state)
        self.assertEqual("effective", self.service.get_portfolio(second).state)
        active = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM resource_allocations WHERE released=0").fetchone()["c"]
        self.assertEqual(1, active)
        # 旧组合释放后，prop-a 可以撤回，且原决定仍保留
        self.service.withdraw_proposal(request_id="w2", actor_id="app1",
                                       proposal_id="prop-a", reason="撤")
        self.assertEqual("withdrawn", self.service.get_proposal("prop-a").current_outcome)
        self.assertEqual("validate", self.service.list_decisions("prop-a")[0].outcome)

    def test_rejected_countersign_leaves_previous_effective_untouched(self):
        self.seed_round_and_proposals()
        self.decide_both()
        first, _ = self.effective_portfolio("v1", ["prop-b"])
        draft = self.service.propose_portfolio(
            request_id="v2p", actor_id="op", round_id="round-1", proposal_ids=["prop-a"],
            supersedes_portfolio_id=first)
        self.service.start_countersign(request_id="v2s", actor_id="op",
                                       portfolio_id=draft.resource_id,
                                       signer_ids=["boss1", "boss2"])
        self.service.countersign(request_id="v2g0", actor_id="boss1",
                                 portfolio_id=draft.resource_id, approve=True)
        self.service.countersign(request_id="v2g1", actor_id="boss2",
                                 portfolio_id=draft.resource_id,
                                 approve=False, comment="不同意")
        self.assertEqual("effective", self.service.get_portfolio(first).state)
        self.assertEqual("rejected", self.service.get_portfolio(draft.resource_id).state)
        active = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM resource_allocations WHERE released=0").fetchone()["c"]
        self.assertEqual(1, active)

    def test_competing_approval_without_supersede_declaration_is_blocked(self):
        self.seed_round_and_proposals()
        self.decide_both()
        first, _ = self.effective_portfolio("v1", ["prop-a", "prop-b"])
        # 竞争组合未声明取代目标：即使全部会签通过，也不能原子取得资源
        draft = self.service.propose_portfolio(
            request_id="v2p", actor_id="op", round_id="round-1",
            proposal_ids=["prop-a", "prop-b"])
        self.service.start_countersign(request_id="v2s", actor_id="op",
                                       portfolio_id=draft.resource_id, signer_ids=["boss1"])
        with self.assertRaises(ResourceContention):
            self.service.countersign(request_id="v2g", actor_id="boss1",
                                     portfolio_id=draft.resource_id, approve=True)
        # 事务回滚：旧组合仍唯一生效，竞争组合回到会签中态（未生效）
        self.assertEqual("effective", self.service.get_portfolio(first).state)
        self.assertEqual("countersigning", self.service.get_portfolio(draft.resource_id).state)
        self.assertEqual("pending",
                         self.service.get_countersign(draft.resource_id).state)
        # 错误地声明其他组合为取代目标同样不能夺取生效版本
        other = self.service.propose_portfolio(
            request_id="v3p", actor_id="op", round_id="round-1",
            proposal_ids=["prop-b"], supersedes_portfolio_id=draft.resource_id)
        self.service.start_countersign(request_id="v3s", actor_id="op",
                                       portfolio_id=other.resource_id, signer_ids=["boss1"])
        with self.assertRaises(ResourceContention):
            self.service.countersign(request_id="v3g", actor_id="boss1",
                                     portfolio_id=other.resource_id, approve=True)

    def test_supersede_declaration_must_point_at_existing_portfolio(self):
        self.seed_round_and_proposals()
        self.decide_both()
        with self.assertRaises(Exception):
            self.service.propose_portfolio(
                request_id="bad", actor_id="op", round_id="round-1",
                proposal_ids=["prop-a"], supersedes_portfolio_id="missing-id")

    def test_database_blocks_two_effective_versions(self):
        self.seed_round_and_proposals()
        self.decide_both()
        self.effective_portfolio("v1", ["prop-a"], signers=("boss1",))
        with self.assertRaises(Exception):
            self.database.connection.execute(
                "INSERT INTO portfolios(portfolio_id,round_id,version,state,created_by,created_at) "
                "VALUES('rogue','round-1',99,'effective','x','now')")


class CountersignPersistenceTest(ReviewTestBase):
    def _build_file_world(self, path):
        database = Database(path)
        svc = ReviewService(database, clock_at())
        self.bootstrap_full(svc)
        self.seed_round_and_proposals(svc)
        self.decide_both(svc)
        return database, svc

    def test_countersign_resumes_after_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database, svc = self._build_file_world(path)
            portfolio = svc.propose_portfolio(
                request_id="pf", actor_id="op", round_id="round-1",
                proposal_ids=["prop-a", "prop-b"])
            svc.start_countersign(request_id="cs", actor_id="op",
                                  portfolio_id=portfolio.resource_id,
                                  signer_ids=["boss1", "boss2"])
            svc.countersign(request_id="g1", actor_id="boss1",
                            portfolio_id=portfolio.resource_id, approve=True)
            database.close()
            # 服务重启：重新打开同一个 SQLite 文件继续未结束的会签
            restarted = Database(path)
            svc2 = ReviewService(restarted, clock_at(2))
            status = svc2.get_countersign(portfolio.resource_id)
            self.assertEqual("pending", status.state)
            self.assertEqual(["signed", "pending"], [s["state"] for s in status.signers])
            svc2.countersign(request_id="g2", actor_id="boss2",
                             portfolio_id=portfolio.resource_id, approve=True)
            self.assertEqual("effective",
                             svc2.get_portfolio(portfolio.resource_id).state)
            valid, _ = svc2.verify_audit()
            self.assertTrue(valid)
            restarted.close()

    def test_duplicate_countersign_request_replays_receipt(self):
        self.seed_round_and_proposals()
        self.decide_both()
        portfolio = self.service.propose_portfolio(
            request_id="pf", actor_id="op", round_id="round-1", proposal_ids=["prop-a"])
        self.service.start_countersign(request_id="cs", actor_id="op",
                                       portfolio_id=portfolio.resource_id,
                                       signer_ids=["boss1"])
        first = self.service.countersign(request_id="gsig", actor_id="boss1",
                                         portfolio_id=portfolio.resource_id, approve=True)
        second = self.service.countersign(request_id="gsig", actor_id="boss1",
                                          portfolio_id=portfolio.resource_id, approve=True)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)


class ExplanationAndComparisonTest(ReviewTestBase):
    def test_explain_decision_reports_outcome_and_reasons(self):
        self.seed_round_and_proposals()
        self.assign_and_score()
        self.service.decide_proposal(request_id="da", actor_id="op",
                                     proposal_id="prop-a", outcome="validate")
        explanation = self.service.explain_decision("prop-a")
        self.assertEqual("validate", explanation["current_outcome"])
        joined = "\n".join(explanation["reasons"])
        self.assertIn("独立评审平均分", joined)
        self.assertIn("ev-pilot:1", joined)
        # prop-b 的问题依赖 q-topo，解释中应给出关键依赖链
        explanation_b = self.service.explain_decision("prop-b")
        self.assertIn("q-topo", "\n".join(explanation_b["reasons"]))
        self.assertEqual(1, len(explanation["decision_history"]))
        self.assertTrue(explanation["snapshot"]["matches_current_version"])

    def test_compare_portfolios_reports_frontier_and_dependency_delta(self):
        self.seed_round_and_proposals()
        self.decide_both()
        a = self.service.propose_portfolio(request_id="ca", actor_id="op",
                                           round_id="round-1",
                                           proposal_ids=["prop-a", "prop-b"])
        b = self.service.propose_portfolio(request_id="cb", actor_id="op",
                                           round_id="round-1",
                                           proposal_ids=["prop-a"])
        comparison = self.service.compare_portfolios(a.resource_id, b.resource_id)
        self.assertEqual(["精密测量"], comparison["frontier_delta"]["lost"])
        self.assertEqual(["q-inst"], comparison["question_delta"]["lost"])
        self.assertEqual(-400.0, comparison["budget_delta"])
        # 组合 a 内部覆盖了 q-inst -> q-topo 这条关键依赖边，组合 b 不再覆盖
        self.assertEqual(-1, comparison["dependency_coverage_delta"])

    def test_dependency_graph_merges_terms(self):
        self.seed_round_and_proposals(close=False)
        graph = self.service.dependency_graph("round-1")
        topo = next(q for q in graph["questions"] if q["question_id"] == "q-topo")
        self.assertIn("topological catalysis", topo["terms"])
        inst = next(q for q in graph["questions"] if q["question_id"] == "q-inst")
        self.assertEqual(["q-topo"], inst["depends_on"])

    def test_term_cannot_be_merged_into_two_questions(self):
        self.seed_round_and_proposals(close=False)
        with self.assertRaises(ConflictError):
            self.service.register_question(
                request_id="q3", actor_id="op", question_id="q-other",
                title="其他", frontier="其他前沿", terms=["拓扑催化"])

    def test_snapshot_payload_is_json_serializable(self):
        self.seed_round_and_proposals()
        json.dumps(self.service.get_snapshot("round-1").__dict__)


if __name__ == "__main__":
    unittest.main()
