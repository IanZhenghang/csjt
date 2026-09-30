# 设计决策记录（DECISIONS）

实施过程中对计划书的补充决策与偏差记录。倒序追加。

## D-2026-09-27-03 证据 RAG 与逐条有限并发

按用户确认方案实施 rag-v1：共享持久证据索引（原始/派生版本隔离），时间必选+BM25/语义并集，模型提示词真正只包含候选事件。保留逐条陈述精判，最多并发2；逐事件审查/归因不合并。新增完整EvidencePack、候选白名单、逐条检查点指纹、单协调器落盘、共享节流/429退避、embedding预算与逐POST用量。补证ID按事件隔离，恢复重建完整证据，媒体传输保持每次最多8帧。

修正旧记录口径：D-2026-09-27-02中的“调用量约降80%”指候选对数缩减，旧实现每条陈述仍一次请求、且背景仍含全部事件，不能据此推断API次数或耗时下降80%。RAG分别统计候选对、提示词字符、请求、tokens与墙钟时间，不预先承诺固定提速。

语义相似在rag-v1只标为疑似复述possible_match，不确认同源。旧run恢复legacy与原配置快照；真实语义召回/反证覆盖仍待人工基准与live对照。新增评估工具不会把未标注或mock输出当作效果证据。

## D-2026-09-27-02 用户决策：对齐启用 embedding 预筛
用户选择放弃"每条陈述 × 全部事件"的最细粒度（v2 式 24 事件下单视频对齐约 150 次调用），改为预筛 + 精判：视频级陈述按 embedding 余弦只取 top-5 事件送 LLM；时间重叠/邻近候选始终保留（不受预筛影响）；被筛除的对落盘 align_prefilter_log.json 可追溯。对齐调用量约降 80%（v2 约 150 → 约 40 次内）。
工程防线：top_k 结果为空时该陈述回退全量候选（不静默丢失）；embedding 不可用时预筛自动退回全量并明示；batch_size 钳制到官方接口上限 10。
代价与缓解：语义相似度低但真实相关的对可能被筛掉（召回风险）——由筛除日志 + 人工抽查对齐结果监控；若抽查发现明显漏挂，可调大 prefilter_top_k 或关回预筛。

## D-2026-09-27-01 embedding 模型与批量参数（实测确认）
- 用户 .env 配 EMBEDDING_MODEL=text-embedding-v3 + 官方百炼端点（EMBEDDING_BASE_URL/EMBEDDING_API_KEY），对话仍走 token-plan 中转；doctor 显示的 v4 只是探测候选顺序，实际以 .env 为准。
- 官方 embedding 接口单请求批量上限 10 条（v3/v4 均是）：batch_size 默认改为 10 并在客户端钳制，修复"batch size is invalid, it should not be larger than 10"导致同源检测跳过的问题。

## D-2026-09-26-09 embedding 增强模块（轻量向量检索）
按用户要求加入 embedding 能力（计划书 §1.3 首版排除向量数据库；本扩展不部署外部服务，SQLite 存向量 + 纯 Python 余弦，数据规模每视频数百条）。三个功能，均只做候选生成与标记、不改变分级与判定规则：
- **同源复述检测**（默认开）：同视频内陈述余弦 ≥0.90 → same_source 证据图边 + claims_samesource.json，只标记不删除（§7.6）；
- **对齐候选预筛**（默认关 align.use_embedding_prefilter）：视频级陈述语义 top-k 事件；时间级候选始终保留；筛除对落盘 align_prefilter_log.json 可追溯；
- **跨视频聚类**（独立命令 crossvideo --run-id）：事件摘要语义聚类发现同一事故/转载分组（§14.5 事件来源组），产物 crossvideo_groups.json。
工程要点：文本级缓存（复用 ResponseCache）；mock 模式自动降级为确定性哈希向量（离线测试可复现）；live 运行开始探测一次（配置模型→候选回退→全失败整轮禁用并明示）；支持独立端点/密钥（EMBEDDING_BASE_URL/EMBEDDING_API_KEY，便于"对话走中转、embedding 走官方"）。doctor --live 新增第 5 项 embedding 探测。
实测：用户的 token-plan 中转端点不支持任何 embedding 模型（全部 Model not exist / 404）——当前 live 运行自动禁用；需启用时在 .env 配置官方百炼端点与密钥。107 项测试全绿。

## D-2026-09-26-08 对齐粒度可配：默认每条陈述单独请求
用户选择最细粒度（单对判断召回最可靠）。`align.claims_per_request`（默认 1）：每次对齐请求包含的陈述条数；每次请求仍展示全部事件完整摘要。代价：对齐调用数 = 陈述数（v2 约 150 次、约 25 分钟），批量运行需相应调大 --max-live-calls。语义上每对判断独立，批处理不减少可见信息，仅增加漏判/ID 混淆风险——粒度细消除该风险。

## D-2026-09-26-07 用户决策：严谨配置 + 思考开关由 .env 全局控制
用户确认：延迟主因是思考模式，选择"保持最严谨的提示词内容 + 思考全局由 .env 控制"。落实：
- `align.event_summary_chars` 默认改为 0（不截断，对齐阶段读取完整事件摘要）；
- `quality.thinking_tasks` 保持默认 []（不按任务开思考），思考模式只由 `.env` 的 `LLM_ENABLE_THINKING` 全局控制（当前 false，且已实测真正生效：payload 显式传 enable_thinking 布尔值）；
- 保留的质量中性提速项：对齐按陈述分批（修复截断崩溃，非质量妥协）、只输出非 unrelated 关联（省略即无关，语义等价）、429 退避与节流。

## D-2026-09-26-06 质量分级开关：按任务开思考 + 对齐摘要长度可配
快速模式（思考全关 + 对齐摘要 80 字符）的质量代价分析：
- 思考关闭对提取类任务（text_extract/visual_observe）影响有限；对判定类任务（review 审查 / attribution 归因）影响最大；
- 摘要截断**只影响对齐阶段的匹配线索**——审查与归因的提示词始终逐条读取完整画面观察，不受该参数影响。
新增两个配置（均进入缓存键，支持 A/B 对比）：
- `quality.thinking_tasks`（默认 []）：全局 LLM_ENABLE_THINKING=false 时仍对这些任务开思考；建议需要质量时设 ["review","attribution"]（v2 的 24 事件约 +25 分钟）；
- `align.event_summary_chars`（默认 80，0=不截断）：对齐提示词中每个事件的摘要长度。
请求级 `LLMRequest.enable_thinking` 覆盖 > 任务白名单 > 全局开关，三级优先。质量对比实验：同一包分别用快速/质量两套配置各开新运行，对比 report.md 的关联分布与事件判定。

## D-2026-09-26-05 延迟定位：enable_thinking 必须显式传 false（A/B 实测）
对运行 20260926-100155-live 的 usage.jsonl 分析：墙钟 2011s 中模型调用占 1996s（流程自身开销仅 15s，含节流/缩放/状态写入）——慢的根源是模型调用，对齐单次 320–407s。A/B 探测证实：payload 省略 enable_thinking 时服务端按默认开启思考（10.1s），显式 false 仅 4.0s（2.5 倍差距，大提示词下放大到分钟级）。此前"关闭思考"实际未生效。修复：
- openai_compatible 方言**始终显式传 `enable_thinking: <bool>`**，不再省略；
- 对齐提示词中的事件摘要截断到 80 字符（24 个事件的输入体积减半）；
- 运行结束统计新增各阶段调用的平均/最大耗时（storage.usage_summary.by_task），此后延迟问题可一眼定位；
- 复核方法：分析 runs/<id>/usage.jsonl 的 duration_ms（注意 duration_ms 含节流与重试等待）。

## D-2026-09-26-04 对齐输出体积与截断健壮性（首个 qwen 运行实测发现）
真实运行中 qwen 产生 24 个事件（现象类型多样，不像 mock 全并成 1 个），对齐单批输出 10 陈述 × 24 事件 = 240 条关联超出 max_tokens 触发截断，且对齐阶段未接住截断标记导致 KeyError 崩溃。修复：
- 对齐提示词改为**只输出非 unrelated 的关联**（无关对省略即视为无关），单批输出量随实际相关对数走（通常为候选对的少数），配合每批 10 条陈述稳定在 token 上限内；全部无关时返回空数组。
- 流水线四个消费点（对齐/视觉/审查/归因）统一接住 `__truncated__` 标记：任务记为 `:truncated` 失败、计入 task_failures、视频状态转 partial，绝不崩溃。
- MockLLMClient 故障注入支持任务限定键（`"align:truncated"`），用于回归测试截断场景。

## D-2026-09-26-03 live 运行提速（首个 qwen 运行近 1 小时未完成）
实测瓶颈：qwen3.8-flash 开思考模式单次调用 30–90s（最大头）；对齐按候选对分批（陈述×事件/40 次，v2 可达 20–40 次）；8s 节流。优化：
- **对齐分批重构**：从"每 40 对候选一批发"改为"每 10 条陈述一批、对全部事件一起判"，调用次数 ≈ 陈述数/10，与事件数无关（v2：约 30 次 → 约 15 次）。每批打印进度行。批次内候选对语义不变（评论仍只做视频级候选）。
- **时间文本合并批次**（config `text.time_text_batching`，默认 per_window 保持计划书 §7.3 与 resume 任务标签兼容；merged 可选）：合并全部窗口的 ASR/OCR spans 去重后按上限拆批，每视频约省 14 次调用；记录自带时间戳，不损失时间信息。批量运行 10 包时建议开启。
- **思考开关**：`LLM_ENABLE_THINKING=false` 可大幅降低单次时延（提取/分类类任务质量影响有限）；判定类任务（审查/归因）如需更高质量可再开回。
- **节流默认建议 3s**（阿里云专用端点；智谱共享接口仍建议 8）。
- 更大的提速空间（并发请求 LLM_MAX_CONCURRENCY>1）留待后续，当前保持顺序执行以简化状态管理。

## D-2026-09-26-02 供应商抽象：切换阿里云百炼（OpenAI 兼容端点）
智谱 glm-5.3-flash 账户限流持续（首次运行后 11 分钟仍 429，见 D-10），用户决定切换到阿里云百炼专用端点（qwen3.8-flash，OpenAI 兼容协议）。实施：
- `BigModelLLMClient` 重构为通用 OpenAI 兼容客户端（保留原类名做别名）：`LLM_PROVIDER=zhipu` 用 `thinking.type=enabled + reasoning_effort` 非流式方言；`LLM_PROVIDER=openai_compatible` 用 `enable_thinking=true` + SSE 流式方言（等价于用户示例中的 `extra_body={"enable_thinking": True}` + `stream=True`），流式只拼接 `content` 增量，`reasoning_content` 按计划书 §9.2 不进入业务输出。
- 供应商默认按 base_url 推断（含 bigmodel.cn → zhipu，否则 openai_compatible）；`LLM_STREAM` 可强制覆盖；400 提示要求流式时自动翻转一次（不消耗重试次数）；400 拒绝 `stream_options` 时自动移除一次。
- 密钥支持 `DASHSCOPE_API_KEY` 作为 `LLM_API_KEY` 未配置时的备用来源。
- 同步改进运行体验（修复"长时间没反应像卡死"）：① 退避/节流等待时打印 `[llm]` 进度提示；② 每个任务开始/结束打印状态；③ 连续 3 个任务因 429 失败 → 自动暂停（stop_reason=rate_limited，任务转 paused_budget，退出码 5），不再干等；④ Ctrl+C 优雅中断并保存状态（退出码 130）。
- 注意：模型/端点变更使缓存键失效（符合 §11.4），切换供应商后应开新运行，不得 resume 混用不同模型的旧结果。

## D-2026-09-25-10 live 限流处理（首次真实运行发现）
首次 live 运行（run 20260925-222140-live）实测：glm-5.3-flash 约 10 次请求/分钟后触发 HTTP 429（11 次成功调用集中在 94 秒内，其后 28 个任务全部因 429 失败；响应无 Retry-After 头）。处理：
- 新增 `LLM_MIN_REQUEST_INTERVAL_SECONDS`（.env，建议 8）：两次真实 POST 的最小间隔节流；
- 429 无 Retry-After 时退避从 2–10s 加长为 20s/40s（分钟级限流窗口）；
- 失败任务可 `resume` 重跑，已成功任务经任务状态与缓存双重跳过，不重复计费。
已成功调用的真实输出通过了全部三层校验（含 extra=forbid 结构校验），文本提取质量符合预期（保留"声称/称"来源语气、引用合法）。

## D-2026-09-25-09 doctor --live 实测通过
四项能力探测全部为 True：文本、文本 JSON mode、视觉、图像+JSON 组合（glm-5.3-flash 对图像输入支持 response_format）。能力记录写入 work/capabilities_latest.json。

## D-2026-09-25-08 自动补证的统一实现
`request_more_frames` 与 `request_original_resolution` 首版统一实现为"高分辨率局部补帧重观察"：以事件中心 ±3s（≤6s）、4Hz、ffmpeg 原质量（-q:v 2，不做缩放）补帧后重新观察，每事件最多一轮（§7.7）。两种请求类型的差异化处理（如只提升分辨率不增加帧数）留待后续版本；其余三种请求进入人工复核队列。

## D-2026-09-25-07 system_engagement 保守判定
- `observed_active` 首版不自动授予：无法区分原生车机界面与发布者叠加文字/回放屏幕时，按计划书 §6.4 保持 `reported_active` 或 `unknown`；业务校验（§10.3）会拒收自动出现的 observed_active。
- 存在 system_state_claim 类陈述（字幕/标题/评论声称系统开启）→ `reported_active`。
- 现象类型为 `uncertain` 的事件不判 `suspected`：画面覆盖不等于现象确认。

## D-2026-09-25-06 陈述去重键
跨窗口/跨批次重复的 Claim 按 `(claim_type, statement[:120], 排序后 evidence_ids)` 去重，满足 §13.2"多窗口引用相同 ASR 不重复计算为独立证据"；原始记录仍在证据库中可回溯。

## D-2026-09-25-05 证据等级的确定性规则
- 现象 high：≥2 条含帧引用且相邻/重叠的观察（visibility clear/partial）且无未解冲突；medium：≥1 条清晰观察；low：仅有陈述支持；insufficient：无支持。
- 系统状态 high 需人工核验（数据中 human_reviewed 全为 false，实际不会出现）；medium：观察声称可见状态显示；low：仅来源声称。
- 归因 high 首版不自动授予；medium：假设支持证据含帧证据；low：仅有非帧支持或纯推测。

## D-2026-09-25-04 v2+ H 帧是完整道路画面（修正 D-03 相应条目）
实测验证（尺寸 + 视觉确认）：v2+ `frames_ocr/half_*.jpg` 与 F 帧同分辨率（1920×1080，v7 为 720×1280 竖屏）且为完整道路驾驶视角画面（半秒位置整帧），并非仪表区裁剪。与计划书 §2.1"1417 张全画面图"一致（1528 总帧数 − v1 的 111 张字幕条带 = 1417）。因此 v2+ 的 F/H 帧统一标记 `full_frame`；仅 v1 的 `ocr_crops/` 条带图标记 `subtitle_crop` 并排除出视觉输入。D-03 中"H 帧标记 other_crop"的表述作废。

## D-2026-09-25-03 数据字段映射的实测确认（T02）
- v1 `text/ocr.jsonl` 每条记录同时含 `text_id`（O 前缀，对外引用 ID）与 `ocr_id`（R 前缀，原始识别 ID）；segment_inputs 的 `ocr_ids` 引用的是 `text_id`。适配器以 `text_id` 为统一 OCR 主键，`ocr_id` 存入 `raw_record_ids`；记录缺 `text_id` 时明确报错（不猜测兼容）。
- v2+ `text/asr.jsonl` 主键为 `candidate_id`（AC 前缀），但 segment_inputs 的 `asr_ids` 引用 `utterance_id`（A 前缀）。适配器统一以 `utterance_id` 为 ASR 主键，`candidate_id` 存入 `raw_refs`。
- metadata.json 提供 title / description_text（可为 null）/ published_at / url / model=null / software_version=null。
- Windows 文件名禁止 `:` 等字符，任务输出文件名经统一清洗（曾因批次标签含 `:` 触发 OSError 22，已修复）。

## D-2026-09-25-02 复用既有 venv
项目目录已有空 `.venv`（Python 3.12.10，无包），直接 ensurepip + 安装，不重建。锁定文件 requirements.lock.txt（pydantic 2.13.5、httpx 0.28.1、Pillow 11.x、PyYAML 6.x、pytest 8.x）。

## D-2026-09-25-01 本轮实施范围
按用户指示执行完整计划书（T01–T12）。本轮无 API 密钥：全部以 mock 模式实现并验证；live 调用路径实现但不执行，待用户配置 `.env` 后自行运行。mock 输出全部标记 is_mock=true 且在报告中注明"占位输出，不代表真实视觉分析"。
