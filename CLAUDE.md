# Project guidance for Claude

Operational conventions for this repo. Keep project-specific guidance here (it's
version-controlled and shared) rather than in per-user local memory.

## Running Python / tests / type-checks

The project's `.venv` interpreter must **not** be invoked directly. Always go
through **uv** (`uv run ...`). If `uv` is not on PATH, it may be installed as a
module of another Python interpreter — invoke it as `python -m uv run ...` with
that interpreter.

- Run tests:
  ```
  uv run pytest -q
  ```
- Type-check (Pylance/pyright engine):
  ```
  uv run --with pyright pyright src tests
  ```

On some Windows setups `uv` is not on PATH and lives as a module of the shared
interpreter, so prefix the above with the full interpreter path, e.g.
`& "$env:LOCALAPPDATA\Microsoft\WindowsApps\python.exe" -m uv run ...`

## Test + commit workflow

After completing each implementation/change, **run the tests and commit**
without being asked again (and type-check when types changed). This is a solo
repo and all history is on `main`, so committing directly to `main` is expected
here rather than branching.

## Commit messages (multi-line)

For any multi-line commit message, write the message to a temp file and use
`git commit -F <file>`, then delete the file. Inline `-m` here-strings are
fragile across shells:

- The **Bash** tool does not understand PowerShell `@'...'@`; it treats each `@`
  as a literal and prepends/appends a stray `@` line to the message.
- Even the **PowerShell** tool's `@'...'@` has mangled multi-line messages
  (e.g. a body with a unicode `…` plus embedded double-quotes caused git to
  receive the words as separate pathspecs).

Reserve inline `-m` for one-line subjects only.

End commit messages with:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```
