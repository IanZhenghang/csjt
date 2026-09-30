"""核心数据结构（Pydantic 2，extra=forbid）。

类型验证只负责结构与约束，不判断内容真实性；
语义约束由引用校验和业务规则模块执行（见 IMPLEMENTATION_PLAN.md §6、§10.3）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 枚举（Literal）
# ---------------------------------------------------------------------------

SourceKind = Literal[
    "frame", "derived_frame", "asr", "ocr", "ocr_span", "comment", "title", "description", "segment"
]
TimeScope = Literal["point", "interval", "video"]
ViewKind = Literal["full_frame", "subtitle_crop", "other_crop"]

ClaimType = Literal[
    "phenomenon_claim",
    "system_state_claim",
    "scene_claim",
    "cause_claim",
    "vehicle_identity_claim",
    "other",
]
AlignmentStatus = Literal["unassigned", "possible_match", "explicit_time_match", "human_confirmed"]

PhenomenonType = Literal[
    "lane_boundary_crossing",
    "unexpected_lateral_motion",
    "apparent_deceleration",
    "obstacle_approach",
    "collision_or_contact",
    "driver_intervention",
    "system_state_change",
    "other",
    "none_observed",
    "uncertain",
]

BoundaryStatus = Literal["coarse", "refined", "human_reviewed"]
SystemEngagement = Literal["observed_active", "reported_active", "observed_inactive", "unknown"]
DefectAssessment = Literal["suspected", "insufficient_evidence", "not_supported", "not_applicable"]
EvidenceGrade = Literal["high", "medium", "low", "insufficient"]

RelationKind = Literal["supports", "contradicts", "same_source", "possible_match", "unrelated"]

EvidenceRequestType = Literal[
    "request_more_frames",
    "request_original_resolution",
    "request_human_audio_review",
    "request_human_system_state_review",
    "request_source_identity_review",
]

TaskStatus = Literal["pending", "running", "succeeded", "failed", "skipped", "paused_budget"]
VideoStatus = Literal["complete", "partial", "failed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 规范化输入记录（适配器输出）
# ---------------------------------------------------------------------------


class SegmentRecord(StrictModel):
    segment_id: str
    start_sec: float
    end_sec: float
    media_path: str  # 包内相对路径
    frame_ids: list[str] = Field(default_factory=list)
    asr_ids: list[str] = Field(default_factory=list)
    ocr_ids: list[str] = Field(default_factory=list)
    all_frame_ids: list[str] = Field(default_factory=list)


class FrameRecord(StrictModel):
    frame_id: str
    timestamp_sec: float
    media_path: str
    width: int | None = None
    height: int | None = None
    view_kind: ViewKind


class AsrRecord(StrictModel):
    asr_id: str  # 统一用 utterance_id（A000 形态）
    start_sec: float
    end_sec: float
    text: str
    method: str | None = None
    human_reviewed: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)  # 如 v2+ 的 candidate_id


class OcrRecord(StrictModel):
    ocr_id: str  # 对外统一 ID：v1 用 text_id（O 前缀），v2+ 用 ocr_id
    timestamp_sec: float
    frame_id: str | None = None
    text: str
    source_type: str | None = None
    source_pass: str | None = None  # full_frame / subtitle_strip / dashboard 等
    human_reviewed: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    raw_record_ids: list[str] = Field(default_factory=list)


class OcrSpanRecord(StrictModel):
    span_id: str
    text: str
    ocr_ids: list[str]  # 指向 OcrRecord.ocr_id
    first_observed_sec: float | None = None
    last_observed_sec: float | None = None
    source_type: str | None = None


class CommentRecord(StrictModel):
    comment_id: str
    parent_id: str | None = None
    root_id: str | None = None
    text: str
    created_at: str | None = None
    likes_at_retrieval: int | None = None
    video_timestamp_sec: float | None = None  # 当前数据全部为空，保持 None
    quality_flags: list[str] = Field(default_factory=list)


class VideoMeta(StrictModel):
    video_id: str
    title: str | None = None
    description: str | None = None
    description_status: str | None = None
    duration_sec: float | None = None
    published_at: str | None = None
    platform: str | None = "bilibili"
    url: str | None = None
    author_name: str | None = None
    model: str | None = None  # 车型字段，当前数据均为空
    software_version: str | None = None


class NormalizedPackage(StrictModel):
    package_id: str  # v1..v10
    package_root: str = ""  # 数据包绝对路径（只读）
    video_id: str
    schema_version: str  # 原始 manifest 的 schema_version
    adapter: str  # "v1" | "v2"
    duration_sec: float
    segments: list[SegmentRecord]
    frames: list[FrameRecord]
    asr: list[AsrRecord]
    ocr: list[OcrRecord]
    ocr_spans: list[OcrSpanRecord]
    comments: list[CommentRecord]
    meta: VideoMeta
    source_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str] = Field(default_factory=dict)  # 相对路径 -> sha256
    manifest_counts: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 正式证据与结论对象（§6）
# ---------------------------------------------------------------------------


class EvidenceRecord(StrictModel):
    evidence_id: str  # {video_id}::{kind}::{local_id}
    video_id: str
    source_kind: SourceKind
    source_file: str
    source_record_id: str | None = None
    text: str | None = None
    time_scope: TimeScope
    timestamp_sec: float | None = None
    start_sec: float | None = None
    end_sec: float | None = None
    media_path: str | None = None
    view_kind: ViewKind | None = None
    source_group_id: str
    human_reviewed: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)


class Claim(StrictModel):
    claim_id: str
    video_id: str
    statement: str
    claim_type: ClaimType
    evidence_ids: list[str]
    source_group_id: str
    temporal_scope: TimeScope = "video"
    start_sec: float | None = None
    end_sec: float | None = None
    candidate_event_ids: list[str] = Field(default_factory=list)
    alignment_status: AlignmentStatus = "unassigned"


class VisualObservation(StrictModel):
    observation_id: str
    video_id: str
    window_id: str
    description: str
    phenomenon_type: PhenomenonType
    evidence_ids: list[str]
    start_sec: float
    end_sec: float
    visibility: str = "unknown"  # 可见性描述
    continuity: str = "unknown"  # 连续性描述（剪辑/遮挡等）
    system_state: str = "unknown"
    limitations: list[str] = Field(default_factory=list)


class EvidenceGradeTriple(StrictModel):
    phenomenon_evidence_grade: EvidenceGrade = "insufficient"
    system_state_evidence_grade: EvidenceGrade = "insufficient"
    attribution_evidence_grade: EvidenceGrade = "insufficient"


class Event(EvidenceGradeTriple):
    event_id: str
    video_id: str
    start_sec: float
    end_sec: float
    boundary_status: BoundaryStatus = "coarse"
    phenomenon_type: PhenomenonType
    phenomenon_summary: str
    observation_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    system_engagement: SystemEngagement = "unknown"
    defect_assessment: DefectAssessment = "insufficient_evidence"
    scene: str | None = None
    vehicle_identity: dict[str, Any] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)
    review_required: bool = True
    processing_status: str = "pending"


class EvidenceRelation(StrictModel):
    from_id: str
    to_id: str
    relation: RelationKind
    proposition: str  # 该边针对的命题
    reason: str
    created_by: str  # "program" 或角色名


class Counterfactual(StrictModel):
    changed_condition: str
    held_constant_conditions: list[str] = Field(default_factory=list)
    expected_difference: str
    reasoning_summary: str
    # 兼容模型输出中的已知拼写错误；schema 和序列化仍只使用标准字段名。
    # 两种字段名同时出现时仍由 extra=forbid 拒收，避免静默丢失内容。
    possible_confounders: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("possible_confounders", "possible_conconfounders"),
    )
    required_validation: list[str] = Field(default_factory=list)


class AttributionHypothesis(StrictModel):
    hypothesis_id: str
    event_id: str
    hypothesis: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    counterfactual: Counterfactual
    verification_status: Literal["unverified"] = "unverified"


class ReviewItem(StrictModel):
    review_item_id: str
    event_id: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_action: str
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["open", "resolved"] = "open"


# ---------------------------------------------------------------------------
# 模型输出草稿（ID 由程序分配，模型不生成最终主键）
# ---------------------------------------------------------------------------


class ClaimDraft(StrictModel):
    statement: str
    claim_type: ClaimType
    evidence_ids: list[str]
    temporal_scope: TimeScope = "video"
    start_sec: float | None = None
    end_sec: float | None = None


class ClaimBatchResult(StrictModel):
    claims: list[ClaimDraft]


class ObservationDraft(StrictModel):
    description: str
    phenomenon_type: PhenomenonType
    evidence_ids: list[str]
    start_sec: float
    end_sec: float
    visibility: str = "unknown"
    continuity: str = "unknown"
    system_state: str = "unknown"
    limitations: list[str] = Field(default_factory=list)


class ObservationBatchResult(StrictModel):
    observations: list[ObservationDraft]


class AssociationDraft(StrictModel):
    claim_id: str
    event_id: str
    relation: RelationKind
    proposition: str
    reason: str


class AlignmentDraftResult(StrictModel):
    associations: list[AssociationDraft]


class EvidenceRequestDraft(StrictModel):
    request_type: EvidenceRequestType
    reason: str
    target_event_ids: list[str] = Field(default_factory=list)


class ReviewIssueDraft(StrictModel):
    kind: str  # contradiction / missing / alternative / quality
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewDraftResult(StrictModel):
    issues: list[ReviewIssueDraft] = Field(default_factory=list)
    evidence_requests: list[EvidenceRequestDraft] = Field(default_factory=list)
    notes: str = ""


class AttributionDraft(StrictModel):
    hypothesis: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    counterfactual: Counterfactual


class AttributionDraftResult(StrictModel):
    hypotheses: list[AttributionDraft] = Field(default_factory=list)
    reason: str = ""
