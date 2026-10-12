# Research OS paper-import MCP

这个服务是论文导入链路的独立副本，不调用 `research-os-backend`。它沿用仓库级
`data/workspace`：PDF、MinerU 结果、图谱草稿和 `paper_imports/jobs.sqlite` 与旧
backend 暂时共享。两个进程的 worker 使用同一把文件锁，同一时刻只有一个执行队列。

## 安装与启动

```bash
cd research-os-mcp
uv sync --extra pdf --extra graph
uv run --extra pdf --extra graph python -m paper_import_mcp.server
```

默认是本机 MCP `stdio` 模式，由 Claude、Codex 等客户端启动该命令。若需要本机
Streamable HTTP：

```bash
uv run --extra pdf --extra graph python -m paper_import_mcp.server --transport streamable-http --port 8001
```

连接地址为 `http://127.0.0.1:8001/mcp`。目前没有远程鉴权，服务拒绝绑定到非本机地址。
如果客户端进程设置了 `HTTP_PROXY`／`ALL_PROXY`，也要设置
`NO_PROXY=127.0.0.1,localhost`，否则本机 MCP 请求可能被代理转走。
配置沿用 `BACKEND_` 环境变量前缀，例如 `BACKEND_WORKSPACE_ROOT`、
`BACKEND_ELASTICSEARCH_URL`、`BACKEND_NEO4J_URI`。必须为图谱抽取提供
`BACKEND_LLM_API_KEY`：先在本项目的 `.env` 中填写。MCP 从任意工作目录启动时也会
读取这个文件；源码链接搜索可另设 `BACKEND_GITHUB_TOKEN`。

## 工具

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `create_paper_import_batch` | `task_root_id`，以及 `pdf_paths` 或 `paper_ids` | 保存 PDF 并入队，立即返回 `batch_id` |
| `get_paper_import_batch` | `batch_id` | 查询批次和逐篇阶段／状态／错误 |
| `retry_failed_paper_import_batch` | `batch_id` | 只重试失败论文 |
| `search_graph` | `query`，可选 `task_root_id`、`limit`（默认 50，范围 1–100） | 自然语言转只读 Cypher，返回 `cypher` 和 `records` |
| `browse_task` | `task_root_id` | 返回任务的方法类别、方法摘要、相关问题和数据集，不调用 LLM |
| `get_paper_context` | `paper_id`、`parse_version`，以及 `chunk_nos` 或 `block_ids`（各最多 50 个） | 精确取回 ES 原文、chunk 上下文、页码／bbox 和 PDF 定位链接，不调用 LLM |

`pdf_paths` 是 **MCP 服务所在机器上的绝对或相对路径**，不是远程客户端电脑的路径。
`paper_ids` 指已有的 `data/workspace/papers/<paper_id>/source.pdf`。
远程客户端上传 PDF 需要后续补独立上传入口；不要将大 PDF 编成 base64 作为工具参数。

## 使用 Prompt 示例

在客户端连接并命名此 MCP 为 `paper2graph` 后，可以向智能体发送：

```text
请使用 paper2graph MCP，导入 XX 目录下的所有 PDF 论文，task_root_id 为 XXX。
请向我汇报导入进度，监督直到全部完成。
```

例如，将目录替换为实际的绝对路径：

```text
请使用 paper2graph MCP，导入 /absolute/path/to/papers 目录下的所有 PDF 论文，task_root_id 为 medical_report_generation。
请向我汇报导入进度，监督直到全部完成，最后总结成功、失败的论文及失败原因。
```

目录必须位于 MCP 服务所在机器上，且智能体需要能列出其中的 PDF 文件。
智能体应先枚举文件，将具体路径通过 `pdf_paths` 提交给 `create_paper_import_batch`，
再使用 `get_paper_import_batch` 持续查询并汇报进度。收到 `batch_id` 只表示入队，
不代表导入完成；应等所有论文进入成功或失败的终态后汇总结果。

### 图谱查询

```text
请使用 paper2graph MCP 的 browse_task，查看 medical_report_generation 任务的方法类别、方法摘要、相关问题和数据集。
再使用 search_graph，查找该任务中使用对比学习的方法及相关局限，并返回论文和证据定位。
```

`search_graph` 使用 Text2Cypher，而非向量相似度检索。提供 `task_root_id` 时，
模型会被要求通过该任务论文的 Claim 筛选知识；生成的 Cypher 随结果返回，便于核验。
执行前通过 `EXPLAIN` 检查只读查询类型，禁止过程调用、外部数据加载和多语句，
并设置 10 秒数据库超时及服务端结果行数上限。

`browse_task` 使用固定查询，只收录该任务论文实际关联的方法、问题和数据集，
不通过共享 Family 直接判断任务归属。不存在的任务返回错误，不会创建节点。

### 原文证据与 PDF 定位

从图谱 Evidence 得到定位信息后调用：

```json
{
  "paper_id": "paper_...",
  "parse_version": "mineru-4.0.5:chunker-v1",
  "chunk_nos": [14],
  "block_ids": ["p3-b4"]
}
```

`chunk_nos` 和 `block_ids` 可以只传一个；同时传入时，block 必须在指定 chunks
内。不存在的编号或解析版本返回错误，不自动切换版本，也不静默丢弃缺失证据。
返回按阅读顺序排列的 `chunks`：

| 字段 | 含义 |
| --- | --- |
| `paper_id`、`parse_version`、`paper_title` | 原文身份及确切解析版本 |
| `pdf_path`、`pdf_url` | 存储的原 PDF 路径、HTTP 文件链接 |
| `bbox_coordinate_system` | `normalized_top_left`：左上角原点，0–1 归一化坐标 |
| `chunks[].chunk_no`、`section_path`、`content_type` | chunk 编号、章节和内容类型 |
| `chunks[].content`、`llm_content` | 完整 chunk 上下文；保留原文和 `[block: p3-b4]` 标签 |
| `chunks[].blocks[]` | 指定的 blocks；未指定 block_ids 时返回 chunk 的全部 blocks |
| `blocks[].block_id`、`type`、`text` | 稳定原文标识、类型、按 ES 字符区间还原的文本，不做 LLM 改写 |
| `blocks[].page_number`、`bbox` | PDF 物理页码（从 1 开始）、`[x0,y0,x1,y1]` 区域；`p3` 对应第 4 页 |
| `blocks[].viewer_url` | 可点击的跳页及高亮链接；缺少 bbox 时只能跳页 |
| `chunks[].assets` | ES 原有图片／表格等引用元数据，不返回图片二进制 |

查看器与 Streamable HTTP MCP 共用进程和端口，无需额外启动服务。例如端口
8002 下返回 `/papers/<paper_id>/viewer?...`，原 PDF 由
`/papers/<paper_id>/source.pdf` 提供，只允许读取论文存储目录内的文件。
查看器通过固定版本的 CDN 加载 PDF.js；PDF 本身仅从本机 MCP 服务读取。
stdio 模式没有 HTTP 查看器，URL 字段为 `null`，仍返回本地路径和原文；
原 PDF 已不存在时，路径和 URL 均为 `null`，ES 原文仍可读取。

代码只增加服务和展示两层：

```text
src/paper_import_mcp/
├── server.py                     # 注册工具和同端口查看器路由
├── services/paper_context.py     # 原文读取、引用核对和定位链接
├── infra/elasticsearch.py        # 复用 PIT 分页，增加 chunk／页范围筛选
├── paths.py                     # 复用存储路径，检查 PDF 文件访问边界
└── web/
    ├── paper_viewer.py           # 只读查看器和 PDF 文件响应
    └── pdf_viewer.html           # PDF.js 页面渲染及 bbox 高亮
```

### 测试

```bash
.venv/bin/python -m unittest discover -s tests -q
```

可选的真实 Neo4j 测试使用事务内临时夹具，完成后回滚，不保留测试节点：

```bash
PAPER_GRAPH_QUERY_TEST_NEO4J=1 .venv/bin/python -m unittest discover -s tests -p test_graph_query.py -v
```
