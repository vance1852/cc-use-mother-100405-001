"""研究组合审议 HTTP 路由测试。"""

import unittest

from science_strategy_foundation.api import route
from science_strategy_foundation.review import ReviewService
from science_strategy_foundation.storage import Database


def headers(actor_id):
    return {"X-Actor-Id": actor_id}


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = ReviewService(self.database)
        self.post("/organizations", "bootstrap",
                  {"request_id": "org", "organization_id": "o1", "name": "国家实验室"})
        self.post("/actors", "bootstrap",
                  {"request_id": "admin", "new_actor_id": "admin", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"})
        for rid, aid, role in [
            ("op-r", "op", "operator"), ("rv-r", "rev1", "reviewer"),
            ("rv2-r", "rev2", "reviewer"), ("ap-r", "app1", "operator"),
            ("ap2-r", "app2", "operator"), ("b1-r", "boss1", "admin"),
        ]:
            self.post("/actors", "admin",
                      {"request_id": rid, "new_actor_id": aid, "display_name": aid,
                       "role": role, "organization_id": "o1"})

    def tearDown(self):
        self.database.close()

    def post(self, path, actor, body):
        status, payload = route(self.service, "POST", path, body, headers(actor))
        self.assertIn(status, (200, 201), payload)
        return payload

    def get(self, path, actor="op"):
        status, payload = route(self.service, "GET", path, None, headers(actor))
        self.assertEqual(200, status, payload)
        return payload

    def seed(self):
        self.post("/teams", "op", {"request_id": "ta", "team_id": "team-a", "name": "甲队",
                                   "organization_id": "o1", "member_ids": ["app1"]})
        self.post("/teams", "op", {"request_id": "tb", "team_id": "team-b", "name": "乙队",
                                   "organization_id": "o1", "member_ids": ["app2"]})
        self.post("/facilities", "op", {"request_id": "fac", "facility_id": "fac-x",
                                        "name": "光源"})
        self.post("/evidence", "app1", {"request_id": "ev", "evidence_id": "ev-1",
                                        "title": "先导数据", "data": {"k": 1}})
        self.post("/questions", "op", {"request_id": "q1", "question_id": "q-a",
                                       "title": "问题甲", "frontier": "量子材料",
                                       "terms": ["问题甲", "question-a"]})
        self.post("/questions", "op", {"request_id": "q2", "question_id": "q-b",
                                       "title": "问题乙", "frontier": "精密测量",
                                       "depends_on": ["q-a"]})
        self.post("/rounds", "op", {"request_id": "rd", "round_id": "r1", "title": "本轮",
                                    "deadline": "2026-10-31", "budget_total": 1000})
        self.post("/proposals", "app1",
                  {"request_id": "p1", "round_id": "r1", "proposal_id": "prop-a",
                   "team_id": "team-a", "question_id": "q-a", "title": "建议甲",
                   "test_criterion": "可证伪判据", "budget": 300,
                   "facility_id": "fac-x", "evidence_refs": [["ev-1", 1]]})
        self.post("/proposals", "app2",
                  {"request_id": "p2", "round_id": "r1", "proposal_id": "prop-b",
                   "team_id": "team-b", "question_id": "q-b", "title": "建议乙",
                   "test_criterion": "可证伪判据", "budget": 400})
        self.post("/rounds/r1/close", "op", {"request_id": "close"})

    def review_and_decide(self):
        self.post("/proposals/prop-a/reviewers", "op", {"request_id": "a1", "reviewer_id": "rev1"})
        self.post("/proposals/prop-a/reviewers", "op", {"request_id": "a2", "reviewer_id": "rev2"})
        self.post("/proposals/prop-b/reviewers", "op", {"request_id": "b1", "reviewer_id": "rev1"})
        self.post("/proposals/prop-b/reviewers", "op", {"request_id": "b2", "reviewer_id": "rev2"})
        self.post("/proposals/prop-a/scores", "rev1", {"request_id": "s1", "score": 90})
        self.post("/proposals/prop-a/scores", "rev2", {"request_id": "s2", "score": 80})
        self.post("/proposals/prop-b/scores", "rev1", {"request_id": "s3", "score": 70})
        self.post("/proposals/prop-b/scores", "rev2", {"request_id": "s4", "score": 65})
        self.post("/proposals/prop-a/decisions", "op",
                  {"request_id": "d1", "outcome": "validate"})
        self.post("/proposals/prop-b/decisions", "op",
                  {"request_id": "d2", "outcome": "explore"})

    def test_full_review_portfolio_flow_over_http(self):
        self.seed()
        self.review_and_decide()
        explanation = self.get("/proposals/prop-b/explanation")
        self.assertEqual("explore", explanation["current_outcome"])
        self.assertTrue(explanation["snapshot"]["matches_current_version"])
        portfolio = self.post("/portfolios", "op",
                              {"request_id": "pf", "round_id": "r1",
                               "proposal_ids": ["prop-a", "prop-b"]})
        portfolio_id = portfolio["resource_id"]
        self.post(f"/portfolios/{portfolio_id}/countersign/start", "op",
                  {"request_id": "cs", "signer_ids": ["boss1"]})
        self.post(f"/portfolios/{portfolio_id}/countersign", "boss1",
                  {"request_id": "sign", "approve": True})
        detail = self.get(f"/portfolios/{portfolio_id}")
        self.assertEqual("effective", detail["state"])

    def test_late_amendment_returns_409_domain_code(self):
        self.seed()
        status, payload = route(self.service, "POST", "/proposals",
                                {"request_id": "late", "round_id": "r1",
                                 "proposal_id": "prop-a", "team_id": "team-a",
                                 "question_id": "q-a", "title": "补交",
                                 "test_criterion": "x", "budget": 1},
                                headers("app1"))
        self.assertEqual(409, status)
        self.assertEqual("submission_closed", payload["error"])

    def test_quorum_failure_returns_409(self):
        self.seed()
        self.post("/proposals/prop-a/reviewers", "op",
                  {"request_id": "a1", "reviewer_id": "rev1"})
        self.post("/proposals/prop-a/scores", "rev1",
                  {"request_id": "s1", "score": 90})
        # 只分配 1 名独立评审，低于默认 2 名
        status, payload = route(self.service, "POST", "/proposals/prop-a/decisions",
                                {"request_id": "d", "outcome": "validate"}, headers("op"))
        self.assertEqual(409, status)
        self.assertEqual("review_quorum", payload["error"])

    def test_collaboration_blocks_assignment_over_http(self):
        self.seed()
        self.post("/collaborations", "op",
                  {"request_id": "col", "reviewer_id": "rev1", "team_id": "team-a"})
        status, payload = route(self.service, "POST", "/proposals/prop-a/reviewers",
                                {"request_id": "x", "reviewer_id": "rev1"}, headers("op"))
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_comparison_and_graph_endpoints(self):
        self.seed()
        self.review_and_decide()
        a = self.post("/portfolios", "op", {"request_id": "ca", "round_id": "r1",
                                            "proposal_ids": ["prop-a", "prop-b"]})
        b = self.post("/portfolios", "op", {"request_id": "cb", "round_id": "r1",
                                            "proposal_ids": ["prop-a"]})
        comparison = self.get(
            f"/portfolio-comparison?a={a['resource_id']}&b={b['resource_id']}")
        self.assertEqual(["精密测量"], comparison["frontier_delta"]["lost"])
        graph = self.get("/dependency-graph?round_id=r1")
        question_ids = {q["question_id"] for q in graph["questions"]}
        self.assertEqual({"q-a", "q-b"}, question_ids)

    def test_decisions_endpoint_lists_append_only_history(self):
        self.seed()
        self.review_and_decide()
        self.post("/proposals/prop-b/reconsiderations", "op",
                  {"request_id": "rc", "outcome": "validate", "reason": "复议升级"})
        decisions = self.get("/proposals/prop-b/decisions")
        self.assertEqual(["explore", "validate"], [i["outcome"] for i in decisions["items"]])
        self.assertIsNotNone(decisions["items"][1]["supersedes"])

    def test_snapshot_endpoint_freezes_versions(self):
        self.seed()
        snapshot = self.get("/rounds/r1/snapshot")
        self.assertEqual(2, snapshot["item_count"])
        self.assertEqual(64, len(snapshot["baseline_hash"]))


if __name__ == "__main__":
    unittest.main()
