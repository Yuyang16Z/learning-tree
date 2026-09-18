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
current learning path: Settings search does not make sibling branches eligible.

The source conversation can still appear in ordinary chat context after its
derived fact is deleted. Later extraction from a new completed answer may produce
a similar fact. This is management of built-in memory, separate from any
configured MCP memory server.

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
