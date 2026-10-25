# Offline retrieval evaluation

`scripts/evaluate_retrieval.py` measures, on a temporary copy of your own
database, how the local token estimator compares with the earlier UTF-8 byte
count, what memory retrieval returns for the questions you actually asked, and
whether topic search finds their nodes through text other than the question
itself. It never changes your database, calls no model API and downloads
nothing. A deterministic synthetic fixture exercises the same code
for tests and for trying the script without personal data. Its relevance labels
are structural proxies; `scripts/label_retrieval.py` lets you grade the facts
yourself, and `--labels` then scores retrieval against your grades too.

## Running it

Run from the repository root. Point `--database` at the SQLite file the app uses
(`branch_learning.db` unless `DATABASE_URL` selects another file):

```sh
uv run python scripts/evaluate_retrieval.py --database branch_learning.db
uv run python scripts/evaluate_retrieval.py --synthetic
```

The app may keep running. If another program opens the file while it is being
copied, nothing is evaluated and the script asks you to retry.

| Option | Meaning |
| --- | --- |
| `--database PATH` | Evaluate a temporary copy of this file. |
| `--synthetic` | Evaluate the built-in fixture instead (exactly one of the two). |
| `--hybrid` | Use the cached local embedding and reranking models; both must be prepared. Without it, retrieval is keyword-only, like `MEMORY_RETRIEVAL_MODE=lexical`. |
| `--scopes path,tree,all` | Retrieval scopes to replay (default: all three). |
| `--k 5` | Cut-off for the proxy metrics (default 5). |
| `--include-text` | Also write questions, search terms and returned fact text. Off by default. |
| `--labels FILE` | Also score retrieval against your own grades from `scripts/label_retrieval.py` (see [Grading relevance yourself](#grading-relevance-yourself)). The file must exist and be valid, or nothing is evaluated (status 1). |
| `--output FILE` | Report path (default `artifacts/retrieval-evaluation.json` in the repository). It may not be the database, under any spelling or alias of its path, or a file beside it whose name starts with the database's name, such as its `-wal` file. |

Exit status 0 means the report was written. Status 1 means the source or the
report path was refused, or the source could not be read; nothing was evaluated
and no report was written.
Status 2 means `--hybrid` was requested but the local embedding or reranking
model is not installed or prepared (no report), or hybrid retrieval was not
achieved: some retrieval fell back during the run (to keywords when embedding
failed, or to the ranking before reranking when the reranker failed), or no
question had facts to rank, so the models were never used. The report is then
written with `semantic.achieved` false, and `semantic` counts the retrievals
that called each model and those that fell back. Command-line usage errors also
exit with 2. Install and prepare the models with
`uv run python scripts/manage.py retrieval` first; the script only loads cached
weights.

## How the copy is made

The file is copied with SQLite's backup API from a read-only connection into a
fresh temporary directory that only you can read (`learning-tree-eval-*` under
the system temporary location), and that directory is deleted when the script
ends. A plain read-only connection to a cleanly closed write-ahead-log database
would leave `-wal` and `-shm` files next to it, so such a file is opened as
immutable; a database with a live log is read through that log, which creates
nothing. A log without its shared-memory file or a leftover rollback journal is
refused: start and stop LearningTree once so SQLite can recover it.

`DATABASE_URL` points at the copy before any application module is imported,
and the web application (`app.main`) is never imported. The copy receives the
app's additive start-up migration, so a database written by an older version
can be evaluated while the original stays byte-identical. All writes the
evaluation needs go to the copy: the retrieval scope switches, temporarily
hidden facts for the replay below, and, with `--hybrid`, cached vectors. While
facts are hidden and revealed, the copy's search-index triggers on facts are
suspended, since each write would otherwise rescan the whole index; retrieval
does not read the index, and topic search is measured before the replay.

## What is measured

**Estimator per learning path.** Every node with a completed answer defines a
learning path: the system prompt that path gets plus every completed question
and answer from the root, as a follow-up from that node would carry them,
without retrieved memories, document excerpts, tool definitions or the
follow-up itself. The report compares its UTF-8 byte count with
`estimate_text_tokens`, gives summary statistics and the bytes per estimated
unit, and counts the paths that would be compacted under each estimator with
the default 32,768-token window and 4,096-token answer reserve and with a
1,048,576-token window, using your `CONTEXT_COMPACTION_RATIO` and the automatic
request size target (131,072 tokens, which decides compaction on the large
window). These are local estimates, not provider token counts.

**Memory retrieval.** Every completed question is replayed through
`retrieve_memory` with its node's learning path, quoted passage and note, once
per scope: `path` (the current learning path only), `tree` (plus other branches
of the topic) and `all` (plus other non-archived topics), each saved as the
retrieval settings of the copy. Each question sees only the facts that existed
when it was asked: created earlier and not yet superseded by a correction. For
each scope the report counts returned facts by origin and type, records the
mode (`lexical` or `hybrid`), reranking and latency, and scores two proxy
labels against the rendered fact list (path facts first, then other branches,
then other topics):

- **self**: facts extracted from the question's own node and from the earlier
  turns of its follow-up thread (its parent, grandparent and so on, up to the
  root or the branch where the thread started), each source node scored
  separately;
- **parent**: facts extracted from the parent node, for follow-ups and
  branches alike.

Labels compare facts by their text, normalized as retrieval normalizes it
before removing duplicates: facts with the same text count once, whichever of
them is returned, and a fact whose text is already in the question's quoted
passage or learning note is left out of the label, because retrieval never
returns what the prompt already holds. The report counts those facts as
`in_prompt` for each scored label and `facts_in_prompt` for each scope.
`recall_at_k` is the share of a label's texts found in the top k, divided by
min(k, label size); `hit_rate_at_k` is the share of cases with at least one of
them in the top k; `mrr_at_k` is the mean reciprocal rank of the first one
within the top k. Questions without such facts are not counted.

**Topic search.** For each completed question, a key term is cut from the
question itself: its longest run of Chinese, Japanese or Korean characters or
its longest word, of at least three characters, where one such character weighs
as much as two letters (so `请解释 Transformer` gives `Transformer`). The term is
searched in the question's topic with the full-text query of the map search
(twenty results). The question's own message is indexed (as a `question` hit)
and contains the term, so that match is counted separately
(`own_question_found`); `node_found` and `node_rank` only count other hits on
the question's node: its answers, facts, note or other turns. The report also
records hit counts by kind and latency. The figures depend on the term rule as
much as on search, so compare them only between runs on the same data.

**Timing.** Retrieval latency per scope and search latency are reported as
count, minimum, p50, p90, p95, p99, maximum and mean in milliseconds
(`memory_retrieval.scopes.<scope>.latency_ms`, `full_text_search.latency_ms`).
Each scope starts with one untimed warm-up call. Latency depends on the machine
and the database; with `--hybrid`, the first retrieval that sees a fact also
embeds it unless the copy already holds its vector. Compare runs on the same
machine only.

## Proxy labels are not relevance judgments

The labels assume that facts from the same thread or from the parent node are
relevant to a later question. That is often true and sometimes wrong: a
follow-up can change subject, and a parent's fact can be correct but beside the
point. Facts from anywhere else are never labelled, so precision, the value of
the wider scopes and answer quality are not measured; wider scopes only show up
when their facts compete with path facts for the candidate list or the
reranker's relative cutoff. For a follow-up, the parent is also the nearest
earlier turn, so the two labels overlap. The replay uses the current text of
facts, notes, titles and quotes, does not restore deleted facts, and takes node
status and archiving as they are now. Scores on the synthetic fixture only show
that the script works; they are not quality claims. Use the numbers to compare
settings or versions on the same data, and read the cases behind surprising
results.

## Grading relevance yourself

Proxy labels cannot tell a relevant fact from a merely nearby one. To measure
that, grade the candidates yourself, once, and reuse the grades for every later
run, for example before and after changing retrieval:

```sh
uv run python scripts/label_retrieval.py --database branch_learning.db
uv run python scripts/evaluate_retrieval.py --database branch_learning.db --labels artifacts/retrieval-labels.json
```

`label_retrieval.py` reads a temporary copy exactly as the evaluation does and
replays every completed question against the facts that existed when it was
asked. Its candidates are pooled, so that the grades are not limited to what
today's ranking returns: the facts of the question's own node, its follow-up
thread and its parent, then the ranked lists of keyword retrieval (and, with
`--hybrid`, the local semantic models) in each scope of `--scopes`, `--depth`
deep (default 10), interleaved by rank, one fact per normalized text and at
most `--pool` facts (default 12). Facts whose text the question's quote or
note already holds are left out, since retrieval never returns them.

Each question with ungraded candidates is shown in the terminal with them,
numbered. Grade each fact **2** (directly useful for answering), **1** (related)
or **0** (not relevant): type the numbers of the relevant facts, each graded 2
unless written `n=1` (or `n=0`); facts not listed get 0, so Enter alone means
none is relevant. `s` skips a question and `q` saves and stops. Grades are saved
after every question to `artifacts/retrieval-labels.json` (`--labels` names
another file) and a later run asks only about candidates without a current
grade. A grade belongs to a question and fact by id together with SHA-256
hashes of both texts, so editing a question makes all its grades stale and
editing a fact makes its grade stale: the labelling asks again, and the
evaluation counts them (`stale_questions`, `stale_facts`) and ignores them.
`--status` reports how much is graded without asking anything.

With `--labels`, each scope gains `labelled` beside the proxies, and every case
of a graded question carries its own `labelled` entry. Facts are compared by
normalized text as the proxies are, with the best grade of each text:

- `precision_at_k`: relevant (grade 1 or 2) texts in the top k, divided by k;
- `recall_at_k`: relevant texts in the top k, divided by min(k, relevant
  texts), as for the proxies;
- `mrr_at_k`: 1 / rank of the first relevant text within the top k, or 0;
- `ndcg_at_k`: discounted cumulative gain (gain 2^grade − 1, discount
  log2(rank + 1)) of the top k, divided by that of the ideal order of the
  graded texts;
- `judged_at_k`: graded texts among everything returned in the top k, over all
  graded questions. A low share means that retrieval now returns facts nobody
  graded, which count as not relevant: run the labelling again.

The first four are averaged over graded questions with at least one relevant
fact; a question with none still counts in `questions` and `judged_at_k`.
Grades are your judgment on the facts as they exist now, under the same replay
limits as the proxies below; compare runs on the same data and grades.

## Privacy

The report contains record ids, counts, scores, timings and settings: no
absolute paths, no file names and, unless `--include-text` is given, no
question, answer, fact, note or title text. It is written with owner-only
permissions to `artifacts/`, which Git ignores and `scripts/check_release.py`
refuses to publish. A report with `--include-text` holds your own words; keep it
private and delete it when you are done. Only reports from the synthetic fixture
may be published as evidence. The labels file holds question and fact ids,
grades and SHA-256 hashes of their texts, never the texts, and is written the
same way; a hash can still confirm a guessed text, so keep it private too.
The labelling shows your questions and facts in the terminal only. The temporary copy holds everything in the
database, including model keys, and is deleted at exit; if the process is
killed, remove any leftover `learning-tree-eval-*` directory from the system
temporary location.

## Synthetic fixture

`--synthetic` builds a fixed database in the temporary directory: four topics
(one archived), branches with quoted passages, follow-up threads, a legacy node
with two turns, an image question, unfinished and failed answers, learning
notes, facts of every type and a correction that supersedes an earlier fact,
with fixed timestamps. Two long answers make one learning path compact only
under the byte count and another under both estimators. `scripts/test_tooling.py`
runs it end to end.

## 中文说明

`scripts/evaluate_retrieval.py` 在你自己数据库的临时副本上做离线评估：比较字符类别估算器与旧的
UTF-8 字节计数、回放真实问题的记忆检索、检查主题全文搜索。它不修改原数据库、不调用模型 API、
不下载任何内容。

- **运行**：在仓库根目录执行
  `uv run python scripts/evaluate_retrieval.py --database branch_learning.db`；不带个人数据试用可加
  `--synthetic`。应用可以保持运行；复制期间若有其他程序打开文件，本次不评估并提示重试。
  `--hybrid` 只加载已准备好的本地向量模型和重排模型，两者都必须可用（先运行
  `uv run python scripts/manage.py retrieval`）；
  `--scopes` 选择回放范围（默认 path、tree、all），`--k` 为指标截断（默认 5），
  `--include-text` 才会写入问题、检索词和返回的事实原文，`--output` 指定报告路径
  （默认仓库内 `artifacts/retrieval-evaluation.json`；不能是数据库本身，无论路径如何拼写或经由别名，
  也不能是数据库旁边以其文件名开头的文件，例如 `-wal`）。退出码 0 表示已写报告；1 表示源文件或
  报告路径被拒绝、或源文件无法读取，未评估也未写报告；2 表示请求了混合检索但向量模型或重排模型
  不可用（不写报告），或没有完整实现混合检索：运行中有检索退回（向量失败时退回关键词，重排失败时
  使用重排前的排序），或没有任何问题有可排序的事实、模型从未被调用（报告中 `semantic.achieved`
  为 false，`semantic` 记录调用各模型的检索数和退回数）；命令行参数错误也返回 2。
- **副本**：通过只读连接和 SQLite 备份 API 复制到仅本人可读的临时目录，结束时删除。干净关闭的
  WAL 数据库以 immutable 方式读取，避免在原文件旁留下 `-wal`/`-shm`；有活动日志时经由日志读取；
  只有日志没有共享内存文件、或残留回滚日志时拒绝执行。导入任何应用模块之前就把 `DATABASE_URL`
  指向副本，从不导入 `app.main`；旧版本数据库只在副本上做增量迁移，原文件字节不变。
- **估算器**：每个有已完成回答的节点对应一条学习路径（系统提示加上从根到该节点的全部已完成问答，
  不含记忆、文档摘录、工具定义和新问题）。报告给出字节数与 `estimate_text_tokens` 的统计、
  每单位字节数，以及在默认 32,768/4,096 窗口和 1,048,576 窗口下、按当前压缩比例和自动的单次请求目标
  （131,072，大窗口下由它决定何时压缩）各有多少路径会被压缩。这些都是本地估算，不是服务商的 token 计数。
- **记忆检索**：每个已完成问题按 path、tree、all 三种范围（写入副本的检索设置）用其学习路径、
  引用和笔记回放 `retrieve_memory`，且只看到提问时已存在、尚未被更正替代的事实。报告记录返回
  事实的来源和类型、模式、是否重排、耗时，并用两类代理标签计算 recall@k、hit rate@k 和 MRR@k：
  self 为本节点及其追问链中更早轮次提炼的事实（每个来源节点单独计分），parent 为父节点的事实。
  标签按与检索去重相同的规范化文本比较：文本相同的事实只计一次；文本已包含在问题的引用或学习笔记
  中的事实不计入标签，因为检索不会返回提示中已有的内容，报告用 `in_prompt`（每个计分标签）和
  `facts_in_prompt`（每个范围）记录这类事实的数量。
- **主题搜索**：检索词取自问题本身：问题中最长的中日韩字符串或最长的单词（至少 3 个字符；
  比较时一个中日韩字符按两个字母计，例如 `请解释 Transformer` 取 `Transformer`），用地图搜索的
  全文查询在其主题内检索（20 条结果）。问题本身的消息已被索引且必然包含检索词，这类命中单独记为
  `own_question_found`；`node_found` 和 `node_rank` 只统计命中该节点其他内容（回答、事实、笔记或
  其他轮次）的情况。报告还记录各类命中数和耗时。结果同样取决于检索词规则，只在同一数据的多次运行
  之间比较。
- **耗时**：检索（分范围）和搜索给出 p50/p90/p95/p99 等毫秒分位数；每个范围先做一次不计时的
  预热。结果依赖机器和数据规模，只在同一台机器上比较。
- **自己标注相关性**：`uv run python scripts/label_retrieval.py --database branch_learning.db`
  在同样的临时副本上回放每个已完成问题（只看提问时已存在的事实），把候选事实汇集起来：本节点、
  追问链和父节点的事实，加上关键词检索（加 `--hybrid` 时再加本地语义模型）在各范围、前
  `--depth`（默认 10）名的结果，按名次交替合并，同一规范化文本只保留一条，每题最多 `--pool`
  （默认 12）条；文本已在引用或笔记中的事实不列出。终端里逐题显示问题和编号的候选：输入相关事实的
  编号即评 2 分（直接有用），写成 `n=1` 为 1 分（相关）、`n=0` 为 0 分，未列出的都是 0 分，直接回车
  表示都不相关；`s` 跳过，`q` 保存并退出。每题评完就保存到 `artifacts/retrieval-labels.json`
  （`--labels` 可换文件），文件只含问题和事实的编号、分数及原文的 SHA-256 哈希，不含原文；之后
  再运行只问还没有有效评分的候选。问题或事实被修改后哈希不再一致，对应评分作废：标注时重新询问，
  评估时计入 `stale_questions`/`stale_facts` 并忽略。`--status` 只报告进度、不提问。
  评估时加 `--labels artifacts/retrieval-labels.json`，每个范围多出 `labelled`：precision@k
  （前 k 中相关文本数 ÷ k）、recall@k（÷ min(k, 相关数)，与代理标签相同）、MRR@k、nDCG@k
  （增益 2^分数 − 1、折扣 log2(名次 + 1)，除以评分事实理想排序的值），以上四项对至少有一条相关
  事实的已评问题取平均；judged@k 为前 k 结果中已评分文本的比例，偏低说明检索开始返回没人评过的
  事实（按不相关计），应再标注一轮。评分代表你对现有事实的判断，回放局限与代理标签相同；只在同一
  数据和同一份评分之间比较。评分文件同样请私下保管：哈希仍可用来确认猜到的原文。
- **局限**：代理标签假设同一追问链或父节点的事实与后续问题相关，这常常成立但并不总是成立；其他
  位置的事实从不标为相关，因此不衡量精确率、扩大范围的价值或回答质量（自己标注后可以衡量前两项）。追问的父节点同时也是最近
  的前一轮，两类标签有重叠。回放使用事实、笔记、标题和引用的当前内容，不恢复已删除的事实。合成
  数据上的分数只说明脚本可用，不代表质量。
- **隐私**：报告只含记录编号、计数、分数、耗时和设置，没有绝对路径和文件名；不加
  `--include-text` 就没有任何问答、事实、笔记或标题原文。报告以仅本人可读的权限写入被 Git 忽略、
  且被 `scripts/check_release.py` 拒绝发布的 `artifacts/`。带原文的报告请自行保管并及时删除；
  只有合成数据的报告可以作为证据公开。临时副本包含数据库中的全部内容（包括模型密钥），结束时删除；
  若进程被强制终止，请手动删除系统临时目录中残留的 `learning-tree-eval-*` 目录。
