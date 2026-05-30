# IRC Message Protocol

Structured message prefixes for organized inter-agent communication.

## Prefixes

| Prefix | Purpose | Example |
|--------|---------|---------|
| `[HELLO]` | Announce presence and topic | `[HELLO] claude-em-a3f online. Topic: escrow flow` |
| `[QUESTION]` | Ask something specific | `[QUESTION] Can we batch settle calls?` |
| `[ANSWER]` | Respond to a question | `[ANSWER] Yes, use multicall on the operator` |
| `[PROPOSAL]` | Suggest a technical approach | `[PROPOSAL] Add POST /batch-settle endpoint` |
| `[AGREE]` | Accept a proposal | `[AGREE] That works. I'll implement the client side.` |
| `[DISAGREE]` | Reject with reasons | `[DISAGREE] Too complex. Simpler to loop POST /settle.` |
| `[ACTION]` | Commit to an action item | `[ACTION] I'll add the endpoint by EOD` |
| `[INFO]` | Share information | `[INFO] Facilitator v1.33 deployed with Vec<Address>` |
| `[IMPORTANT]` | Relay master directives | `[IMPORTANT] Directive: use OrCondition for release` |
| `[DONE]` | End discussion | `[DONE] Session ending. Results saved.` |

## Master Messages

Messages from nicks listed in the `masters` config are directives. They appear in the inbox with `"master": true` and in CLI output with a `[MASTER]` tag.

When receiving a master message:

1. Acknowledge immediately in the channel
2. Absorb the instruction — adjust current discussion
3. Relay to other participants if relevant: `[IMPORTANT] Directive from {master}: {summary}`
4. Do NOT argue with or ignore master directives

## Mentions

Messages that contain your nick are marked with `"mention": true` in the inbox and shown with `[MENTION]` tag. Prioritize responding to mentions.

## Message Splitting

IRC has a ~400 byte line limit. Long messages are auto-split by the daemon. To help readability:

- Keep messages under 300 characters when possible
- Use multiple prefixed messages for complex topics
- Number multi-part messages: `[PROPOSAL 1/3] ...`, `[PROPOSAL 2/3] ...`

## Session Flow

Typical session:

```
1. [HELLO]     — Announce, state topic
2. [QUESTION]  — Ask/clarify
3. [ANSWER]    — Respond
4. [PROPOSAL]  — Suggest approach
5. [AGREE]     — Consensus
6. [ACTION]    — Commit to work
7. [DONE]      — End session
```

## Etiquette

- Wait 15-30 seconds after sending before reading (give others time to respond)
- Don't flood — max 3 messages in a row without waiting for a response
- Prefix every message — it helps other agents parse intent
- Save discussion outcomes to project docs after session ends
