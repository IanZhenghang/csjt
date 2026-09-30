# 当前项目状态

## 2026-09-27 证据 RAG 实施

rag-v1 已实现：共享SQLite证据索引、时间/BM25/向量混合检索、完整EvidencePack、逐条对齐检查点与并发2、补证唯一ID及完整恢复、逐POST用量和embedding计费记录。旧manifest恢复legacy；新manifest记录pipeline_version与usage_accounting。设计说明见 docs/RAG_ARCHITECTURE.md，评估步骤见 docs/RAG_BENCHMARK.md。

工程回归200项全通过；原始10包按自带SHA-256清单核对2168个文件，0缺失、0不匹配。三方案隔离离线实验正在验收；真实语义召回、反证覆盖和端到端live耗时尚未通过人工基准验证。下方早期状态保留为历史记录，不代表当前尚无live路径或embedding能力。

## 当前阶段
T01–T12 首版全部完成（离线 + mock 验收通过；live 路径已实现待密钥验证）

## 已完成
- T01 项目骨架：pyproject/config/.env.example/CLI（doctor、inspect、normalize、run、batch、resume、evaluate、stability）、离线 doctor、依赖锁定 requirements.lock.txt。
- T02 输入适配：PackageV1Adapter（schema 1.0.0）/ PackageV2Adapter（schema 1.0），未知结构报错；10 包 normalize 计数与计划书 §2.1 表完全一致。
- T03 数据校验：引用/时间/路径/计数检查；10 包 0 错误；11 条缺父评论按预期告警；inspect 命令产出 inventory.json + validation_report.json。
- T04 证据库与窗口：全局 ID `{video}::{kind}::{local_id}`、OCR span 与底层记录同源分组、6s+1s 窗口、1Hz 采样≤8 图、Pillow 缩放输出 derived_media、ffmpeg 补帧接口；v1 字幕裁剪图排除在视觉输入外；v6 尾段 0.099s 保留。
- T05 LLM 客户端：MockLLMClient（故障注入）与 BigModelLLMClient（重试/Retry-After/401 停止/一次格式修复/截断标记/预算/usage 记录/缓存）；doctor --live 能力探测（需密钥）。
- T06 文本提取：背景/时间文本/评论三批次，按记录边界拆批，父评论仅作上下文；每条陈述引用合法证据；跨窗口去重。
- T07 视觉观察：首轮仅帧+ID+时间戳；观察区分道路/界面/画面文字；局部补帧重观察（每事件最多一轮）。
- T08 对齐与证据图：确定性合并（同类+间隔≤1s+无剪辑疑点），剪辑疑点拆分并标记可能相关；评论只做视频级候选关联，不生成时间；evidence_graph.json 节点+边。
- T09 审查与归因：五种补证请求（仅前两种自动执行）、候选归因≤3 个 + 空假设路径、verification_status 固定 unverified。
- T10 规则与报告：三维度证据分级（attribution 不自动授予 high）、defect_assessment 判定（无 confirmed_defect）、业务校验降级、report.md + review_queue.jsonl + summary.md。
- T11 批量与恢复：SQLite 状态机（六种任务状态/三种视频状态）、原子写入、run/batch/resume；resume 验证零重复调用。
- T12 复核与实验：annotations/annotation_template.jsonl、evaluate 命令（IoU≥0.3 一对一匹配 + 0.5 敏感性、引用存在率、弃答比例，区分 mock/live）、stability 命令（3 次独立采样绕过缓存）、实验 A/B/C/D 预设（run --experiment）。

## 未完成
- 真实 API 调用验证（无密钥）：`doctor --live`、`run/batch --mode live` 代码路径已实现，待在 .env 配置 LLM_API_KEY 后运行。
- 人工标注与对照实验 A–D 的真实执行、加权融合模型（计划书 §8.4，属后续科研扩展）。

## 最近验证
- 命令：`.venv\Scripts\python.exe -m pytest -q`（84 项全绿：新增流式 SSE 解析、供应商方言、400 自动转流式、持续限流自动暂停等 8 项）
- 命令：`.venv\Scripts\python.exe -m defect_agent batch --data-root 'D:\数据汇总\数据汇总' --mode mock`
- 结果：10 包全部 complete，393 次调用 0 错误；每包生成 report.md/events/hypotheses/evidence_graph/review_queue。
- 命令：`resume --run-id 20260925-215445-mock` → usage.jsonl 393→393 行，成功任务零重复调用。
- 数据完整性：对照 10 包自带 checksums.sha256 全量校验 2168 个文件，0 不匹配（运行前后原始数据只读保证成立）。
- `evaluate`（is_mock=true，引用存在率 1.0）与 `stability`（3 次独立采样）命令验证通过。
- 日期：2026-09-26（供应商切换与运行体验改进后全量回归）
- 是否调用真实 API：否（测试全部离线 mock）

## 已完成（新增）
- embedding 增强模块（D-09）：同源复述检测（默认开，只标记不删除）、对齐候选预筛（默认关）、跨视频聚类命令 `crossvideo --run-id`。mock 自动降级、live 启动探测、支持 EMBEDDING_BASE_URL/EMBEDDING_API_KEY 独立端点。107 项测试全绿。注意：当前 token-plan 中转端点不支持 embedding，live 运行会自动禁用并提示；启用需在 .env 配官方百炼 embedding 端点。

## 已知问题
- **live 运行提速已实施**（2026-09-26，见 DECISIONS D-2026-09-26-03）：对齐改按陈述分批（≈陈述数/10 次调用）、时间文本合并批次（config 可选，批量建议开）、建议 `LLM_ENABLE_THINKING=false` + 节流 3s。qwen3.8-flash 开思考时单次 30–90s 是最大时延来源。
- **供应商已切换**（2026-09-26）：阿里云百炼 qwen3.8-flash（OpenAI 兼容端点，doctor --live 四项能力探测通过：文本/文本JSON/视觉/视觉JSON）。旧智谱 live 运行 20260925-222140/223449 **不要 resume 混用**；换模型后开新运行。
- mock 视觉观察为占位输出（uncertain 现象、标注"不代表真实视觉分析"），仅用于工程闭环；语义质量需 live 运行后人工抽检。
- `request_original_resolution` 与 `request_more_frames` 首版统一实现为"高分辨率局部补帧重观察"（4Hz、原分辨率、每事件一轮），差异化处理留待后续（见 DECISIONS D-08）。
- v2+ 的 ocr_spans 位于 manifest optional_source_data（非 QA 文件），已加载用于文本压缩并在 NormalizedPackage.warnings 说明。

## 下一步唯一任务
- 在 .env 配置 LLM_API_KEY 后执行 `& .\.venv\Scripts\python.exe -m defect_agent doctor --live`，验证文本/图像/JSON 组合能力并记录 capabilities.json。
- 随后 `run --package 'D:\数据汇总\数据汇总\v2' --mode live --max-live-calls 80` 做首个真实单包闭环，人工复核 report.md 中画面观察是否与实际内容相符。
- 完成标准：能力探测四项有明确结论；live 单包 complete；人工确认非 mock 输出区分标记正确。

## 最新运行
- run_id：20260925-215445-mock
- 结果目录：`D:\数据汇总\driving_defect_agent\runs\20260925-215445-mock`
- 模型与提示词版本：mock-model；提示词哈希见该目录 run_manifest.json（五角色 prompts/*.md）
