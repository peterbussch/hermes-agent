---
name: obsidian
description: Read, search, create, and edit notes in the Obsidian vault.
version: 1.0.1
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Obsidian, Notes, Markdown, Vault]
    related_skills: []
---

# Obsidian Vault

Use this skill for filesystem-first Obsidian vault work: reading notes, listing notes, searching note files, creating notes, appending content, and adding wikilinks.

## Vault path

Use a known or resolved vault path before calling file tools. Never create a
vault merely because a default path is absent.

On Peter's workstation, the existence of
`/Users/peterbusscher/vault/AGENTS.md` binds this skill to the canonical vault
at `/Users/peterbusscher/vault` unless the user explicitly names a different
vault in the current request. This binding takes precedence over an unset
`OBSIDIAN_VAULT_PATH`. If that variable points elsewhere, do not treat the
other directory as Peter's canonical knowledge base without explicit
confirmation.

On other machines, use the concrete path in `OBSIDIAN_VAULT_PATH`. If it is
unset, ask for the vault path and fail closed; do not fall back to
`~/Documents/Obsidian Vault` or initialize a new directory.

File tools do not expand shell variables. Do not pass paths containing `$OBSIDIAN_VAULT_PATH` to `read_file`, `write_file`, `patch`, or `search_files`; resolve the vault path first and pass a concrete absolute path. Vault paths may contain spaces, which is another reason to prefer file tools over shell commands.

If the vault path is unknown, `terminal` is acceptable for resolving
`OBSIDIAN_VAULT_PATH` or checking the Peter-workstation marker above. Once the
path is known, switch back to file tools.

## Peter's canonical vault

When the Peter-workstation binding is active:

1. Read `AGENTS.md`, `_system/POLICY.md`, `_system/TAXONOMY.md`,
   `_system/SCHEMA.md`, and the nearest topic index before writing. Honor human
   edit guards and taboo zones.
2. Search before creating. Prefer
   `/Users/peterbusscher/vault/_system/bin/llmwiki search "QUERY" --limit 10`,
   then inspect the relevant topic `wiki/_index.md` or recursive Base. Update an
   existing canonical note when it already answers the need.
3. For an immutable `_session_research.md` capture, use the existing helper with
   reviewed metadata and inspect its JSON dry run before saving:

   ```bash
   python3 /Users/peterbusscher/.agents/skills/vault-save/scripts/vault_persist.py \
     /absolute/path/to/_session_research.md --source hermes \
     --project PROJECT_SLUG --topic TOPIC_SLUG \
     --related '[[vault/relative/existing-note]]' \
     --source-task ACTUAL_TASK_ID --dry-run
   ```

   Repeat without `--dry-run` only after the destination and metadata are
   correct. Re-run the exact command to verify `status: unchanged`. Do not
   imitate the helper with a direct `_inbox` write.
4. Use ordinary file tools for reviewed article/index edits that are not session
   captures. Do not create a second `SCHEMA.md`, `index.md`, `log.md`, wiki root,
   or Obsidian Sync configuration.

## Read a note

Use `read_file` with the resolved absolute path to the note. Prefer this over `cat` because it provides line numbers and pagination.

## List notes

Use `search_files` with `target: "files"` and the resolved vault path. Prefer this over `find` or `ls`.

- To list all markdown notes, use `pattern: "*.md"` under the vault path.
- To list a subfolder, search under that subfolder's absolute path.

## Search

Use `search_files` for both filename and content searches. Prefer this over `grep`, `find`, or `ls`.

- For filenames, use `search_files` with `target: "files"` and a filename `pattern`.
- For note contents, use `search_files` with `target: "content"`, the content regex as `pattern`, and `file_glob: "*.md"` when you want to restrict matches to markdown notes.

## Create a note

Use `write_file` with the resolved absolute path and the full markdown content. Prefer this over shell heredocs or `echo` because it avoids shell quoting issues and returns structured results.

## Append to a note

Prefer a native file-tool workflow when it is not awkward:

- Read the target note with `read_file`.
- Use `patch` for an anchored append when there is stable context, such as adding a section after an existing heading or appending before a known trailing block.
- Use `write_file` when rewriting the whole note is clearer than constructing a fragile patch.

For an anchored append with `patch`, replace the anchor with the anchor plus the new content.

For a simple append with no stable context, `terminal` is acceptable if it is the clearest safe option.

## Targeted edits

Use `patch` for focused note changes when the current content gives you stable context. Prefer this over shell text rewriting.

## Wikilinks

Obsidian links notes with `[[Note Name]]` syntax. When creating notes, use these to link related content.
