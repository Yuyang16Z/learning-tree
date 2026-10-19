# Context budgets and source-grounded compaction

LearningTree manages the context sent with each answer request. Short eligible conversations stay complete. When history exceeds the available input allowance, the app keeps recent complete turns and combines cached model summaries with cited original sentences from older sources. It does not rewrite or delete the saved conversation.

This is separate from learning-memory retrieval. Memory retrieval selects extracted `Memory` records; context compaction selects material from original messages, source passages and learning notes on the current path. Both ultimately share the same request budget.

## Configure the selected model

Open **Settings → Models & API keys**, add or edit a model, and expand **Advanced**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Context window | 32,768 | The capacity used for local request budgeting. Set it to the actual window supported by your provider and model. **Detect** asks the provider (Anthropic model information, or the `context_length`/`max_model_len`-style extras of an OpenAI-compatible `/models` list), falls back to an approximate built-in catalog for well-known model families (where a vendor caps input below the advertised total, such as gpt-5's 272,000 of 400,000, the catalog suggests the input cap, because this window also holds the answer reserve), fills the field and tells you which source answered; it never runs automatically, and the value is only stored when you save the form. When you edit a saved model without typing its key, Detect uses the stored key only if the protocol and address are still the saved ones; after changing either it sends nothing and answers from the catalog, asking for the new endpoint's key (saving with the key field still blank keeps the stored key for the new address).  A reported window outside the accepted 8,192–2,097,152 range is named in the note but not filled in. While detection runs, the field and **Save** wait; a result that arrives after you changed the API format, URL, model ID or key is discarded. |
| Answer reserve / Max output tokens | 4,096 | Space withheld from the local input budget. For native Anthropic it is also the requested output limit; OpenAI-compatible requests keep provider-controlled output length. |

The local input allowance is:

```text
input budget = max(0, context window − answer reserve − headroom)
headroom     = max(512, floor(context window / 20))

Default example:
32,768 − 4,096 − 1,638 = 27,034 estimated input units

compaction budget = floor(input budget × CONTEXT_COMPACTION_RATIO)   (default 0.6 → 16,220)
```

History is compacted once its estimate exceeds the compaction budget, so follow-ups and tool results keep room below the hard input budget, which every request is still fitted against. When tools reserve working space or schema allowance, the ratio applies to the room left after those reserves. `CONTEXT_COMPACTION_RATIO` (0.1–1.0, `.env`) moves the threshold; 1.0 compacts only at the hard limit. Mandatory input (the question, quote, images and current note) is judged against the hard budget only.

The settings are stored per model in SQLite. They are not `.env` options. An additive schema update initializes existing configurations to 32,768, raised when needed to accommodate a previously configured large output limit. This is a local starting value, not provider discovery. Saved chats and model credentials are preserved. Configuration validation requires at least 1,024 input units after both the answer reserve and proportional headroom.

When tools are available, initial context assembly also leaves `min(4096, input_budget // 3)` units free for a tool call and its result. This working space prevents a packed system summary from blocking the source reread it requests. Each subsequent request still undergoes a full budget check.

An OpenAI-compatible model's **Answer reserve** is only a local planning allowance. LearningTree does not add a new output-cap parameter to those requests, so the provider may generate more than that reserve. Native Anthropic requests continue to send `max_tokens`. In either case, use the provider's real limits rather than increasing the configured window merely to dismiss an error.

## Many selected tools

One checked MCP server can expose many functions. When their combined definitions exceed the schema allowance (normally one third of the input budget, adjusted for mandatory input and internal readers), LearningTree switches to a request-local tool catalog. It initially sends discovery, source readers, the answer's `read_tool_result` reader and a small relevant set: the built-in web search/fetch tools and the managed web-research MCP's `web_search`/`read_webpage` (under their `mcp_<id>_` names), plus up to three definitions matching the question. The model uses `search_available_tools` to find selected functions by name or description, then receives their complete native schemas on its next request. Searching does not execute the functions or enable unselected servers.

Read-only readers (original sources, attached documents, the history search and stored tool results) make a request tool-capable even when no tool is selected, for example when a memory scope switch offers the history search; such a request reserves the same tool room, so history compacts earlier (15–20% earlier at 32,768 tokens: the reserve itself plus the readers' definitions). If an endpoint rejects a request because it carries tools, an answer that carried only these readers is sent again without them, and that endpoint and model get no readers for the rest of the process; selected tools are never dropped, and their rejection is reported with a fixed message. The initial context reserves room for later schema loading as well as tool results. Newly requested definitions can replace older ones within that allowance. An individual definition too large to fit is reported as unavailable through discovery; its required arguments are never silently removed. Small tool sets keep the direct path. Catalog mode allows six tool rounds, followed when necessary by one answer-only request to synthesize the collected evidence.

## Agent budgets

Besides the round cap, two budgets end an answer's tool rounds early: `AGENT_TIME_LIMIT_SECONDS` (default 180) measured from the first tool-capable request, and `AGENT_TOOL_TOKEN_BUDGET` (default 60,000) counted with the same request estimator over every tool-result message the answer has gathered. Both are read from the environment or `.env`. They are checked between rounds: a round that has started runs all of its calls, and a call in progress is never interrupted, so a slow tool can overrun the time limit by its own duration. Once either budget is exceeded, the next request is the existing final round without tools, with the same closing instruction to answer from the collected evidence and mark what was not verified. The answer's diagnostics record `tool_budget_stop` (`time`, `tokens`, or empty when only the round cap or the model ended the rounds), shown in the collapsed block under the answer. Token counts are local estimates, not provider counts.

## What the estimator counts

The estimator covers system instructions, message content, selected memory, quoted material, images, tool definitions and the tool calls/results accumulated in the current loop, including the reasoning that a thinking endpoint such as DeepSeek gets back on its tool-call turns. It runs before answer-provider requests, including each continuation after a tool result.

- Text and serialized structured content are estimated from character classes: CJK ideographs, Kana and Hangul count about 0.7 unit per character, visible ASCII (letters, digits, punctuation) about 0.33, whitespace about 0.1 and other non-ASCII characters one unit each, multiplied by a 1.15 safety factor. Summary, extraction and document producers still budget UTF-8 bytes; the context builder converts its remaining room explicitly, at the byte density of the material the producer draws on (between 1.7 and 2.5 bytes per unit, so a history or document in Cyrillic, Greek, Arabic or Hebrew, about 1.8, is not handed more text than the room holds), and re-checks the result with the estimator.
- After each real (non-demo) answer, the app records the ratio of reported to estimated input tokens per model configuration in the rebuildable `ModelCalibration` table (an exponential moving average clamped to 0.5–3.0). The ratio is removed with the model and reset when an edit switches the configuration to another protocol, address or model name; editing the label, key, window or answer reserve keeps it. An answer that finishes after its model was deleted or switched writes nothing, so a model added later under the same id starts uncalibrated, and concurrent answers each count. The sample pairs each provider request's estimate with that request's own reported input, and counts only requests (tool rounds and the final answer, each on its own) whose estimate is at least 2,000 units, whose provider reported input tokens and that carry no image: fixed per-request overhead dominates smaller requests, and an image's fixed reservation would measure the provider's image pricing rather than the estimator. A ratio above 1 shrinks that model's local input budget proportionally; ratios below 1 never enlarge it.
- Each answer also keeps its estimate, reported usage, cached tokens, tool rounds, the number of memory records it was given and timings as numbers only. An answer with tool rounds makes several provider requests (each round and the final answer): the totals add them up, and a reported total stays unknown when any request did not report that count, while the request count and the largest single request are kept as well. The collapsed block under the answer shows that largest request as **Context**, since one request is what must fit the window, and the summed input inside as **Input across N requests**. A stopped answer gets its numbers once its provider request returns, also when a retry has already started, and open pages reload them. These diagnostics are excluded from tree JSON exports.
- Each image reserves 4,096 units plus overhead; it is not calculated from the provider's actual image processing rules.
- Messages, tool schemas and protocol metadata carry additional fixed or serialized overhead.

These are **not exact vendor token counts**. The app does not download a tokenizer, send counting requests or discover hidden provider overhead. Conservative estimates can compact earlier than necessary; fixed image estimates and provider-specific processing can still miss a limit. The allowance and headroom reduce oversized requests without guaranteeing provider acceptance or a precise total-token ceiling.

## How older context is compressed

The current question, its attached images, selected source passage and current learning note are mandatory. Eligible history is considered as complete question/answer groups, retaining the recent groups that fit. Only older material on the active path is eligible for summaries. Partial answers remain saved for inspection but are not promoted into summaries or extracts; historical images not attached to the current request are explicitly marked as unseen.

For ordinary chat, once preflight, tool discovery and source-reader budgeting are complete, the app may request one new summary from the **currently selected answer model**. This is an additional, potentially billable model request and may delay the start of the answer. The existing response label temporarily shows “Organizing context…”; no separate model setup is needed. Short chats and offline demo mode never request a summary. Inline explanations retain local extractive compaction without an extra summary call.

Summaries organize learning goals, covered concepts, unresolved questions, corrections and conditions, with explicit references to supplied sources. The prompt distinguishes “explained” from “understood” and treats user statements and earlier model answers as unverified material. Summary requests have no tools, no automatic retries and a short timeout. Invalid, truncated, over-budget, or incorrectly sourced output is discarded; local extraction remains available when generation fails.

For recognized V4/Flash models on the official DeepSeek endpoint, summary requests disable default thinking using the [documented parameter](https://api-docs.deepseek.com/guides/thinking_mode/) to avoid spending the preparation timeout on reasoning. Unknown models and compatible gateways receive no provider-specific options. This does not change the normal answer's thinking settings.

Stable older blocks are cached separately in SQLite's `ContextSummary` table. Keys include source content, tree identity, model identity and summary format version. Later questions can reuse unchanged blocks; every question generates at most one new block, while other material uses cached summaries or local excerpts. The app does not repeatedly summarize a summary as though it were original evidence. Block boundaries and the size limit for a summarized block follow the model's configured window, not its learned calibration ratio, so a changing ratio never invalidates cached blocks. Oversized source blocks can remain extractive when a complete summary request would not fit; a block whose request the calibrated budget refuses is passed over for one that fits, without a failed attempt.

Under a limited context budget, the app ranks source blocks by lexical overlap with the current question and selected passage. It spends the one optional summary call on the most relevant eligible uncached block; when cached summaries cannot all fit, it includes the more relevant ones first (recent blocks break ties), then presents the selected summaries in path order. If uncached blocks remain, it reserves part of the allowance for cited original-sentence excerpts. This ranking is a deterministic relevance heuristic, not semantic retrieval or a guarantee that every needed detail is present. The original path remains available through the source reader.

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

## Prompt order and provider caching

The system prompt is assembled stable-first: the fixed instructions, then the learning-path lines for the ancestors (with their notes or imported summaries), then the current node, the current learning note, the quoted passage, document excerpts and finally the retrieved memories. Turns under the same path therefore share a long identical head. For native Anthropic models every answer request sets the top-level cache marker, so the request up to the growing conversation tail is cached whatever the length of that head; the head is also sent as its own text block with an `ephemeral` cache marker when it is at least 1,024 characters and still a prefix of the final fitted prompt (fitting can replace optional context). A request below the model's minimum cacheable length is processed without caching and costs nothing extra. A provider that rejects the marker with HTTP 400 gets one plain retry and the markers are then disabled for that endpoint for the rest of the process; summaries and titles are sent unmarked. OpenAI-compatible providers receive unchanged requests, since their prefix caching needs no parameters. Cache reads and writes are visible in the answer diagnostics as cached tokens.

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

在模型「高级设置」填写实际上下文窗口与回答预留，默认分别为 **32,768 / 4,096**；点「检测」可向服务商查询该模型的上下文窗口（查不到时使用内置的近似目录并明确标注；服务商对输入另有上限时，如 gpt-5 总窗口 400,000 中输入最多 272,000，目录给出输入上限，因为这里的窗口还包含回答预留），填入表单后仍由你核对保存，不会自动运行；超出 8,192–2,097,152 可填写范围的检测值只在说明中列出、不会填入，检测期间窗口字段和「保存」暂不可用，期间改动 API 格式、地址、模型 ID 或密钥后返回的结果会被丢弃；编辑已保存的模型且未重新填写密钥时，检测只在协议和地址都未改动时使用已保存的密钥，改动后不发送任何请求，只给出目录值并提示填写新服务端的密钥（此时若密钥留空直接保存，已保存的密钥会沿用到新地址）。系统另外保留估算余量，逐轮检查系统提示、问题、引用、图片、历史和工具信息（包括 DeepSeek 等思考型接口在工具调用轮次需要回传的推理内容）。历史估算超过输入预算的 `CONTEXT_COMPACTION_RATIO`（默认 0.6）时就开始压缩，给追问和工具结果留出空间；每次请求仍按硬预算做最终裁剪，必要输入只受硬预算限制。系统提示按「稳定优先」排列：固定规则、祖先学习路径（含笔记与导入摘要）、当前节点、当前笔记、引用、文档摘录，记忆放在最后。Anthropic 原生模型的每次回答请求都设置顶层缓存标记，无论稳定开头多长都缓存到不断增长的对话末尾；该稳定开头不少于 1,024 字符且仍是最终提示的前缀时，还会作为带 `ephemeral` 缓存标记的独立文本块发送。低于模型最小可缓存长度的请求不缓存，也不额外计费。服务商以 HTTP 400 拒绝该标记时自动重试一次并在本进程内停用；摘要和标题请求不带标记。OpenAI 兼容接口的请求不变。按字符类别估算的 token 数（中日韩文字约 0.7、可见 ASCII 约 0.33、空白约 0.1、其他非 ASCII 字符 1，再乘 1.15 安全系数）与固定图片开销只是本地估算，不是服务商的精确 token 计数，也不能保证所有请求必定被服务商接受。摘要与文档摘录按 UTF-8 字节计算预算，换算比例取自所用材料本身（每单位 1.7–2.5 字节），俄文、希腊文、阿拉伯文、希伯来文等双字节文字的历史或文档不会超出预留空间。真实（非演示）回答结束后，系统按模型记录「实际 ÷ 估算」比例（指数平均，限制在 0.5–3.0，存于可重建的 `ModelCalibration` 表；删除模型时一并删除，模型改为其他协议、地址或模型名时重置，只改名称、密钥、窗口或回答预留时保留；模型被删除或改换后才结束的回答不再写入，之后复用同一编号的新模型从未校准开始，同时结束的多个回答各自计入）：每次请求（各工具轮与最终回答分别计算）的估算只与该请求自己的实际用量配对，且只计入估算不少于 2,000 单位、服务商返回了输入用量、不带图片的请求，因为小请求以固定开销为主，图片的固定预留衡量的是服务商的图片计价而非估算器；比例大于 1 时按比例缩小该模型的本地输入预算，小于 1 不会放大。每条回答另外保存估算、实际用量、缓存命中、工具轮数、所用记忆条数与耗时等数字诊断（各次请求的合计；回答中任一请求未返回某项用量时，该项合计记为未知；另记请求次数与最大的一次请求），显示在回答下方：「上下文」是最大的一次请求，因为单次请求才需要放进上下文窗口；有工具轮时，各次请求的输入合计在展开后显示为「输入合计（N 次请求）」。已停止的回答在其请求返回后补存诊断，即使已经开始重试也不会丢失，打开的页面会随之刷新。诊断不随学习记录导出。OpenAI 兼容接口的回答预留不等于输出上限；Anthropic 仍使用其最大输出参数。

短对话保持完整；长对话优先保留当前问题、引用、笔记和能容纳的近期完整问答，较早内容由当前选择的模型按需生成摘要，每次提问最多新增一次摘要调用，可能增加费用和短暂等待。摘要记录学习目标、已讲内容、未解决疑问、纠正与条件，不能把「已讲过」当成「已掌握」。结果按来源分块缓存，后续复用；分块边界与可摘要块的大小上限只取决于模型配置的窗口，不受校准比例影响，因此比例变化不会让已缓存的摘要失效，校准后的预算只决定某块的摘要请求能否发送，放不下的块会让位给放得下的块；空间不足时按本轮问题的词项相关性选择待生成和已缓存的来源块，同分优先较近的来源，并为未摘要来源预留原句摘录空间。其他旧内容保留本地原句摘录。摘要失败、超时、引用无效或过长时，继续用本地摘录，不阻断正常回答。这是本地相关性启发式，不能保证找全细节；演示模式和短对话不调用摘要，划词解释仍使用本地摘录。

摘要只用于当前学习路径，不混入兄弟分支，不写入长期记忆，也不改写聊天原文。编辑笔记后相关摘要失效；修改问题生成的新版本独立处理；删除节点或整棵树会同步删除依赖它们的摘要，尚未返回的摘要不能把已删除内容写回来。摘要可能遗漏或误解细节，不能宣称无损压缩；需要精确信息时仍应回读原文。

勾选一组 MCP 可能展开成很多函数。工具定义较多时改为按需发现与加载，选中的工具仍可检索，完整参数定义在下一轮发给模型；检索本身不会执行工具。系统同时预留后续加载和结果所需空间。单个定义仍过大时会明确告知，不会擅自删减参数。

除轮数上限外，还有两项 Agent 预算：`AGENT_TIME_LIMIT_SECONDS`（默认 180 秒，从第一次可调用工具的请求开始计时）和 `AGENT_TOOL_TOKEN_BUDGET`（默认 60,000，用同一请求估算器累计本次回答收到的全部工具结果）。两者在轮与轮之间检查：已开始的一轮会执行完它的全部调用，正在执行的调用不会被中断，所以慢工具可能让实际耗时略超上限。任一预算用完后，下一次请求就是原有的最后一轮无工具请求，沿用相同的收尾指令，根据已取得的资料作答。回答诊断中的 `tool_budget_stop` 记录是哪项预算提前结束了工具轮（`time`、`tokens`，或在仅由轮数上限或模型自行结束时为空），显示在回答下方的折叠块中。token 数是本地估算，不是服务商的计数。

首次上下文已压缩，或用户选中了可能继续产生较长结果的工具时，会启用 `read_learning_source`，供 Agent 分页回读当前学习路径原文，执行时重新核对来源范围。短且未选工具的问答保留普通流式路径。回读不会把兄弟分支、其他知识树或任意文件开放给模型。后续工具调用增长时先省略较早问答，再回收可选历史、记忆和文档摘录；仍不够时，超长工具结果标记只保留首尾，不能为补读而重放有副作用的操作。当前问题、引用、图片、学习笔记及原始存档会保留；必要输入仍超预算时提示调整输入或模型配置。
