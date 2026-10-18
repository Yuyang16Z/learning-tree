# Memory management

Open **Settings → Memory** to manage what LearningTree recalls. The interface is
available in English and Chinese.

## Preferences and habits

Existing automatic preferences first appear together in one editable text area.
Reading this view does not rewrite or delete the original records. Duplicate
text is combined for display.

The editor stays at a fixed height and scrolls internally. All Settings tabs use
the same responsive outer frame; longer pages scroll inside that frame.

Save explicitly to make the profile user-managed. Automatic extraction never
appends to or overwrites that text, including an intentionally empty saved
profile. New preferences can still be learned in the separate **AI additions**
section below it. Existing historical classifications are preserved until you
edit them; the upgrade does not reclassify or replay old conversations.

The profile accepts up to 6,000 characters. Longer legacy content remains visible
without truncation and must be shortened before saving. Switching settings tabs
keeps the draft; closing settings with unsaved changes asks before discarding it.
Simultaneous edits from another window cause a conflict instead of silently
overwriting either version. Copy your local draft before loading a newer version
if you want to combine the two.

The profile is included as user-provided learning context, with the current
request taking precedence. It remains subject to the configured model's context
budget; long profiles can be omitted as a whole when they do not fit.

### AI additions

The additions section starts collapsed and shows ten records per page. Each
record has its applicable scope (global or its learning topic), the conversation
source and the user's original supporting words. Edit or delete individual
records without modifying the main profile. An edited addition is protected
from subsequent automatic updates. A pending suggestion does not affect answers;
resolve a conflict in the main profile and remove the suggestion when finished.

**Automatically remember preferences** controls future preference extraction.
Turning it off leaves existing additions in use and does not stop topic-fact
extraction. Delete unwanted additions to stop recalling them.

After a completed non-demo answer, the existing background memory call can
extract both facts and explicit durable preferences. Preference evidence must
come from the user's own words, not from the assistant's answer, quoted examples
or tool/document text. One-off requests and inferred habits are not promoted to
durable preferences. Extraction considers bounded existing additions and the
read-only main profile; it returns small structured changes instead of rewriting
the entire profile. Interpretation still depends on the configured model, so
source links and manual corrections remain important.

Exact duplicates are skipped. A clear correction may replace an automatic
addition only within the same scope. Conflicts with the main profile or a
user-edited addition remain pending. Stale background results are discarded if
the profile, settings, source or candidate records changed during extraction.

An input ledger prevents the same completed question from being processed again,
including when an answer is regenerated or a derived addition has been deleted.
The ledger contains IDs and hashes, not preference text. Extraction is best-effort:
a failed provider call does not retry that input automatically. A genuinely new
question can establish a new preference, including one previously deleted.

At answer time, only active additions with valid completed source nodes apply.
Global additions can cross trees; topic additions stay within their tree. The
priority is the current request, then the main user profile, then applicable
additions (user-edited before automatic). At most eight additions within an
800-character rendered budget are recalled; each statement stays complete.
Deleting a source node or tree also deletes its additions and extraction ledger.

## Topic facts

- Search by text across stored facts, then filter by learning topic.
- Browse ten records per page instead of an unbounded list.
- Expand a record to inspect it, edit an incorrect statement, or return to its
  source conversation.
- Select records on the current page and delete only that selection. Changing
  the search, topic or page clears the selection.

Editing a fact preserves its topic and source node, invalidates its cached
embedding, and makes the next eligible retrieval use the revised content.
Deleting a fact also removes its vector cache. Neither action rewrites or deletes
the original conversation. Relevance retrieval still uses valid sources on the
current learning path unless you widen it below: Settings search itself does not
make sibling branches eligible.

### Structured facts

Each extracted fact carries a type shown as a small tag: definition, condition,
correction (a misconception the answer corrected), open question, learner state
(what you are working on or asked for this time) or conclusion. The extractor
also receives up to twenty recent facts from the current learning path so it can
mark a new fact as correcting an earlier one instead of restating it. The earlier
fact is then tagged **Superseded**: it stays stored and listed until you delete
it, but it no longer reaches answers, the search index or the model's history
search. Deleting the newer fact, or the branch or topic it came from, makes the
earlier fact current again. Only facts that extraction wrote and that were never
edited by hand can be superseded, and only by a fact from the same learning path.
Facts stored before this version are never superseded: earlier versions did not
record hand edits, so any of them may hold your own correction. Facts extracted by older versions read as
conclusions. Interpretation of types and corrections depends on the configured
model; the tags are hints, not verified classifications.

## Retrieval scope

Below the retrieval status, **Settings → Memory** offers two switches:

- **Other branches of this topic** adds facts from sibling branches of the
  current learning tree.
- **Other learning topics** adds facts from other, non-archived learning trees.

The current learning path is always searched and cannot be turned off. Widened
facts go through the same ranking steps as path facts, but as a separate pool:
the similarity gate, the candidate cut and the reranker cutoff of each pool are
computed within that pool. They are rendered after the path facts in their own
sections, each line labelled with the branch or topic it came from, and they
share a smaller budget (four entries, 1,200 characters) than the path (eight
entries, 2,400 characters). Turning a switch on therefore never removes a path
fact the answer would otherwise have received. To keep each question's cost
bounded however many facts other branches and topics hold, at most 48 widened
facts per question are embedded, scored and reranked: the best keyword matches
first, then the most recent. A widened fact that shares no words with the
question can therefore be missed once a scope holds more than 48 facts.
Each widened hit is rechecked against current records for its own scope before
it enters the prompt: a sibling whose answer failed, or a topic that was
archived meanwhile, drops out. Both switches are off by default; changing them
is saved with a revision token, and a conflicting save from another window
reloads the latest settings instead of overwriting them.

The same switches govern the answer model's read-only `search_learning_history`
tool. It searches completed answers, notes and current facts of the current path
by default, never questions (including the one being answered); the `tree` and
`all` scopes are refused unless the matching switch was on when the answer
started, and the answer's memory note uses that same reading of the switches.
The switches stay independent there too: `all` adds other topics to the current
path and includes this topic's other branches only when **Other branches of this
topic** is also on. The topic being answered in stays searchable under `all`
even when it is archived. The tool never writes, never embeds and is not offered
in demo mode.

Offering the tool turns an answer into a tool-capable request, which keeps room
for tool results: with a 32,768-token window, history is compacted 15–20%
earlier while a switch is on. A model endpoint that rejects requests carrying
tools (for example an Ollama model without a tool template, or vLLM started
without a tool parser) still answers: the first rejected answer is sent again
without the read-only readers, and that endpoint and model get none for the
rest of the session. Tools you select yourself are never dropped; when an
endpoint rejects them, the answer says so instead of asking you to retry.

### Referenced memories

Under each answer, a compact **Referenced** line lists the memories that were
actually part of the prompt, as memory ids with their source node, labelled when
they came from another branch or topic. Entries open the source node, switching
topic when needed. Records that compaction dropped from the prompt are not
listed. The list is stored with the answer and shown again later; it refers to
this database's ids, so learning-tree exports leave it out.

The stored list holds only record ids and their scope. Topic names are read when
the answer is shown, so a renamed topic shows its new name, and entries whose
fact, source node or topic no longer exists are left out. Deleting a topic, a
branch or facts also removes the entries that pointed at them from the answers
that kept them, so a deleted topic leaves neither its name nor its ids behind
in other topics. Text that an answer or its tool steps already quoted (for
example a history-search result) belongs to that answer and is not rewritten.

## Topic search

The learning map's search box filters titles and quoted passages instantly and,
after a short pause, also asks the server for full-text matches in your
questions, completed answers, learning notes and current facts of the open topic. Those appear as a
second group with a verbatim snippet; choosing an answer hit opens the node and
highlights the matching passage. Pending or interrupted answers and superseded
facts are never listed. Results are capped at thirty, and a search is dropped
when you switch topics before it returns.

Search uses a SQLite FTS5 trigram index (`search_index`) maintained by database
triggers, so it follows every edit, completion, supersession and deletion
immediately. Index rows are found by a rowid derived from their source record,
so deleting a large topic or saving a streaming answer does not slow down as the
index grows. Start-up creates the index when it is missing and rebuilds it when
its row count disagrees with the stored records or its triggers come from
another version; it is derived data that a restored backup rebuilds on first
start. Queries shorter than three characters use a case-insensitive LIKE scan
instead, and so does every search on a SQLite build without FTS5 or one that
cannot open the stored index (for example after moving the database to another
build). Start-up then removes the index triggers so that saving answers, facts
and notes keeps working, and the next start that can open the index rebuilds it.

The source conversation can still appear in ordinary chat context after its
derived fact is deleted. Later extraction from a new completed answer may produce
a similar fact. This is management of built-in memory, separate from any
configured MCP memory server.

## Evaluating retrieval on your data

To see how the retrieval scopes and topic search behave on your own records
without changing them, run
`uv run python scripts/evaluate_retrieval.py --database branch_learning.db`. It
works on a temporary copy, replays each completed question against the facts
that existed when it was asked, and writes counts, proxy recall scores and
timings to `artifacts/`; see [offline retrieval evaluation](retrieval-evaluation.md).
That run uses keyword retrieval, which applies no similarity or reranking
threshold. Only a run with `--hybrid` and prepared local models applies the
similarity gate and the reranker cutoffs, and only at their current defaults;
the script has no option to vary them. The proxy scores are structural
assumptions, not human relevance judgments.

## Storage and compatibility

The additive `PreferenceProfile` table stores the user-managed singleton and its
revision. `PreferenceSupplement` stores additions separately;
`PreferenceLearningState` stores the switch and revision; `PreferenceExtraction`
stores processed input identities. Existing `Memory` rows and IDs remain intact.
A saved profile supersedes historical preference rows; facts still use `Memory`.
Database backups include these tables. Individual learning-tree JSON exports
contain conversations and attachments, not preference profiles or additions.

Legacy `GET /memories` remains available. The new management endpoints are
`GET/PUT /memories/preferences`, `GET /memories/facts`, `PATCH /memories/{id}`,
and `POST /memories/delete` for selected fact IDs. Both direct and `/api` routes
are available. Profile writes use a revision token; fact edits compare the
expected original content before committing. Clear-all leaves an empty managed
profile so an older preference edit cannot silently restore cleared content.

Addition endpoints are `GET/PUT /memories/preferences/learning`,
`GET /memories/preferences/supplements` and
`PATCH/DELETE /memories/preferences/supplements/{id}`. Edits and deletes use
revision tokens. Clear-all removes additions and invalidates in-flight writes,
while retaining the content-free input ledger to prevent replay.

`GET/PUT /memories/retrieval/settings` reads and saves the two scope switches
(`include_siblings`, `include_other_trees`) with a revision token; the row is
only created on the first save and is kept when topics are deleted.
`GET /trees/{id}/search?q=&limit=` returns `{results: [{node_id, message_id,
kind, snippet, rank}]}` with `kind` one of `title`, `question` (a question you
asked; `message_id` is that question), `message` (an answer), `note` or `memory`. `Memory` rows gained `fact_type`, `terms`, `superseded_by`,
`updated_at` and `origin` (`legacy` for existing rows); `Message` rows gained
`sources` (ids and scope). All additions are additive and idempotent; database
backups include them, learning-tree exports exclude `sources`.

## 中文说明

- **检索范围**：「设置 → 记忆」新增两个开关：「同主题的其他分支」和「其他学习主题」。
  当前学习路径始终参与检索，不能关闭。扩大范围后的记忆与路径记忆经过同样的排序步骤，但作为
  独立的一组：相似度门槛、候选截断和重排截断都只在各自组内计算；它们单独分节渲染、
  每条标注来源分支或主题，并使用更小的预算（4 条、1,200 字符，路径为 8 条、2,400 字符），
  因此打开开关不会让原本能进入回答的路径记忆被挤掉；为了让每次提问的开销有上限，扩大范围的记忆
  每次最多 48 条进入向量计算和重排（先取关键词最匹配的，再取最新的），所以范围内超过 48 条时，
  与问题没有共同词语的记忆可能检索不到；进入提示前会按各自范围重新核对来源是否仍然有效。
  两个开关默认关闭，保存带版本号，别处同时修改会重新载入最新设置而不是覆盖。
- **结构化事实**：提炼出的事实带类型标签（定义、适用条件、纠正误解、待解决疑问、学习状态、
  结论）。新事实可以标记为修正某条当前路径上的旧事实，旧事实显示「已更新」并保留可删，
  但不再进入回答、搜索索引和模型的历史检索；删除新事实或它所在的分支、主题后，旧事实会恢复
  为当前事实。用户手动编辑过的事实，以及升级前保存的事实（旧版本不记录手动编辑，可能含有
  用户自己的修正）都不会被自动替代。
- **参考了哪些记忆**：每条回答下方列出实际进入提示的记忆编号及来源节点，来自其他分支或
  主题的会标注；点击可跳到来源节点。被压缩掉的记忆不会列出。该列表随回答保存，但不随
  学习树导出。保存的只有记录编号和范围，主题名称在显示时读取；记忆、来源节点或主题已删除
  的条目不再显示。删除主题、分支或记忆时，其他回答中指向它们的条目会一并移除，已删除主题
  的名称和编号不会留在其他主题里；回答正文或工具步骤中已经引用的文字属于该回答本身，不会被改写。
- **主题内全文搜索**：学习树搜索框除了即时匹配标题和引用，还会向服务端查询提问、已完成
  回答、学习笔记和当前事实中的原文片段，作为第二组结果展示；选择回答结果会跳转并高亮对应段落。
  索引由 SQLite FTS5（三元组分词）和触发器维护，触发器按由来源记录推算的 rowid 定位索引行，
  删除大主题或保存流式回答不会随索引增大而变慢；启动时索引缺失、计数不符或触发器来自其他
  版本会自动重建；短于 3 个字符的查询、不支持 FTS5 或无法打开已有索引（例如数据库换到另一个
  SQLite 版本上）时使用 LIKE 扫描，启动时还会移除索引触发器，保证回答、记忆和笔记照常保存，
  下次能打开索引的启动会重建它。
- 模型可用的只读工具 `search_learning_history` 受同一组开关约束：只返回回答、笔记和事实，
  不返回提问（包括正在回答的这个问题）；默认只查当前路径，`tree`/`all` 范围需在回答开始时
  已经启用；两个开关互相独立，`all` 在当前路径之外加入
  其他主题，只有「同主题的其他分支」也开启时才包含当前主题的其他分支；当前主题即使已归档，
  `all` 也能查到它的路径。它不写入数据库，演示模式不会提供。提供这个工具会让回答变成可调用
  工具的请求，需要为工具结果预留空间：上下文窗口为 32,768 时，开关打开后历史压缩会提前约
  15%–20%。不支持工具调用的模型接口（例如没有工具模板的 Ollama 模型、未启用工具解析的 vLLM）
  仍能回答：第一次被拒的回答会去掉只读工具重新发送，本次运行期间该接口和模型不再附带它们。
  用户自己选择的工具不会被悄悄去掉，接口拒绝时会直接说明，而不是提示重试。
- **离线评估**：运行 `uv run python scripts/evaluate_retrieval.py --database branch_learning.db`，
  在临时副本上查看检索范围和主题搜索在自己数据上的表现，原数据库不会改变；报告写入
  `artifacts/`，详见[离线检索评估](retrieval-evaluation.md)。这条命令使用关键词检索，不经过
  相似度和重排阈值；只有加 `--hybrid` 并准备好本地模型时，才会按当前默认值经过相似度门槛和
  重排截断，脚本也没有调整这些阈值的选项。其中的代理指标不是人工相关性判断。
