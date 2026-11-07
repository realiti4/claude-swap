---
name: handoff
description: Write a handoff note for the current session so the next session (for example after `cswap switch`) starts with the context. Use when the user runs /handoff or says they are switching accounts.
disable-model-invocation: true
---

# Handoff

Thinking from one Claude organization cannot be reused after `cswap switch`, so
write down what the next session needs to know.

1. Target file: the path given as the argument (`$ARGUMENTS`) if any. Otherwise
   run `echo ${XDG_STATE_HOME:-~/.local/state}/claude-handoff/$(pwd | tr / -).md`.
2. Write that file (overwrite it; the Write tool creates the directory) with
   these sections, short and concrete:
   - **Goal**: what we are trying to achieve
   - **Done**: what is finished, with file paths
   - **In progress / blocked**: where it stopped and why
   - **Next steps**: ordered, the first one should be doable immediately
   - **Decisions & gotchas**: choices already made, things not to redo
3. Reply with the path and a one-line summary. Do not paste the whole note.

Keep it under ~40 lines. Do not include secrets or credentials.
