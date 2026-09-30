"""证据索引隔离、快照原子性和混合检索的离线回归。"""

import json
import sqlite3
import pytest

from defect_agent.retrieval import EvidenceIndex, IndexedItem, Retriever


class QueryEmbedding:
    def __init__(self, vector=None, error=None):
        self.vector = [1.0, 0.0] if vector is None else vector
        self.error = error
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.error:
            raise self.error
        return [self.vector]


def _index(path, **overrides):
    settings = dict(video_id="V", source_revision="source-1", model_namespace="provider/model/dim2")
    settings.update(overrides)
    return EvidenceIndex(path, **settings)


def _item(item_id, text="测试", **overrides):
    values = dict(item_id=item_id, kind="event", text=text, video_id="V", vector=[1.0, 0.0])
    values.update(overrides)
    return IndexedItem(**values)


def test_persistent_index_isolates_every_revision_and_video(tmp_path):
    path = tmp_path / "index.sqlite"
    configurations = [{}, {"video_id": "OTHER"}, {"source_revision": "source-2"},
                      {"model_namespace": "other/model/dim2"}, {"derived_revision": "run-2"}]
    for number, config in enumerate(configurations):
        index = _index(path, **config)
        index.replace([_item("same-local-id", str(number), video_id=config.get("video_id", "V"))])
        index.close()
    for number, config in enumerate(configurations):
        index = _index(path, **config)
        assert [item.text for item in index.items()] == [str(number)]
        index.close()


def test_replace_is_a_complete_snapshot_and_preserves_provenance(tmp_path):
    path = tmp_path / "index.sqlite"
    writer, reader = _index(path), _index(path)
    writer.replace([_item("old")])
    old = Retriever(reader).retrieve("q").trace.snapshot_id
    item = _item("new", evidence_ids=["V::asr::A1"], metadata={"raw_refs": ["source"]})
    writer.replace([item])
    assert reader.items() == [item]
    assert Retriever(reader).retrieve("q").trace.snapshot_id != old
    writer.replace([item])
    same = Retriever(reader).retrieve("q").trace.snapshot_id
    assert same == Retriever(writer).retrieve("q").trace.snapshot_id
    writer.replace([])
    assert reader.items() == []
    writer.close()
    reader.close()


def test_failed_replace_rolls_back_deletion_and_snapshot(tmp_path):
    path = tmp_path / "index.sqlite"
    index = _index(path)
    index.replace([_item("old")])
    before = Retriever(index).retrieve("q").trace.snapshot_id
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_bad BEFORE INSERT ON evidence_index_items "
            "WHEN NEW.item_id = 'bad' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        index.replace([_item("a-good"), _item("bad")])
    assert [item.item_id for item in index.items()] == ["old"]
    assert Retriever(index).retrieve("q").trace.snapshot_id == before
    index.close()


def test_foreign_video_and_duplicate_ids_do_not_replace_current_snapshot(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    index.replace([_item("old")])
    for invalid in [[_item("foreign", video_id="OTHER")], [_item("same"), _item("same")]]:
        with pytest.raises(ValueError):
            index.replace(invalid)
        assert [item.item_id for item in index.items()] == ["old"]
    index.close()


def test_hybrid_union_preserves_distinct_lexical_and_semantic_hits(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    index.replace([
        _item("lexical", "车辆并未碰撞，NOA 120 状态正常", vector=[0.0, 1.0]),
        _item("semantic", "护栏前出现接触", vector=[1.0, 0.0]),
        _item("other", "夕阳道路", vector=[-1.0, 0.0]),
        _item("excluded-kind", "车辆碰撞 NOA 120", kind="comment"),
    ])
    result = Retriever(index, QueryEmbedding(), semantic_top_k=1, lexical_top_k=1).retrieve(
        "车辆碰撞 noa 120", kinds=["event"], query_id="C1",
    )
    assert {item.item_id for item in result.items} == {"lexical", "semantic"}
    assert result.trace.candidate_ids == ["lexical", "other", "semantic"]
    assert result.trace.filtered_out_ids == ["other"]
    assert result.trace.reasons["lexical"] == ["lexical_top_k"]
    assert result.trace.reasons["semantic"] == ["semantic_top_k"]
    assert result.trace.lexical_scores["lexical"] > 0
    assert result.trace.semantic_scores["semantic"] == pytest.approx(1)
    assert not result.trace.degraded
    assert json.loads(json.dumps(result.trace.to_dict()))["query_id"] == "C1"
    index.close()


def test_temporal_only_retains_overlap_near_and_points_without_embedding(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    index.replace([
        _item("overlap", start_sec=9, end_sec=11),
        _item("near", start_sec=22, end_sec=24),
        _item("point", start_sec=15, end_sec=15),
        _item("far", start_sec=22.01, end_sec=24),
        _item("untimed"),
    ])
    client = QueryEmbedding(error=RuntimeError("must never call"))
    result = Retriever(index, client).retrieve("q", start_sec=10, end_sec=20, temporal_only=True)
    assert {item.item_id for item in result.items} == {"overlap", "near", "point"}
    assert set(result.trace.filtered_out_ids) == {"far", "untimed"}
    assert client.calls == 0
    assert not result.trace.degraded
    index.close()


def test_default_top_five_union_and_chinese_lexical_matching(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    index.replace(
        [_item(f"semantic-{i}", "阳光", vector=[1, 0.01 * i]) for i in range(5)]
        + [_item(f"lexical-{i}", "车辆碰撞", vector=[0, 1]) for i in range(5)]
        + [_item("irrelevant", "下雨", vector=[-1, 0])]
    )
    result = Retriever(index, QueryEmbedding()).retrieve("车辆碰撞")
    assert len(result.items) == 10
    assert result.trace.filtered_out_ids == ["irrelevant"]
    assert all("lexical_top_k" in result.trace.reasons[f"lexical-{i}"] for i in range(5))
    assert all("semantic_top_k" in result.trace.reasons[f"semantic-{i}"] for i in range(5))
    index.close()


@pytest.mark.parametrize("bad_vector", [None, [], [0, 0], [float("nan"), 1],
                                          [float("inf"), 1], [True, 1], ["bad", 1], [1, 0, 0]])
def test_invalid_or_missing_index_vector_falls_back_to_all_candidates(tmp_path, bad_vector):
    index = _index(tmp_path / "index.sqlite")
    index.replace([_item("valid"), _item("bad", vector=bad_vector)])
    result = Retriever(index, QueryEmbedding(), semantic_top_k=1, lexical_top_k=1).retrieve("nothing")
    assert {item.item_id for item in result.items} == {"valid", "bad"}
    assert result.trace.degraded and result.trace.mode == "fallback_all"
    assert result.trace.semantic_scores == {}
    assert result.trace.filtered_out_ids == []
    index.close()


@pytest.mark.parametrize("client", [None, QueryEmbedding(vector=[]), QueryEmbedding(vector=[0, 0]),
                                    QueryEmbedding(vector=[1, 0, 0]), QueryEmbedding(error=RuntimeError("offline"))])
def test_query_embedding_failure_is_explicit_and_cannot_silently_drop_items(tmp_path, client):
    index = _index(tmp_path / "index.sqlite")
    index.replace([_item(str(i)) for i in range(8)])
    result = Retriever(index, client).retrieve("query")
    assert len(result.items) == 8
    assert result.trace.degraded and result.trace.fallback_reason
    assert all("vector_failure_all_candidates" in reasons for reasons in result.trace.reasons.values())
    index.close()


def test_explicit_lexical_fallback_and_temporal_union(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    index.replace([
        _item("lexical", "中文车辆 NOA 120"),
        _item("temporal", "无关", start_sec=10, end_sec=15),
        _item("unselected", "大雨"),
    ])
    result = Retriever(index, None, lexical_top_k=1).retrieve(
        "车辆 noa 120", start_sec=10, end_sec=12, fallback_all_on_vector_failure=False,
    )
    assert {item.item_id for item in result.items} == {"lexical", "temporal"}
    assert result.trace.mode == "lexical_fallback"
    index.close()


def test_empty_snapshot_and_kind_filter_need_no_embedding(tmp_path):
    index = _index(tmp_path / "index.sqlite")
    client = QueryEmbedding()
    assert Retriever(index, client).retrieve("q").items == []
    index.replace([_item("item")])
    assert index.items(kinds=[]) == []
    assert Retriever(index, client).retrieve("q", kinds=["absent"]).items == []
    assert client.calls == 0
    with pytest.raises(ValueError):
        Retriever(index, client).retrieve("q", temporal_only=True)
    with pytest.raises(ValueError):
        Retriever(index, client).retrieve("q", start_sec=20, end_sec=10)
    index.close()
