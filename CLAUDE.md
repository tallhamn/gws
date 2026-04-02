# CLAUDE.md

## Git Workflow

- **Push finished work to main.** Do not leave completed work on local branches. Once work is done and verified, push to `main` to avoid merge conflicts from stale local branches.

## Worktrees

- Git worktrees live in `.worktrees/` at the repo root (already in `.gitignore`).
- Always create worktrees inside `.worktrees/` to keep the repo root clean and make cleanup predictable.
- When done with a worktree, remove it (`git worktree remove .worktrees/<name>`) and delete the branch if it was merged.
