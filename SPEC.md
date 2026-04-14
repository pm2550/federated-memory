# 联邦式记忆文件系统 — 规范

**核心理念**: 本地工作区产记忆, 云端只收和存, MCP 让任何 AI 跨工作区读.

## 分工
- **md** = 沉淀知识 (人/AI 手工浓缩, 是经验资产)
- **session** = 完整过程 (原样归档, 不摘要, 不裁剪, 为了保留细节)
- **index** ≠ **summary** — 索引只是为了更快找到哪个文件, 最终返回原文

## 目录结构

```
/data/memory/
├── INDEX.md                    # 全局入口, AI 首次连上来先读这个
├── README.md                   # AI 客户端用法说明
├── _meta/
│   └── workspaces.json         # [{id, machine, tool, project, owner, created_at, updated_at}]
└── workspaces/
    └── <workspace_id>/         # id = slug(machine-tool-project), 例如 mymachine-claude-myproject
        ├── identity.json       # 该工作区完整身份
        ├── INDEX.md            # 该工作区的知识目录 (auto)
        ├── knowledge/          # md 文件 — 沉淀的知识
        │   ├── project_xxx.md
        │   └── feedback_xxx.md
        └── sessions/           # raw session 归档
            └── YYYY/MM/
                └── <filename>.jsonl.gz
```

## Workspace Identity

**必要字段**: `machine`, `tool`, `project`.
**生成 id**: `slug(machine)-slug(tool)-slug(project)`, 例如 `mymachine-claude-myproject`.

```json
{
  "id": "mymachine-claude-myproject",
  "machine": "mymachine",
  "tool": "claude-code",
  "tool_version": "2.1.87",
  "project": "myproject",
  "owner": "your-username",
  "branch": "release",
  "cwd_hint": "~/projects/myproject",
  "created_at": "2026-04-11T08:30:00Z",
  "updated_at": "2026-04-11T08:30:00Z"
}
```

注册一次即可 (`register_workspace`), 之后所有 upload 都带这个 id.

## Session 存储格式

**决定**: 原样保存, 不修改原文件. 包一层 envelope 作为第一行 JSON.

```
<filename>.jsonl.gz  — gzip 后的 JSONL
  第一行: envelope metadata
  第二行起: 原始 tool session 逐字复制
```

Envelope 结构:
```json
{
  "type": "envelope",
  "schema_version": 1,
  "workspace_id": "mymachine-claude-myproject",
  "tool": "claude-code",
  "tool_version": "2.1.87",
  "session_id": "0ec74ae5-5427-4e73-8722-d4ea6471eaf1",
  "source_path": "~/.claude/projects/my-project/session-id.jsonl",
  "source_size": 34567,
  "source_mtime": "2026-04-11T07:20:15Z",
  "uploaded_at": "2026-04-11T08:30:00Z",
  "sha256": "abcd1234..."
}
```

原文件**整体**作为 envelope 后的载荷, 逐字节保留. grep 时跳过第一行即可. 归档目录按**原始 mtime** 分到 `YYYY/MM/`, 避免同一个月跨月份.

**去重**: 服务端用 `sha256` 判断, 重复上传直接幂等 200 返回.

## 写权限边界

- 所有写操作必须带 `workspace_id`
- 服务端拒绝写到 `workspaces/<其他id>/` 下
- 路径沙箱继续生效 (`..` / 空字节 / 绝对路径拒绝)
- 读无边界 — 任何客户端可以读所有工作区 (这正是联邦的点)

## MCP 工具

### 读 (默认廉价, 先读摘要再下钻)

| 工具 | 作用 | 返回 |
|---|---|---|
| `get_overview` | 一次看全貌 | 全局 INDEX.md + workspaces.json + 每个工作区最近 mtime |
| `list_workspaces` | 列出所有工作区 | workspaces.json |
| `list_knowledge` | 列某工作区的 md | 文件名 + 首行标题 + mtime |
| `read_knowledge` | 读一个 md | 原文 |
| `search_knowledge` | grep md 内容 | file:line:snippet |
| `list_sessions` | 列 session 归档 | 按时间倒序, 带 envelope 摘要 |
| `read_session` | 读一个 session | gz 解压后 JSONL (可能很大) |
| `search_sessions` | grep session 内容 | file:line:snippet |

### 写 (分区, 每次必须带 workspace_id)

| 工具 | 作用 |
|---|---|
| `register_workspace` | 首次 / 更新 identity |
| `upload_knowledge` | 写 md 到 `workspaces/<id>/knowledge/` |
| `upload_session` | 写 session 到 `workspaces/<id>/sessions/YYYY/MM/` |
| `delete_knowledge` | 删 md (只能删自己的) |

注意 **没有 `delete_session`** — session 是审计记录, 只加不删, 要清理走服务器 cron.

## 客户端同步策略

### Claude CLI — SessionEnd hook
`~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionEnd": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "bash ~/.memory-sync/sync-claude.sh",
        "timeout": 30,
        "async": true
      }]
    }]
  }
}
```
脚本读 `$transcript_path` (从 stdin JSON), 压缩, upload.

### Codex CLI — 定时扫描
Codex 没 SessionEnd hook. 用 Windows Task Scheduler / cron 每 5 分钟扫描 `~/.codex/sessions/YYYY/MM/DD/` 找 mtime 大于上次上传时间的 rollout-*.jsonl 文件, 上传.

### 去重
客户端维护 `~/.memory-sync/uploaded.db` (SQLite 或 TSV: sha256, source_path, uploaded_at). 跳过已上传.

## 服务器资源

存储驻留层: 几十 KB 代码 + 几 MB Go 二进制 + 所有归档.
- text-only: 几百 GB 能用很多年
- 2vCPU / 898MB / SSD — 足够个人使用
- 瓶颈: 大量小文件 grep → 加 SQLite FTS5 索引 (phase 2, 不在本次)

## 本次实现范围

**Phase 1 (本次)**:
- 新目录结构 + seed INDEX.md/README.md
- 新 MCP 工具 (register/upload/list/read/search × knowledge/sessions)
- 写权限分区 + 沙箱
- 保留旧工具 (list_files 等) 作为 raw 操作

**Phase 2 (下次)**:
- SQLite FTS5 索引 (只索引 md, 不索引 session 内容)
- 客户端 Claude hook 脚本 + Codex 定时扫描
- install.sh 增加 workspace identity 提示
- 去重数据库

本次先把**服务端的联邦结构** + **一个可以跑的上传接口**做出来, 然后用手动 curl 验证.
