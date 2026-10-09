"""Shared fixtures for the sharing / notification / RBAC test modules: an owner, an invitee, an outsider and an admin,
plus one chart (two leaf KPIs, eleven rubric levels each) owned by the owner."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session
from sqlalchemy_utils import Ltree

from app.auth import security as sec
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.models.user import User


def hdr(user: User | uuid.UUID | str) -> dict[str, str]:
    """Acts as `user` (the test client turns X-User-Id into a signed bearer token)."""
    uid = user.id if isinstance(user, User) else user
    return {"X-User-Id": str(uid)}


def bearer(user: User) -> dict[str, str]:
    return {"X-Test-Anonymous": "1", "Authorization": f"Bearer {sec.create_access_token(user.id, user.role)[0]}"}


def make_chart(db: Session, owner: User, name: str = "Shared chart") -> dict:
    sc = Scorecard(name=name, owner_id=owner.id, domain="Hackathon", target_score=7)
    db.add(sc)
    db.flush()
    ver = ScorecardVersion(scorecard_id=sc.id, version_number=1, created_by=owner.id)
    db.add(ver)
    db.flush()
    sc.current_version_id = ver.id
    node_ids = [uuid.uuid4(), uuid.uuid4()]
    for i, nid in enumerate(node_ids):
        db.add(KpiNode(id=nid, scorecard_version_id=ver.id, parent_id=None, path=Ltree(nid.hex), level=1,
                       name=f"KPI {i}", weight=50, display_order=i))
    db.flush()
    for nid in node_ids:
        for level in range(11):
            db.add(KpiGuideline(kpi_node_id=nid, score_level=level, qualitative_text=f"L{level}"))
    db.commit()
    return {"id": str(sc.id), "version_id": str(ver.id), "node_ids": [str(n) for n in node_ids]}


class Team:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.owner = User(email="owner@example.com", name="Olga Owner", username="olga")
        self.invitee = User(email="invitee@example.com", name="Ivan Invitee", username="ivan")
        self.outsider = User(email="outsider@example.com", name="Otto Outsider", username="otto")
        self.admin = User(email="admin@example.com", name="Ada Admin", username="ada", role="admin")
        db.add_all([self.owner, self.invitee, self.outsider, self.admin])
        db.commit()
        self.chart = make_chart(db, self.owner)

    @property
    def sc(self) -> str:
        return self.chart["id"]

    def invite(self, client, who: User | str, sender: User | None = None):
        ident = who if isinstance(who, str) else who.email
        return client.post(
            f"/api/v1/scorecards/{self.sc}/invitations", json={"identifier": ident}, headers=hdr(sender or self.owner)
        )

    def share(self, client, who: User | None = None) -> str:
        """Invite + accept; returns the invitation id."""
        who = who or self.invitee
        inv = self.invite(client, who)
        assert inv.status_code == 201, inv.text
        done = client.post(f"/api/v1/invitations/{inv.json()['id']}/accept", headers=hdr(who))
        assert done.status_code == 200, done.text
        return inv.json()["id"]


@pytest.fixture()
def team(db_session: Session) -> Team:
    return Team(db_session)
