"""embedding 增强功能：同源复述检测、对齐候选预筛、跨视频聚类。

三个功能都只做"候选生成与标记"，不改变分级与判定规则；
embedding 不可用时优雅降级，流程不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config as config_module
from .llm import EmbeddingClient, MockEmbeddingClient  # noqa: F401 - 类型引用
from .schemas import Claim, EvidenceRelation
from .vectors import VectorItem, VectorStore, cosine


def same_source_pass(
    claims: list[Claim],
    embedding_client,
    threshold: float,
    video_id: str,
    provenance_verified: bool = True,
) -> tuple[list[EvidenceRelation], list[dict]]:
    """同视频内陈述两两余弦 ≥ threshold → same_source 边（只标记，不删除）。"""
    if embedding_client is None or len(claims) < 2:
        return [], []
    import time

    started = time.monotonic()
    vectors = embedding_client.embed([claim.statement for claim in claims])
    embed_seconds = time.monotonic() - started
    relations: list[EvidenceRelation] = []
    records: list[dict] = []
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            score = cosine(vectors[i], vectors[j])
            if score < threshold:
                continue
            relations.append(
                EvidenceRelation(
                    from_id=claims[i].claim_id,
                    to_id=claims[j].claim_id,
                    relation="same_source" if provenance_verified else "possible_match",
                    proposition=("语义高度相似的复述陈述（不作为独立证据计数）" if provenance_verified else
                                 "疑似语义复述；来源关系未验证，不改变独立证据计数"),
                    reason=f"embedding 余弦相似度 {score:.3f} ≥ {threshold}",
                    created_by="embedding_same_source" if provenance_verified else "embedding_resemblance",
                )
            )
            records.append(
                {
                    "claim_a": claims[i].claim_id,
                    "claim_b": claims[j].claim_id,
                    "similarity": round(score, 4),
                    "text_a": claims[i].statement[:60],
                    "text_b": claims[j].statement[:60],
                    "video_id": video_id,
                }
            )
    # 进度与效果输出：嵌入耗时、配对规模、命中数与最相似的几对
    model_name = getattr(embedding_client, "model", "mock-embedding")
    pair_count = len(claims) * (len(claims) - 1) // 2
    print(
        f"  [embedding] 同源检测：{len(claims)} 条陈述（{pair_count} 对比较，"
        f"嵌入 {embed_seconds:.1f}s，模型 {model_name}）",
        flush=True,
    )
    if records:
        top = sorted(records, key=lambda r: r["similarity"], reverse=True)[:3]
        print(
            (f"  [embedding] 同源命中 {len(records)} 对 → 标记 same_source。最相似样例："
             if provenance_verified else
             f"  [embedding] 疑似复述 {len(records)} 对（仅标记候选，不确认同源）。最相似样例："),
            flush=True,
        )
        for pair in top:
            print(
                f"      {pair['similarity']:.3f}  [{pair['text_a'][:32]}] <-> [{pair['text_b'][:32]}]",
                flush=True,
            )
    else:
        print(
            f"  [embedding] 同源命中 0 对（阈值 {threshold}：本视频无高度相似复述）",
            flush=True,
        )
    return relations, records


def build_prefilter_retrieval(
    claims: list[Claim],
    event_clusters,  # list[EventCluster]
    embedding_client,
    top_k: int,
) -> tuple[dict[str, set[str]], list[dict]]:
    """视频级陈述 → 语义 top-k 事件；时间级陈述不受限制（由 build_candidates 处理）。

    返回 (retrieval, 筛除日志)。筛除日志保证预筛决策可追溯。
    """
    if embedding_client is None:
        return {}, []
    video_level = [c for c in claims if c.start_sec is None or c.end_sec is None]
    if not video_level or not event_clusters:
        return {}, []

    store = VectorStore()
    store.upsert_many(
        [
            VectorItem(
                item_id=cluster.event_local_id,
                video_id=cluster.event_local_id.split("::")[0],
                kind="event",
                text=cluster.summary or cluster.phenomenon_type,
                vector=event_vector,
                model=getattr(embedding_client, "model", ""),
            )
            for cluster, event_vector in zip(
                event_clusters, embedding_client.embed([c.summary or c.phenomenon_type for c in event_clusters])
            )
        ]
    )

    claim_vectors = embedding_client.embed([claim.statement for claim in video_level])
    retrieval: dict[str, set[str]] = {}
    filtered_log: list[dict] = []
    all_event_ids = {cluster.event_local_id for cluster in event_clusters}
    kept_total = 0
    removed_total = 0
    for claim, claim_vector in zip(video_level, claim_vectors):
        top = store.top_k(claim_vector, top_k, kinds=["event"])
        # 保护：top 结果为空（如向量异常）时该陈述回退全量候选，绝不静默丢失
        if not top:
            retrieval[claim.claim_id] = set(all_event_ids)
            continue
        allowed = {item_id for item_id, _ in top}
        retrieval[claim.claim_id] = allowed
        kept_total += len(allowed)
        removed = sorted(all_event_ids - allowed)
        removed_total += len(removed)
        if removed:
            filtered_log.append(
                {
                    "claim_id": claim.claim_id,
                    "statement": claim.statement[:60],
                    "kept": sorted(allowed),
                    "filtered_out": removed,
                    "note": "视频级候选被 embedding 预筛；时间级候选不受影响",
                }
            )
    store.close()

    # 进度与效果输出：候选对规模变化与分布统计
    full_pairs = len(video_level) * len(event_clusters)
    kept_pairs = kept_total
    if video_level:
        dist: dict[int, int] = {}
        for claim in video_level:
            dist[len(retrieval.get(claim.claim_id, set()))] = dist.get(len(retrieval.get(claim.claim_id, set())), 0) + 1
        dist_text = "，".join(f"{k}个事件×{v}条" for k, v in sorted(dist.items(), reverse=True))
        print(
            f"  [embedding] 对齐预筛：{len(video_level)} 条视频级陈述 × {len(event_clusters)} 个事件"
            f"（全量 {full_pairs} 对）→ 预筛后 {kept_pairs} 对送 LLM"
            f"（筛除 {removed_total} 对，-{removed_total * 100 // max(1, full_pairs)}%）；"
            f"分布：{dist_text}",
            flush=True,
        )
    return retrieval, filtered_log


# ---------------------------------------------------------------------------
# 跨视频聚类（同一事故 / 转载分组）
# ---------------------------------------------------------------------------


def run_crossvideo(args) -> int:
    """crossvideo 子命令：对一次运行的全部事件做跨视频语义聚类。"""
    from .config import load_config
    from .llm import create_embedding_client

    config = load_config(args.config)
    run_dir = config_module.PROJECT_ROOT / "runs" / args.run_id
    if not run_dir.is_dir():
        print(f"[失败] 运行目录不存在: {run_dir}")
        return 1

    entries: list[dict] = []
    for events_file in sorted(run_dir.glob("videos/*/events.jsonl")):
        video_id = events_file.parent.name
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            entries.append(
                {
                    "item_id": event["event_id"],
                    "video_id": video_id,
                    "text": f"{event.get('phenomenon_type', '')}：{event.get('phenomenon_summary', '')}",
                    "time": [event.get("start_sec"), event.get("end_sec")],
                }
            )
    if not entries:
        print("[失败] 该运行没有事件可聚类")
        return 1

    client = create_embedding_client(config, budget=None, cache=None)
    if client is None:
        print("[失败] embedding 不可用（未配置密钥且非 mock 模式），无法聚类")
        return 1
    if client.is_mock:
        print("[提示] 当前为 mock embedding，聚类结果仅用于流程验证")

    threshold = float(args.threshold if args.threshold else config.embedding.get("crossvideo_threshold", 0.80))
    vectors = client.embed([entry["text"] for entry in entries])
    store = VectorStore()
    store.upsert_many(
        [
            VectorItem(
                item_id=entry["item_id"],
                video_id=entry["video_id"],
                kind="event",
                text=entry["text"],
                vector=vector,
                model=getattr(client, "model", ""),
                meta={"time": entry["time"]},
            )
            for entry, vector in zip(entries, vectors)
        ]
    )

    groups = []
    for members in store.clusters_by_threshold(threshold, cross_video_only=False):
        videos = {m.video_id for m in members}
        if len(members) < 2:
            continue
        pairs = [
            {"item_a": a.item_id, "item_b": b.item_id, "similarity": round(score, 4)}
            for a, b, score in store.pairs_above(threshold)
            if a.item_id in {m.item_id for m in members} and b.item_id in {m.item_id for m in members}
        ]
        groups.append(
            {
                "group_id": f"cg{len(groups) + 1:02d}",
                "cross_video": len(videos) > 1,
                "videos": sorted(videos),
                "members": [
                    {
                        "event_id": m.item_id,
                        "video_id": m.video_id,
                        "time": m.meta.get("time"),
                        "summary": m.text[:100],
                    }
                    for m in members
                ],
                "similarity_pairs": pairs[:50],
            }
        )
    # 跨视频组排前
    groups.sort(key=lambda g: (not g["cross_video"], g["group_id"]))
    store.close()

    payload = {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "threshold": threshold,
        "embedding_model": getattr(client, "model", ""),
        "is_mock": bool(client.is_mock),
        "note": "语义聚类仅提示可能的同源/转载关系，不构成已核实结论",
        "total_events": len(entries),
        "groups": groups,
        "cross_video_groups": sum(1 for g in groups if g["cross_video"]),
    }
    output = run_dir / "crossvideo_groups.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"事件总数 {len(entries)}；多成员组 {len(groups)}（其中跨视频 {payload['cross_video_groups']} 组）")
    for group in groups[:10]:
        tag = "跨视频" if group["cross_video"] else "同视频"
        print(f"  [{tag}] {group['group_id']}: {', '.join(group['videos'])}（{len(group['members'])} 个事件）")
    print(f"已写入 {output}")
    return 0
