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