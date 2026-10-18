# copilot-cli-relay

Client and routing terms used by this project. App terminology follows the
[ChatGPT desktop app documentation](https://learn.chatgpt.com/docs/app),
checked on October 18, 2026.

## Language

**ChatGPT desktop app**:
The unified desktop application containing ChatGPT and Codex experiences.
_Avoid_: Codex Desktop as the current product name

**Codex in the ChatGPT desktop app**:
The Codex experience within the unified app, distinct from ChatGPT Chat and Work.
_Avoid_: ChatGPT support when only the Codex experience is meant

**Codex Desktop**:
The historical label for the Codex desktop experience, now accessed within
the unified ChatGPT app.

**Local Codex chat**:
A Codex conversation whose execution environment is the user's computer,
rather than a cloud environment.
_Avoid_: Cloud task when referring to a local chat

**Relay provider**:
The named Codex model connection that points to this project's local relay.
_Avoid_: Copilot credential when referring to the connection definition

**Copilot upstream**:
The GitHub Copilot service that authenticates and serves the model request
forwarded by the relay.
_Avoid_: OpenAI backend or Anthropic backend when describing the relay's destination
