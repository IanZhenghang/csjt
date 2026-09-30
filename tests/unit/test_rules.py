"""T10 规则分级与业务校验单测。"""

from __future__ import annotations

from defect_agent.rules import assess_defect, business_checks, finalize_event
from defect_agent.schemas import (
    AttributionHypothesis,
    Claim,
    Counterfactual,
    Event,
    VisualObservation,
)


def _observation(system_state="unknown", visibility="clear", evidence=None):
    return VisualObservation(
        observation_id="OBS1",
        video_id="V",
        window_id="V::window::W001",
        description="观察",
        phenomenon_type="lane_boundary_crossing",
        evidence_ids=evidence or ["V::frame::F0001"],
        start_sec=1.0,
        end_sec=5.0,
        visibility=visibility,
        continuity="continuous",
        system_state=system_state,
    )


def _event(phenomenon="lane_boundary_crossing", contradicting=None, observations=None):
    return Event(
        event_id="R::V::event::E001",
        video_id="V",
        start_sec=1.0,
        end_sec=5.0,
        phenomenon_type=phenomenon,
        phenomenon_summary="s",
        observation_ids=[o.observation_id for o in (observations or [])],
        supporting_evidence_ids=[],
        contradicting_evidence_ids=contradicting or [],
    )


def _claim(claim_type="system_state_claim"):
    return Claim(
        claim_id="R::V::claim::C001",
        video_id="V",
        statement="智驾开着",
        claim_type=claim_type,
        evidence_ids=["V::ocr::O1"],
        source_group_id="g",
        temporal_scope="interval",
        start_sec=1.0,
        end_sec=4.0,
    )


class _FakeStore:
    def __init__(self, kinds):
        self._kinds = kinds

    def get(self, evidence_id):
        kind = self._kinds.get(evidence_id)
        if kind is None:
            return None
        return type("R", (), {"source_kind": kind})()


def test_uncertain_phenomenon_never_suspected():
    """现象类型 uncertain：即使有画面覆盖也不判 suspected。"""
    observations = [
        VisualObservation(
            observation_id="OBS1", video_id="V", window_id="W",
            description="d", phenomenon_type="uncertain",
            evidence_ids=["V::frame::F0001"], start_sec=0.0, end_sec=6.0,
            visibility="clear",
        )
    ]
    event = _event(phenomenon="uncertain", observations=observations)
    grades = {"phenomenon_evidence_grade": "high"}
    event.system_engagement = "reported_active"
    assert assess_defect(event, observations, [_claim()], grades) == "insufficient_evidence"


def test_reported_active_with_support_suspected():
    observations = [_observation()]
    event = _event(observations=observations)
    grades = {"phenomenon_evidence_grade": "medium"}
    event.system_engagement = "reported_active"
    assert assess_defect(event, observations, [_claim()], grades) == "suspected"


def test_unknown_system_state_insufficient():
    observations = [_observation()]
    event = _event(observations=observations)
    grades = {"phenomenon_evidence_grade": "medium"}
    event.system_engagement = "unknown"
    assert assess_defect(event, observations, [], grades) == "insufficient_evidence"


def test_claim_only_low_support_not_suspected():
    event = _event(observations=[])
    grades = {"phenomenon_evidence_grade": "low"}
    event.system_engagement = "reported_active"
    assert assess_defect(event, [], [_claim()], grades) == "insufficient_evidence"


def test_finalize_never_grants_observed_active():
    observations = [_observation(system_state="画面可见 pilot 图标")]
    event = _event(observations=observations)
    finalized = finalize_event(event, observations, [_claim()], [], _FakeStore({}))
    # 无法区分原生界面与叠加文字 → 不得 observed_active
    assert finalized.system_engagement == "reported_active"
    assert finalized.defect_assessment in ("suspected", "insufficient_evidence")
    assert finalized.review_required is True
    assert finalized.attribution_evidence_grade == "insufficient"


def test_business_checks_observed_active_rejected():
    event = _event()
    event.system_engagement = "observed_active"
    violations = business_checks(event, [], [])
    assert any("observed_active" in v for v in violations)


def test_attribution_grade_medium_with_frame_support():
    hypothesis = AttributionHypothesis(
        hypothesis_id="H1",
        event_id="R::V::event::E001",
        hypothesis="假设",
        supporting_evidence_ids=["V::frame::F0001"],
        counterfactual=Counterfactual(
            changed_condition="c", expected_difference="e", reasoning_summary="r"
        ),
    )
    from defect_agent.rules import grade_attribution

    store = _FakeStore({"V::frame::F0001": "frame"})
    assert grade_attribution([hypothesis], store) == "medium"
    assert grade_attribution([], store) == "insufficient"


def test_no_confirmed_defect_enum():
    """首版不提供 confirmed_defect。"""
    from defect_agent.schemas import DefectAssessment
    import typing

    values = typing.get_args(DefectAssessment)
    assert "confirmed_defect" not in values
    assert set(values) == {"suspected", "insufficient_evidence", "not_supported", "not_applicable"}
