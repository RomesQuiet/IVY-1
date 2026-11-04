# Security and performance fixes

Status as of 2026-10-31. Run `python -m tests.run` to check.

## Security
| # | Fix | Status |
|---|-----|--------|
| 1 | Replay the app-deletion incident | Done: 8 attack variants must ask first and are refused even if OK is clicked. Uses the rule checks, not a live model. |
| 2 | Undo for destructive actions | Done: `remove_file` and `restore_removed`; `run_shell` no longer deletes; `.app` uninstalls are kept for restoring. |
| 3 | Permanent action log | Done: `~/Library/Logs/Ivy/actions.jsonl`, private, secrets hidden. |
| 4 | Protect API keys | Done: `.env`, `memory.json`, `settings.json`, `state.json` set to owner-only. Admins can still read them with `sudo`. |
| 5 | Stop stale copies | Done: `make_app.sh` warns about other accounts' copies; a copy older than the installed app refuses shell and uninstall tools. |

## Performance
| # | Fix | Status |
|---|-----|--------|
| 6 | Measure | Done: tool and model times are logged. See them with `python -m ivy.actionlog`. |
| 7 | Reduce rate-limit waits | Waiting on Romeu: needs a free Cerebras key in `.env`, then a live test. |
| 8 | Faster loading | Done: `sounddevice` loads only when voice is used. Brain and voice import about 9 s to 4.6 s. |

## Housekeeping
| # | Fix | Status |
|---|-----|--------|
| 9 | Back up | Fixes before this file are on `origin/main`; this work still needs committing. Decide whether `pixel_mario/` stays. |
| 10 | Real-use checks | Not done: needs Romeu. A real install through Ivy, real sites in her browser, Mail and selection on real data. |

## Thinking procedure (2026-11-01)
Every request now goes through the AI, with depth set by the route. See `CLAUDE.md`. Real runs with
tools replaced by stand-ins: "open safari" 432 in + 36 out, one call. "Why is the sky blue?" about
2,300 in total. A pong build used about 2,700 in + 2,000 out on its first call alone.

## Known limits
- `brew`, `pip` and `npm` uninstalls can't be restored from the holding folder; they can only be reinstalled.
- Scripts run by `run_python` can still delete files once approved, except in `/Applications`.
- Removed items stay in `~/Library/Application Support/Ivy/removed` until cleared by hand.
- Ivy now can't read `.env` from another Mac account, so she won't start there.
