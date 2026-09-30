# Evidence RAG 人工基准与性能比较

评估工具只读取已生成的运行产物，不调用模型，不生成或猜测人工标签。离线单测通过只能说明检索、记录与评估流程按约定工作，**不能声称已经验证真实语义召回**。

## 1. 从 RAG 运行准备待复核材料

```powershell
.\.venv\Scripts\python.exe -m defect_agent.rag_evaluation prepare --run-dir runs/<RAG运行ID> --output annotations/rag_benchmark.json
```

工具读取 `videos/*/tasks/retrieval_align_*.json`、`claims.jsonl`、`events.jsonl`、观察及规范化证据。在 v2、v4、v7、v10 中，按来源类型及视频早/中/晚时间区域轮转选取每包最多 12 条查询，无时间陈述单独覆盖。`package_summary` 明示缺失视频、缺失检索轨迹、样本不足和材料缺口；旧运行没有 RAG 轨迹时，不会伪造检索结果。

`items` 保留查询原文、原始证据 ID、时间、所有候选事件与实际选中事件；`materials` 提供事件目录和对应原始证据，包括媒体路径。事件 ID 使用本视频局部 ID，如 E001；不同视频的同名 ID 不能混用。模型摘要是复核线索，标签必须核对原文及可用的视频帧/片段。

所有期望标签初始均为 `null`，`review_status` 为 `pending`。`null` 表示尚未标注，不能改写为“没有关联”。人工复核每条查询后填写：

- `expected_event_ids`：所有应该检索到的相关事件，含支持或反驳该陈述的事件。
- `expected_contradicting_event_ids`：应被找回的反证事件。
- `expected_system_state_event_ids`：应被找回的系统状态相关事件。
- `counterevidence_reviewed`、`system_state_reviewed`、`replay_reviewed`：确认检查过反证、系统状态和剪辑/回放歧义后设为 `true`。无法复核时保留待复核，不猜测答案。
- `reviewer_id`、`reviewed_at`、`review_notes`、`replay_notes`：记录复核者、时间及判断依据。
- 全部候选都已检查且标签完整后，设置 `annotation_complete=true`、`review_status="human_reviewed"`。

经人工确认不存在某类目标时填写空列表 `[]`。先独立检查完整事件目录，再看 `selected_ids`，降低被当前检索结果影响标签的风险。不要只给选中事件打标签。该小基准覆盖有限，应另保留扩展样本评估泛化能力。

## 2. 计算人工标注检索召回

```powershell
.\.venv\Scripts\python.exe -m defect_agent.rag_evaluation score --benchmark annotations/rag_benchmark.json --run-dir runs/<RAG运行ID> --output experiments/rag_recall.json
```

只有 `human_reviewed` 且标注和复核字段完整的条目进入指标。相关、反证、系统状态分别报告命中数/期望数及微平均 recall。没有正例时 recall 为 `null`；未标注项和无法匹配的运行项列入 `excluded`，不计成零分。跨运行使用查询原文、原始引用与时间匹配，同时核对源版本及事件目录；源文件、查询或事件发生变化时必须重新复核。

mock 向量不具备真实语义能力：即使 mock 得分为 1，也只验证评估流程。live 且存在人工正例时 `live_human_labeled_recall_measured=true` 表示测过这些标签上的召回，**不表示证明语义模型有效或完整事件发现无遗漏**。查看逐条 `degraded`：全候选降级也能取得高召回，但没有检索节省。`semantic_effectiveness_proven` 不由工具自动授予。

## 3. 比较旧流程、RAG 串行和 RAG 并行

```powershell
.\.venv\Scripts\python.exe -m defect_agent.rag_evaluation compare --run-dirs runs/<旧运行> runs/<RAG串行运行> runs/<RAG并行运行> --output experiments/rag_comparison.json
```

模式由 manifest 的 `pipeline_version`（兼容 `pipeline_flags.version`）与 `config_snapshot.json` 的 `yaml.rag.align_max_workers` 判定：legacy、rag_serial、rag_parallel。比较相同视频和输入哈希、相同模型/推理参数、相同冷暖条件的完整 live 运行。串行与并行只改变 worker 数；不要同时修改抽帧、来源数据或质量相关设置。

运行准备时应在 manifest 显式记录 `benchmark_conditions.cache_state` 为 `cold` 或 `warm`，反映实际控制条件。**不要事后因缓存命中为零而填 cold**；已构建的向量索引、恢复任务及模型响应缓存都会影响时延。建议用独立实验目录/缓存测冷启动，再在相同输入与缓存下测暖启动，并分别比较。当前工具没有主动运行或清理缓存的命令。

比较表包含：各 task 的缓存记录、非缓存记录、可确认的真实 POST 数、embedding 统计、报告 tokens、429，以及每视频 `performance.json.wall_seconds` 和 `tasks/alignment_metrics.json` 的对齐耗时/候选规模/上下文字符数。所有缺失数据为 `null`；不将失败请求缺失的 token usage 当零消耗。`known_total_tokens` 只是已报告部分的总和，`reported_total_tokens=null` 表示整段统计不完整。

新版 manifest 应设置 `usage_accounting="post-attempt-v1"`：此口径每个真实请求尝试一条非缓存 usage，包含重试和格式修复。旧日志常聚合最终成功或修复结果，`non_cached_logged_events` 不能直接解释为实际 POST；其 `actual_posts` 和完整 429 次数保持 `null`。旧运行可比较已记录且条件一致的 wall time，不能直接计算实际请求降幅。

缺少冷暖声明、输入哈希或视频耗时，混合 mock/live，或模型条件不一致时，工具列出 `comparability_issues`。即使性能条件可比，也必须结合人工召回结果判断质量取舍。没有供应商价格及账单，费用始终为 `null`，不能用 token 数自行推算收费。

## 4. 可复现离线功能实验

```powershell
.\.venv\Scripts\python.exe scripts/validate_rag_offline.py --data-root 'D:\数据汇总\数据汇总' --output-root experiments/rag_validation
```

每次运行创建唯一时间戳子目录，依次在独立子进程和独立 PROJECT_ROOT 中执行 legacy、RAG 串行、RAG 并行三个方案。各方案使用全新缓存与索引，只处理 v2/v4/v7/v10，强制 mock 且阻断 HTTP；不读取密钥、不修改原始数据或用户 runs/work、不删除或覆盖旧实验目录。默认 YAML 的抽帧、预算及 24000 字上下文限制保持不变。

输出 `experiment_manifest.json`、`compare.json`、`manual_benchmark.json`、`score_unreviewed.json`，并保留每方案原始产物、独立日志与 worker 状态。manifest 显示 complete/partial/failed、缺失包和上下文失败。Mock 可能把大量陈述关联到同一事件而触发上下文上限；这些 partial 必须保留，不能提高上限后宣称通过。三方案间源码变化会停止实验，防止混用实现版本。

未标注基准不会产生真实召回分数。实验汇总中的实际 tokens 和费用保持 `null`；记录的耗时只用于排查离线执行和同步开销，不能据此声称真实 API 提速。
