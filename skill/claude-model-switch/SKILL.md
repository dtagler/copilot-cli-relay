---
name: claude-model-switch
description: Switch between Claude Code models exposed by copilot-cli-relay. Use when the user wants to change Claude models, pick a different Claude version, or asks about available Claude models. Triggers on phrases like "switch Claude model", "change Claude model", "use opus", "use sonnet", "use haiku", "which Claude models", "list Claude models", or any mention of switching to a specific Claude model variant. Do not use for Codex, OpenAI, GPT, or `/codex/v1/models` model selection. Also use when the user types /claude-model-switch.
---

# Claude Model Switch

Switch between Claude Code models using the built-in `/model` command. Present Claude models clearly and help the user pick the right one. This skill is Claude-specific; Codex/OpenAI model selection is configured through Codex provider settings, not the Claude `/model` command.

## Discover available models

Read the running relay's `/claude/v1/models` catalog before recommending a model.
Use the configured loopback port, normally:

```powershell
$catalog = Invoke-RestMethod -Uri 'http://127.0.0.1:4141/claude/v1/models' -NoProxy -MaximumRedirection 0 -TimeoutSec 15
$catalog.data | Select-Object id, display_name
```

This is metadata discovery, not inference. Do not read `.env`, extract credentials,
install software, or start/reconfigure the relay to list models. If the relay is
unavailable, say current availability could not be verified. Do not present a
remembered model list as live or guarantee account access.

## Behavior

With an argument, match the requested model to the returned IDs and give the
exact built-in `/model` command. Without an argument, show the returned models
and ask which one the user wants. Report unavailable exact selections rather
than silently substituting another model.

The relay's Claude catalog does not contain context-window metadata. Do not
invent context sizes or copy old context-window claims into the result.

## Shorthand Matching

Normalize dots, dashes, spaces, and an optional `claude` prefix, then compare
against the live IDs case-insensitively. For example, `opus47` can match
`claude-opus-4-7` only if that ID was returned. If a family name such as `opus`
matches multiple versions, ask the user to choose. Do not infer "latest" from a
fixed table or lexicographic ordering.

## Important

The `/model` command is a built-in CLI command — tell the user to run it themselves. Do NOT attempt to call it as a tool. Just output the exact command they should copy-paste or type.
