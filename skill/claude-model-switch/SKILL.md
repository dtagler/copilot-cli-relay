---
name: claude-model-switch
description: Switch between Claude Code models exposed by copilot-cli-relay. Use when the user wants to change Claude models, pick a different Claude version, or asks about available Claude models. Triggers on phrases like "switch Claude model", "change Claude model", "use opus", "use sonnet", "use haiku", "which Claude models", "list Claude models", or any mention of switching to a specific Claude model variant. Do not use for Codex, OpenAI, GPT, or `/codex/v1/models` model selection. Also use when the user types /claude-model-switch.
---

# Claude Model Switch

Switch between Claude Code models using the built-in `/model` command. Present Claude models clearly and help the user pick the right one. This skill is Claude-specific; Codex/OpenAI model selection is configured through Codex provider settings, not the Claude `/model` command.

## Available Models

| Shorthand     | Model ID                          | Context  |
|---------------|-----------------------------------|----------|
| opus-4.7-1m   | `claude-opus-4-7-1m-internal`     | 1M       |
| opus-4.6-1m   | `claude-opus-4-6-1m`             | 1M       |
| opus-4.7      | `claude-opus-4-7`                | 200K     |
| opus-4.6      | `claude-opus-4-6`                | 200K     |
| opus-4.5      | `claude-opus-4-5`                | 200K     |
| sonnet-4.6    | `claude-sonnet-4-6`              | 200K     |
| sonnet-4.5    | `claude-sonnet-4-5`              | 200K     |
| haiku-4.5     | `claude-haiku-4-5`               | 200K     |

## Behavior

When the user invokes this skill:

1. **With an argument** — match it to a Claude model ID (exact or shorthand) and tell the user the exact `/model` command to run. For example, if they say `/claude-model-switch opus47`, respond with: "Run `/model claude-opus-4-7` to switch."

2. **Without an argument** — display the table of available models and ask which one they want.

## Shorthand Matching

Accept flexible shorthands. Strip dots, dashes, and spaces, then match case-insensitively:

- `opus47`, `opus-4.7`, `opus 4.7` → `claude-opus-4-7`
- `opus47-1m`, `opus-4.7-1m`, `opus471m` → `claude-opus-4-7-1m-internal`
- `opus46`, `opus-4.6`, `opus 4.6` → `claude-opus-4-6`
- `opus46-1m`, `opus-4.6-1m`, `opus461m` → `claude-opus-4-6-1m`
- `opus45`, `opus-4.5` → `claude-opus-4-5`
- `sonnet46`, `sonnet-4.6` → `claude-sonnet-4-6`
- `sonnet45`, `sonnet-4.5` → `claude-sonnet-4-5`
- `haiku45`, `haiku-4.5` → `claude-haiku-4-5`
- Just `opus` → default to `claude-opus-4-7` (latest Opus)
- Just `sonnet` → default to `claude-sonnet-4-6` (latest Sonnet)
- Just `haiku` → default to `claude-haiku-4-5` (latest Haiku)

## Important

The `/model` command is a built-in CLI command — tell the user to run it themselves. Do NOT attempt to call it as a tool. Just output the exact command they should copy-paste or type.
