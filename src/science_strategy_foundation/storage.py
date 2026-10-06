"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    PRIMARY KEY (team_id, actor_id)
);
CREATE TABLE IF NOT EXISTS reviewer_collaborations (
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (reviewer_id, team_id)
);
CREATE TABLE IF NOT EXISTS evidence_versions (
    evidence_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    published INTEGER NOT NULL CHECK(published IN (0, 1)),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (evidence_id, version)
);
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    frontier TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS question_terms (
    term TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(question_id)
);
CREATE TABLE IF NOT EXISTS question_dependencies (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    depends_on TEXT NOT NULL REFERENCES questions(question_id),
    CHECK(question_id <> depends_on),
    PRIMARY KEY (question_id, depends_on)
);
CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    deadline TEXT NOT NULL,
    min_reviewers INTEGER NOT NULL DEFAULT 2 CHECK(min_reviewers >= 1),
    state TEXT NOT NULL CHECK(state IN ('open', 'closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_pools (
    round_id TEXT PRIMARY KEY REFERENCES rounds(round_id),
    budget_total REAL NOT NULL CHECK(budget_total >= 0)
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    title TEXT NOT NULL,
    test_criterion TEXT NOT NULL,
    budget REAL NOT NULL CHECK(budget >= 0),
    facility_id TEXT REFERENCES facilities(facility_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_hash TEXT NOT NULL,
    current_outcome TEXT,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal_evidence_refs (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    evidence_id TEXT NOT NULL,
    evidence_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (proposal_id, evidence_id, evidence_version),
    FOREIGN KEY (evidence_id, evidence_version)
        REFERENCES evidence_versions(evidence_id, version)
);
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    summary TEXT NOT NULL,
    merged_from_json TEXT NOT NULL DEFAULT '[]',
    merged_into TEXT REFERENCES proposals(proposal_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL UNIQUE,
    baseline_hash TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_snapshot_items (
    snapshot_id TEXT NOT NULL REFERENCES review_snapshots(snapshot_id),
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, proposal_id)
);
CREATE TABLE IF NOT EXISTS review_assignments (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    state TEXT NOT NULL CHECK(state IN ('assigned', 'recused', 'conflicted')),
    reason TEXT NOT NULL DEFAULT '',
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (proposal_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS review_scores (
    proposal_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (proposal_id, reviewer_id),
    FOREIGN KEY (proposal_id, reviewer_id)
        REFERENCES review_assignments(proposal_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    snapshot_id TEXT REFERENCES review_snapshots(snapshot_id),
    outcome TEXT NOT NULL CHECK(outcome IN (
        'explore', 'validate', 'waitlist', 'reject',
        'conditional', 'withdrawn', 'merged')),
    basis_json TEXT NOT NULL,
    supersedes TEXT REFERENCES decisions(decision_id),
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(proposal_id, sequence)
);
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    state TEXT NOT NULL CHECK(state IN ('draft', 'countersigning', 'effective', 'superseded', 'rejected')),
    supersedes_portfolio_id TEXT REFERENCES portfolios(portfolio_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(round_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolios_one_effective
    ON portfolios(round_id) WHERE state = 'effective';
CREATE TABLE IF NOT EXISTS portfolio_items (
    portfolio_id TEXT NOT NULL REFERENCES portfolios(portfolio_id),
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    PRIMARY KEY (portfolio_id, proposal_id)
);
CREATE TABLE IF NOT EXISTS resource_allocations (
    allocation_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(portfolio_id),
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
    pool_round_id TEXT NOT NULL REFERENCES rounds(round_id),
    amount REAL NOT NULL CHECK(amount >= 0),
    facility_id TEXT,
    released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_allocation_active_proposal
    ON resource_allocations(proposal_id) WHERE released = 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_facility_single_occupant
    ON resource_allocations(facility_id) WHERE facility_id IS NOT NULL AND released = 0;
CREATE TABLE IF NOT EXISTS countersign_workflows (
    portfolio_id TEXT PRIMARY KEY REFERENCES portfolios(portfolio_id),
    state TEXT NOT NULL CHECK(state IN ('pending', 'signed', 'rejected')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS countersign_signatures (
    portfolio_id TEXT NOT NULL,
    signer_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'signed', 'rejected')),
    request_id TEXT,
    comment TEXT NOT NULL DEFAULT '',
    signed_at TEXT,
    PRIMARY KEY (portfolio_id, signer_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
