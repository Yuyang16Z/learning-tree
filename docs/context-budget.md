# Context budgets and source-grounded compaction

LearningTree manages the context sent with each answer request. Short eligible conversations stay complete. When history is too large, the app keeps recent complete turns and selects cited original sentences from older sources. It does not rewrite or delete the saved conversation.

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

## What the estimator counts

The estimator covers system instructions, message content, selected memory, quoted material, images, tool definitions and the tool calls/results accumulated in the current loop. It runs before answer-provider requests, including each continuation after a tool result.

- Text and serialized structured content use UTF-8 byte length as a conservative local proxy.
- Each image reserves 4,096 units plus overhead; it is not calculated from the provider's actual image processing rules.
- Messages, tool schemas and protocol metadata carry additional fixed or serialized overhead.

These are **not exact vendor token counts**. The app does not download a tokenizer, send counting requests or discover hidden provider overhead. Conservative estimates can compact earlier than necessary; fixed image estimates and provider-specific processing can still miss a limit. The allowance and headroom reduce oversized requests without guaranteeing provider acceptance or a precise total-token ceiling.

## How older context is compressed

The current question, its attached images, selected source passage and current learning note are mandatory. Eligible history is considered as complete question/answer groups, retaining the recent groups that fit. Older content becomes a source for deterministic, extractive compaction. Partial answers remain saved for inspection but are not promoted into these extracts; historical images not attached to the current request are explicitly marked as unseen.

The extractor selects complete original sentences using rule-based categories: corrections, applicable conditions, unresolved questions, learning goals, definitions, user understanding and historical explanations. Selection also considers overlap with the current question and the available budget. Each excerpt includes its role, source node, section, message ID when applicable, and character range.

For example, a retained line can identify a condition directly in the original answer:

```text
[适用条件；assistant；node=<id>, section=message, message=<id>, chars=<start>:<end>] …the original sentence…
```

The reference above is illustrative, not a reference to a shipped conversation. Category labels do not certify correctness. User notes and past model explanations remain unverified source material.

This is **extractive compaction, not an LLM-written summary**. It adds no model dependency, model download or dedicated summarization call. It preserves the wording of selected sentences, but it cannot preserve every relevant detail: sentences may be omitted, important context can lie in adjacent sentences, and a sentence too large for the extractor's candidate limit is skipped rather than cut through a qualification. The prompt marks the result as incomplete historical material.

The extraction cache is a disposable, process-local LRU with at most 64 entries. Keys include the source-content hash and extraction version. Editing a source changes the key, so its old extract is not reused for the edited content. Restarting clears the cache. It is not a new authoritative memory store and does not modify original messages.

Extracted learning memories also consume budget. They are selected or skipped as complete entries, including multiline conditions. The app avoids treating a memory as redundant merely because the same information appeared in older history that might subsequently be omitted.

## Read the original when needed

The built-in `read_learning_source` tool lets the answer model page through original material on the active learning path. It is enabled when the initial context has been compressed, or when the user selected tools whose later results could force further compaction. Short requests without selected tools keep the ordinary streaming path. Excerpt references guide rereading; the tool is read-only and is separate from MCP, filesystem access and vector retrieval.

At execution, the backend rechecks the current path and source record. A model-supplied node ID does not grant access to siblings, another tree or arbitrary files. Deleted or ineligible sources are rejected. A returned page is only part of the source; the model may need another page before reaching a conclusion.

Rereading does not restore every omitted detail automatically. It depends on the model requesting the relevant source. The read itself is local, while handling its result uses the normal agent/provider continuation and may add a normal inference round.

The chat runtime adjusts source-text page length and directory page size to the tool working space. A smaller window returns shorter pages with accurate continuation offsets. Original records remain unchanged; the reader does not pretend that a partial page is the complete source. Deep-thinking requests retain their existing Anthropic thinking configuration when compaction introduces the source tool.

## Tool results and inputs that cannot fit

Tool outputs can make a previously small request too large. The final fitting step first removes older history as complete protocol groups and explicitly notes the omission, reminding the model to reread eligible sources if needed. It preserves the current question and current tool-call IDs, arguments and required native blocks, including Anthropic signatures.

If tool-result bodies still exceed the budget, the request can retain their beginning and end with an explicit notice that the middle was omitted. That notice tells the model not to infer that missing content does not exist and not to repeat a side-effecting operation merely to obtain a fuller result. A shortened result is not a full execution log or proof that the entire output was checked.

If mandatory content such as the current question, quotation, images or tool schemas cannot fit, the app reports an actionable error. Shorten the input, reduce images or enabled tools, or correct the model's configured window when the provider actually supports more. The app does not silently cut mandatory input or change stored originals to force the request through.

See [local operation and backups](operations.md), [architecture](architecture.md) and [the memory-retrieval design](architecture.md#learning-memory-retrieval).

## 中文说明

在模型「高级设置」填写实际上下文窗口与回答预留，默认分别为 **32,768 / 4,096**。系统另外保留估算余量，逐轮检查系统提示、问题、引用、图片、历史和工具信息。UTF-8 字节数与固定图片开销只是本地估算，不是服务商的精确 token 计数，也不能保证所有请求必定被服务商接受。OpenAI 兼容接口的回答预留不等于输出上限；Anthropic 仍使用其最大输出参数。

短对话保持完整；长对话优先保留近期完整问答，较早内容按条件、纠正、问题、定义和用户理解等类别抽取完整原句，附上来源。这里没有调用另一个模型自由改写，也没有改动聊天原文；但抽取可能漏掉相关句子或相邻语境，不能宣称无损压缩。

首次上下文已压缩，或用户选中了可能继续产生较长结果的工具时，会启用 `read_learning_source`，供 Agent 分页回读当前学习路径原文，执行时重新核对来源范围。短且未选工具的问答保留普通流式路径。回读不会把兄弟分支、其他知识树或任意文件开放给模型。后续再次省略历史会明确提示；超长工具结果会标记只保留首尾，不能为补读而重放有副作用的操作。若当前问题、引用、图片或工具定义本身已超预算，则提示调整输入或模型配置。
