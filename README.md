# 智能驾驶缺陷线索提取与归因 Agent（研究原型）

从社交媒体视频数据包（标题、简介、ASR、OCR、评论、帧图）中提取智能驾驶相关异常线索，
区分"可观察现象 / 来源陈述 / 候选归因"三个层次，输出可追溯、可复核的结构化结果与 Markdown 报告。

- 实施依据：[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)（T01–T12 任务编号均指向该文档）
- 当前进度：[STATUS.md](STATUS.md)
- 设计变更记录：[DECISIONS.md](DECISIONS.md)

## 证据 RAG（rag-v1）

新运行默认使用 `rag-v1`，保留五个角色、全片首轮观察和逐条陈述精判。
共享 SQLite 索引位于 `work/evidence_index.sqlite`；视频级陈述使用语义 top-5 与
BM25 top-5 并集，时间陈述保留重叠/邻近 2 秒候选。向量失效时退回全候选。
每次对齐只发送当前陈述及其候选事件，默认最多同时执行 2 条独立对齐请求。

审查与归因读取完整 `EvidencePack`，含原文、时间、来源、观察限制与合法引用 ID。
原始材料中的指令仍视为数据。补充检索项不自动成为支持关系；相似陈述不自动确认同源。
陈述对齐的证据包及拼接后的提示文本、审查与归因的事件证据包均不设程序字符上限。
完整保留必需证据与有效补充候选，旧运行配置中的 `rag.max_context_characters` 不再限制这些阶段。
缺失引用或跨视频证据仍会输出 `partial / incomplete_context` 和复核项；模型服务自身的上下文容量仍适用。

配置在 `config/default.yaml`：`pipeline.version: rag-v1`、
`rag.align_max_workers: 2`（改为 1 可做串行对照）。
新运行可显式使用 `pipeline.version: legacy` 做原方案对照；旧运行的 `resume` 固定使用
原 manifest 和配置快照，缺版本的历史运行视为 legacy，不自动升级。

每条陈述保存独立检查点与输入指纹。`tasks/retrieval_*.json` 记录检索轨迹，
`tasks/evidence_pack_*.json` 记录实际上下文，`tasks/alignment_metrics.json` 与
`performance.json` 分别记录对齐输入规模和处理耗时。补证帧/观察 ID 按事件隔离，
恢复会重建完整证据记录。新运行的 usage 按真实 POST（含重试/修复/embedding）统计；
候选对数、请求次数和 token 用量分别报告。

验证与人工基准说明见 [docs/RAG_BENCHMARK.md](docs/RAG_BENCHMARK.md)。
Mock 只验证工程行为；真实召回、反证覆盖和供应商端耗时需完成 live 对照与人工复核后评价。

## 安装

```powershell
Set-Location 'D:\数据汇总\driving_defect_agent'
py -3.12 -m venv .venv   # 已存在则跳过
& .\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

依赖锁定版本见 `requirements.lock.txt`（由 `pip freeze` 生成，接手者勿自行升级）。

## 常用命令

```powershell
$agentPython = '.\.venv\Scripts\python.exe'

& $agentPython -m defect_agent doctor                 # 离线自检
& $agentPython -m defect_agent doctor --live          # 联网能力探测（需在 .env 配置 LLM_API_KEY）
& $agentPython -m defect_agent inspect --data-root 'D:\数据汇总\数据汇总'
& $agentPython -m defect_agent normalize --data-root 'D:\数据汇总\数据汇总'
& $agentPython -m defect_agent run --package 'D:\数据汇总\数据汇总\v2' --mode mock
& $agentPython -m defect_agent run --package 'D:\数据汇总\数据汇总\v2' --mode live --max-live-calls 80
& $agentPython -m defect_agent batch --data-root 'D:\数据汇总\数据汇总' --mode mock
& $agentPython -m defect_agent resume --run-id '<已有运行ID>'
& $agentPython -m defect_agent evaluate --run-id '<已有运行ID>' --annotations 'annotations\annotation_template.jsonl'
& $agentPython -m defect_agent stability
& $agentPython -m pytest -q
```

## 模型输出格式修复

文本提取、画面观察、陈述对齐、审查与归因等结构化输出，必须通过 JSON 解析和
对应 schema 校验，才能作为成功结果继续处理。格式失败时，程序会把最新一轮输出、
具体校验错误和完整 schema 一起交给模型，要求修正字段名、必填字段、类型及枚举值，
每轮重新校验；仅保存通过校验的结果。

`budget.max_format_repairs_per_result` 默认是 `3`，表示首次输出失败后最多追加
3 轮修复（最多 4 次逻辑生成；网络错误的重试单独计数）。`0` 表示禁用格式修复，
负数与非整数配置会被拒绝。所有真实请求仍计入调用次数和 token 预算，也受请求时限约束。
达到修复上限、预算或时限仍未成功时，该任务保留失败或暂停状态，后续可用 `resume`
重试；不合格输出不会被当作成功结果传给后续阶段。格式合格只表示结构满足要求，
不代表结论已被验证。

`run`、`batch` 和 `resume` 都支持 `--max-format-repairs N` 显式覆盖次数。
旧运行的 `resume` 默认保留原配置快照中的次数（例如 1），要为本次恢复增加修复机会，执行：

```powershell
& .\.venv\Scripts\python.exe -m defect_agent resume --run-id 20260927-103649-112977-live --max-format-repairs 3
```

## 边界（必须遵守）

- 原始数据目录只读；所有生成文件写入本项目目录。
- 默认 mock 模式；真实 API 调用需显式 `--mode live` 且密钥只从环境变量 / `.env` 读取。
- 模型输出仅为候选结论，`verification_status` 固定 `unverified`；系统不得声称已证实根因。
- 详见 IMPLEMENTATION_PLAN.md §3.3 固定边界。
