---
name: claude-model-switch
description: Switch between Claude Code models exposed by copilot-cli-relay. Use when the user wants to change Claude models, pick a different Claude version, or asks about available Claude models. Triggers on phrases like "switch Claude model", "change Claude model", "use opus", "use sonnet", "use haiku", "which Claude models", "list Claude models", or any mention of switching to a specific Claude model variant. Do not use for Codex, OpenAI, GPT, or `/codex/v1/models` model selection. Also use when the user types /claude-model-switch.
---

# Claude Model Switch

Switch between Claude Code models using the built-in `/model` command. Present Claude models clearly and help the user pick the right one. This skill is Claude-specific; Codex/OpenAI model selection is configured through Codex provider settings, not the Claude `/model` command.

## Available Models

| Shorthand     | Model ID                          | Context  |
|---------------|-----------------------------------|----------|
| opus-5        | `claude-opus-5`                  | 1M       |
| opus-4.8      | `claude-opus-4-8`                | 1M       |
| opus-4.7      | `claude-opus-4-7`                | 1M       |
| opus-4.6      | `claude-opus-4-6`                | 1M       |
| sonnet-5      | `claude-sonnet-5`                | 1M       |
| sonnet-4.6    | `claude-sonnet-4-6`              | 1M       |
| haiku-4.5     | `claude-haiku-4-5`               | 200K     |

## Behavior

When the user invokes this skill:

1. **With an argument** — match it to a Claude model ID (exact or shorthand) and tell the user the exact `/model` command to run. For example, if they say `/claude-model-switch opus47`, respond with: "Run `/model claude-opus-4-7` to switch."

2. **Without an argument** — display the table of available models and ask which one they want.

## Shorthand Matching

Accept flexible shorthands. Strip dots, dashes, and spaces, then match case-insensitively:

- `opus5`, `opus-5`, `opus 5` → `claude-opus-5`
- `opus48`, `opus-4.8`, `opus 4.8` → `claude-opus-4-8`
- `opus47`, `opus-4.7`, `opus 4.7` → `claude-opus-4-7`
- `opus46`, `opus-4.6`, `opus 4.6` → `claude-opus-4-6`
- `sonnet5`, `sonnet-5`, `sonnet 5` → `claude-sonnet-5`
- `sonnet46`, `sonnet-4.6` → `claude-sonnet-4-6`
- `haiku45`, `haiku-4.5` → `claude-haiku-4-5`
- Just `opus` → default to `claude-opus-5` (latest Opus)
- Just `sonnet` → default to `claude-sonnet-5` (latest Sonnet)
- Just `haiku` → default to `claude-haiku-4-5` (latest Haiku)

## Important

The `/model` command is a built-in CLI command — tell the user to run it themselves. Do NOT attempt to call it as a tool. Just output the exact command they should copy-paste or type.
