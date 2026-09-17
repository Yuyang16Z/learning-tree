# Memory management

Open **Settings → Memory** to manage what LearningTree recalls. The interface is
available in English and Chinese.

## Preferences and habits

Existing automatic preferences first appear together in one editable text area.
Reading this view does not rewrite or delete the original records. Duplicate
text is combined for display.

The editor stays at a fixed height and scrolls internally. Long content remains
editable without pushing the topic-memory controls farther down the page.

Save explicitly to make the profile user-managed. From then on, automatic
extraction does not append preferences or overwrite the profile; topic facts
continue to be extracted. An empty saved profile intentionally disables those
older preferences. Temporary study topics belong in topic facts, not permanent
habits. Existing historical classifications are preserved until you edit them.

The profile accepts up to 6,000 characters. Longer legacy content remains visible
without truncation and must be shortened before saving. Switching settings tabs
keeps the draft; closing settings with unsaved changes asks before discarding it.
Simultaneous edits from another window cause a conflict instead of silently
overwriting either version. Copy your local draft before loading a newer version
if you want to combine the two.

The profile is included as user-provided learning context, with the current
request taking precedence. It remains subject to the configured model's context
budget; long profiles can be omitted as a whole when they do not fit.

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
revision. Existing `Memory` rows and IDs remain intact during migration. A saved
profile supersedes historical preference rows; facts continue to use `Memory`.
Database backups include both. Individual learning-tree JSON exports do not
export the global preference profile.

Legacy `GET /memories` remains available. The new management endpoints are
`GET/PUT /memories/preferences`, `GET /memories/facts`, `PATCH /memories/{id}`,
and `POST /memories/delete` for selected fact IDs. Both direct and `/api` routes
are available. Profile writes use a revision token; fact edits compare the
expected original content before committing. Clear-all leaves an empty managed
profile so an older preference edit cannot silently restore cleared content.
