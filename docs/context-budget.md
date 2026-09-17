# Context budgets and source-grounded compaction

LearningTree manages the context sent with each answer request. Short eligible conversations stay complete. When history exceeds the available input allowance, the app keeps recent complete turns and combines cached model summaries with cited original sentences from older sources. It does not rewrite or delete the saved conversation.

This is separate from learning-memory retrieval. Memory retrieval selects extracted `Memory` records; context compaction selects material from original messages, source passages and learning notes on the current path. Both ultimately share the same request budget.

## Configure the selected model

Open **Settings → Models & API keys**, add or edit a model, and expand **Advanced**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Context window | 32,768 | The capacity used for local request budgeting. Set it to the actual window supported by your provider and model. It is not automatically discovered from the model name. |
| Answer reserve / Max output tokens | 4,096 | Space withheld from the local input budget. For native Anthropic it is also the requested output limit; OpenAI-compatible requests keep provider-controlled output length. |

The local input allowance is:

```text
input budget = max(0, context window − answer reserve − headroom)
headroom     = max(512, floor(context window / 20))

Default example:
32,768 − 4,096 − 1,638 = 27,034 estimated input units
```

The settings are stored per model in SQLite. They are not `.env` options. An additive schema update initializes existing configurations to 32,768, raised when needed to accommodate a previously configured large output limit. This is a local starting value, not provider discovery. Saved chats and model credentials are preserved. Configuration validation requires at least 1,024 input units after both the answer reserve and proportional headroom.

When tools are available, initial context assembly also leaves `min(4096, input_budget // 3)` units free for a tool call and its result. This working space prevents a packed system summary from blocking the source reread it requests. Each subsequent request still undergoes a full budget check.

An OpenAI-compatible model's **Answer reserve** is only a local planning allowance. LearningTree does not add a new output-cap parameter to those requests, so the provider may generate more than that reserve. Native Anthropic requests continue to send `max_tokens`. In either case, use the provider's real limits rather than increasing the configured window merely to dismiss an error.

## Many selected tools

One checked MCP server can expose many functions. When their combined definitions exceed the schema allowance (normally one third of the input budget, adjusted for mandatory input and internal readers), LearningTree switches to a request-local tool catalog. It initially sends discovery, source readers and a small relevant set. The model uses `search_available_tools` to find selected functions by name or description, then receives their complete native schemas on its next request. Searching does not execute the functions or enable unselected servers.

The initial context reserves room for later schema loading as well as tool results. Newly requested definitions can replace older ones within that allowance. An individual definition too large to fit is reported as unavailable through discovery; its required arguments are never silently removed. Small tool sets keep the direct path. Catalog mode allows six tool rounds, followed when necessary by one answer-only request to synthesize the collected evidence.

## What the estimator counts

The estimator covers system instructions, message content, selected memory, quoted material, images, tool definitions and the tool calls/results accumulated in the current loop. It runs before answer-provider requests, including each continuation after a tool result.

- Text and serialized structured content use UTF-8 byte length as a conservative local proxy.
- Each image reserves 4,096 units plus overhead; it is not calculated from the provider's actual image processing rules.
- Messages, tool schemas and protocol metadata carry additional fixed or serialized overhead.

These are **not exact vendor token counts**. The app does not download a tokenizer, send counting requests or discover hidden provider overhead. Conservative estimates can compact earlier than necessary; fixed image estimates and provider-specific processing can still miss a limit. The allowance and headroom reduce oversized requests without guaranteeing provider acceptance or a precise total-token ceiling.

## How older context is compressed

The current question, its attached images, selected source passage and current learning note are mandatory. Eligible history is considered as complete question/answer groups, retaining the recent groups that fit. Only older material on the active path is eligible for summaries. Partial answers remain saved for inspection but are not promoted into summaries or extracts; historical images not attached to the current request are explicitly marked as unseen.

For ordinary chat, once preflight, tool discovery and source-reader budgeting are complete, the app may request one new summary from the **currently selected answer model**. This is an additional, potentially billable model request and may delay the start of the answer. The existing response label temporarily shows “Organizing context…”; no separate model setup is needed. Short chats and offline demo mode never request a summary. Inline explanations retain local extractive compaction without an extra summary call.

Summaries organize learning goals, covered concepts, unresolved questions, corrections and conditions, with explicit references to supplied sources. The prompt distinguishes “explained” from “understood” and treats user statements and earlier model answers as unverified material. Summary requests have no tools, no automatic retries and a short timeout. Invalid, truncated, over-budget, or incorrectly sourced output is discarded; local extraction remains available when generation fails.

For recognized V4/Flash models on the official DeepSeek endpoint, summary requests disable default thinking using the [documented parameter](https://api-docs.deepseek.com/guides/thinking_mode/) to avoid spending the preparation timeout on reasoning. Unknown models and compatible gateways receive no provider-specific options. This does not change the normal answer's thinking settings.

Stable older blocks are cached separately in SQLite's `ContextSummary` table. Keys include source content, tree identity, model identity and summary format version. Later questions can reuse unchanged blocks; every question generates at most one new block, while other material uses cached summaries or local excerpts. The app does not repeatedly summarize a summary as though it were original evidence. Oversized source blocks can remain extractive when a complete summary request would not fit.

Cache reuse and writes revalidate original sources. Editing a learning note invalidates dependent summaries; revising a message creates a separate version whose changed sources have different keys. Deleting a subtree or tree removes its dependent summaries, and a late provider response cannot recreate them. These derived summaries are **not** long-term learning memories or evidence of mastery. They are included in private database backups, excluded from tree JSON exports, and can be rebuilt from originals.

The extractor selects complete original sentences using rule-based categories: corrections, applicable conditions, unresolved questions, learning goals, definitions, user understanding and historical explanations. Selection also considers overlap with the current question and the available budget. Each excerpt includes its role, source node, section, message ID when applicable, and character range.

For example, a retained line can identify a condition directly in the original answer:

```text
[适用条件；assistant；node=<id>, section=message, message=<id>, chars=<start>:<end>] …the original sentence…
```

The reference above is illustrative, not a reference to a shipped conversation. Category labels do not certify correctness. User notes and past model explanations remain unverified source material.

This extractive fallback adds no dedicated model call. It preserves the wording of selected sentences, but it cannot preserve every relevant detail: sentences may be omitted, important context can lie in adjacent sentences, and a sentence too large for the extractor's candidate limit is skipped rather than cut through a qualification. Model summaries can also omit or misinterpret details even when their structure and source IDs validate. Neither approach is lossless; the prompt marks the result as incomplete historical material, and exact details should be reread from originals.

The extraction cache is a disposable, process-local LRU with at most 64 entries. Keys include the source-content hash and extraction version. Editing a source changes the key, so its old extract is not reused for the edited content. Restarting clears the cache. It is not a new authoritative memory store and does not modify original messages.

Extracted learning memories also consume budget. They are selected or skipped as complete entries, including multiline conditions. The app avoids treating a memory as redundant merely because the same information appeared in older history that might subsequently be omitted.

## Read the original when needed

The built-in `read_learning_source` tool lets the answer model page through original material on the active learning path. It is enabled when the initial context has been compressed, or when the user selected tools whose later results could force further compaction. Short requests without selected tools keep the ordinary streaming path. Excerpt references guide rereading; the tool is read-only and is separate from MCP, filesystem access and vector retrieval.

At execution, the backend rechecks the current path and source record. A model-supplied node ID does not grant access to siblings, another tree or arbitrary files. Deleted or ineligible sources are rejected. A returned page is only part of the source; the model may need another page before reaching a conclusion.

Rereading does not restore every omitted detail automatically. It depends on the model requesting the relevant source. The read itself is local, while handling its result uses the normal agent/provider continuation and may add a normal inference round.

The chat runtime adjusts source-text page length and directory page size to the tool working space. A smaller window returns shorter pages with accurate continuation offsets. Original records remain unchanged; the reader does not pretend that a partial page is the complete source. Deep-thinking requests retain their existing Anthropic thinking configuration when compaction introduces the source tool.

## Tool results and inputs that cannot fit

Tool outputs can make a previously small request too large. The final fitting step first removes older history as complete protocol groups and explicitly notes the omission, reminding the model to reread eligible sources if needed. It preserves the current question and current tool-call IDs, arguments and required native blocks, including Anthropic signatures.

If more room is needed, the request drops optional historical excerpts, retrieved memories and document snippets from the system context, with an omission notice. The current quotation and learning note remain intact. Source and document readers stay available to retrieve eligible originals within the remaining tool rounds.

If tool-result bodies still exceed the budget, the request can retain their beginning and end with an explicit notice that the middle was omitted. That notice tells the model not to infer that missing content does not exist and not to repeat a side-effecting operation merely to obtain a fuller result. A shortened result is not a full execution log or proof that the entire output was checked.

If mandatory content such as the current question, quotation, images, required readers or accumulated signed tool-call metadata cannot fit, the app reports an actionable error. Shorten the input, reduce images, or correct the model's configured window when the provider actually supports more. The app does not silently cut mandatory input or change stored originals to force the request through.

See [local operation and backups](operations.md), [architecture](architecture.md) and [the memory-retrieval design](architecture.md#learning-memory-retrieval).

## 中文说明

在模型「高级设置」填写实际上下文窗口与回答预留，默认分别为 **32,768 / 4,096**。系统另外保留估算余量，逐轮检查系统提示、问题、引用、图片、历史和工具信息。UTF-8 字节数与固定图片开销只是本地估算，不是服务商的精确 token 计数，也不能保证所有请求必定被服务商接受。OpenAI 兼容接口的回答预留不等于输出上限；Anthropic 仍使用其最大输出参数。

短对话保持完整；长对话优先保留当前问题、引用、笔记和能容纳的近期完整问答，较早内容由当前选择的模型按需生成摘要，每次提问最多新增一次摘要调用，可能增加费用和短暂等待。摘要记录学习目标、已讲内容、未解决疑问、纠正与条件，不能把「已讲过」当成「已掌握」。结果按来源分块缓存，后续复用；其他旧内容保留本地原句摘录。摘要失败、超时、引用无效或过长时，继续用本地摘录，不阻断正常回答。演示模式和短对话不调用摘要；划词解释仍使用本地摘录。

摘要只用于当前学习路径，不混入兄弟分支，不写入长期记忆，也不改写聊天原文。编辑笔记后相关摘要失效；修改问题生成的新版本独立处理；删除节点或整棵树会同步删除依赖它们的摘要，尚未返回的摘要不能把已删除内容写回来。摘要可能遗漏或误解细节，不能宣称无损压缩；需要精确信息时仍应回读原文。

勾选一组 MCP 可能展开成很多函数。工具定义较多时改为按需发现与加载，选中的工具仍可检索，完整参数定义在下一轮发给模型；检索本身不会执行工具。系统同时预留后续加载和结果所需空间。单个定义仍过大时会明确告知，不会擅自删减参数。

首次上下文已压缩，或用户选中了可能继续产生较长结果的工具时，会启用 `read_learning_source`，供 Agent 分页回读当前学习路径原文，执行时重新核对来源范围。短且未选工具的问答保留普通流式路径。回读不会把兄弟分支、其他知识树或任意文件开放给模型。后续工具调用增长时先省略较早问答，再回收可选历史、记忆和文档摘录；仍不够时，超长工具结果标记只保留首尾，不能为补读而重放有副作用的操作。当前问题、引用、图片、学习笔记及原始存档会保留；必要输入仍超预算时提示调整输入或模型配置。
