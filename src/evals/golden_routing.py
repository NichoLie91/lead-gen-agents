"""Golden routing cases (Phase 2): what the bot must do with owner messages.

Three layers are pinned:
- exact slash commands -> parse_command (deterministic);
- free text -> parse_intent_response (the Gemini contract: whitelisted
  commands, agent work, or a conversational reply);
- keyword routing fallback in LeadAgent._execute_agent_task (runs when the
  brain is unavailable).

A case = (message, expected_route) where expected_route is one of:
  command:<cmd>            -> parse_command must return this canonical command
  command:<cmd>:<args>     -> parse_command must return cmd + args
  intent:command:<cmd>     -> parse_intent_response must map JSON to this command
  intent:command:<cmd>:<args> -> ... with these args
  intent:agent             -> parse_intent_response must return action=agent
  intent:reply             -> parse_intent_response must return action=reply
  intent:invalid           -> parse_intent_response must return {} (fall back safe)
  route:<task>             -> LeadAgent._execute_agent_task keyword fallback must
                              call the named task method (find/score/draft/check/send)
"""
from __future__ import annotations

GOLDEN_ROUTING = [
    # ---- exact slash commands (layer 1: parse_command) ----
    ("/help", "command:/help"),
    ("/status", "command:/status"),
    ("/run", "command:/run"),
    ("/run full", "command:/run:full"),
    ("/run discovery", "command:/run:discovery"),
    ("/run outreach-email", "command:/run:outreach-email"),
    ("/run  full", "command:/run:full"),  # extra spaces normalize
    ("/drafts", "command:/drafts"),
    ("/list drafts", "command:/list drafts"),
    ("/approve", "command:/approve"),
    ("/approve all", "command:/approve:all"),
    ("/approve ab12cd34", "command:/approve:ab12cd34"),
    ("/reject all", "command:/reject all"),  # multi-word command IS canonical
    ("/reject 9f8e7d6c", "command:/reject:9f8e7d6c"),
    ("/inbound", "command:/inbound"),
    ("/followups", "command:/followups"),
    ("/stop", "command:/stop"),
    ("/sheet", "command:/sheet"),
    ("/id", "command:/id"),
    ("/usage", "command:/usage"),
    ("/usage 50", "command:/usage:50"),
    ("/send all email", "command:/send all email"),
    ("/send all instagram", "command:/send all instagram"),
    # parse_command is case-sensitive by design: uppercase input is NOT an
    # exact command (it falls through to the Gemini brain instead).
    ("/SEND ALL EMAIL", "command:"),
    ("  /status  ", "command:/status"),  # surrounding whitespace
    # ---- Gemini intent contract (layer 2: parse_intent_response) ----
    ('{"action": "command", "command": "/run", "args": "full"}', "intent:command:/run:full"),
    ('{"action": "command", "command": "/status"}', "intent:command:/status"),
    ('{"action": "command", "command": "/approve", "args": "all"}', "intent:command:/approve:all"),
    ('{"action": "command", "command": "/reject", "args": "ab12cd34"}', "intent:command:/reject:ab12cd34"),
    ('{"action": "command", "command": "/run", "args": "discovery"}', "intent:command:/run:discovery"),
    ('```json\n{"action": "command", "command": "/stop"}\n```', "intent:command:/stop"),
    ('Sure! {"action": "command", "command": "/sheet"}', "intent:command:/sheet"),
    # Gemini may only suggest whitelisted commands:
    ('{"action": "command", "command": "/rm -rf", "args": "all"}', "intent:invalid"),
    ('{"action": "command", "command": "send-email-directly"}', "intent:invalid"),
    # agent work:
    ('{"action": "agent", "task": "find plumber leads in Dallas", "params": {"vertical": "plumber", "city": "Dallas"}}', "intent:agent"),
    ('{"action": "agent", "task": "score ABC Plumbing 4.8 stars 120 reviews in Houston"}', "intent:agent"),
    ('{"action": "agent", "task": "draft an email for a dental clinic in Atlanta", "params": {}}', "intent:agent"),
    ('{"action": "agent", "task": ""}', "intent:invalid"),  # empty task is not agent work
    # replies:
    ('{"action": "reply", "text": "Six agents: Atlas, Scout, Enrichment, Outreach, Followups, Inbound."}', "intent:reply"),
    ('{"action": "reply", "text": ""}', "intent:reply"),
    # malformed / adversarial -> {} (caller falls back to a safe message):
    ('', "intent:invalid"),
    ('not json at all', "intent:invalid"),
    ('{"action": "unknown_action"}', "intent:invalid"),
    ('{"action": "command"}', "intent:invalid"),  # missing command
    ('{"task": "no action key"}', "intent:invalid"),
    ('{"action": "agent"}', "intent:invalid"),  # missing task
    # ---- keyword routing fallback (layer 3: _execute_agent_task) ----
    # route:<method> = the task method the keyword router must call.
    ("find me plumbing leads in Dallas", "route:_task_discover"),
    ("search for hvac businesses in Phoenix", "route:_task_discover"),
    ("discover new leads in Tampa", "route:_task_discover"),
    ("score ABC Plumbing 4.8 stars 120 reviews", "route:_task_score"),
    ("what rating does this business need", "route:_task_score"),
    ("draft an email for a dental clinic in Atlanta", "route:_task_draft"),
    ("write an email for Blue Creek Cleaning", "route:_task_draft"),
    ("what leads do we have in Tampa", "route:_task_check"),
    ("list the leads in the sheets", "route:_task_check"),
    ("show me HOT leads in Dallas", "route:_task_check"),
    # The draft branch owns the word 'email', so this routes to Outreach's
    # draft task, NOT the send trigger — priority order is pinned on purpose.
    ("send the approved emails", "route:_task_draft"),
    ("flush the queue now", "route:send"),
]
