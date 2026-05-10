"""
Memory prompts — distillation and recall prompt templates.

Prompts are fetched from **LangFuse Prompt Management** at runtime.
Local fallbacks below are used when the prompt hasn't been created
in LangFuse yet, so the system works out-of-the-box.

To manage these prompts in LangFuse Cloud:
  1. Open LangFuse → Prompts → + New Prompt
  2. Create prompts with the names shown in LANGFUSE_PROMPT_NAMES
  3. Use {{variable}} (Mustache syntax) for template variables
  4. Publish a version → it's live instantly, no code deploy needed
"""

from infastructure.observability import fetch_prompt

# ---------------------------------------------------------------------------
# LangFuse prompt names — create these in your LangFuse dashboard
# ---------------------------------------------------------------------------

LANGFUSE_PROMPT_NAMES = {
    "distill_system": "gift-concierge-distill-system",
    "distill_user":   "gift-concierge-distill-user",
    "recall_system":  "gift-concierge-recall-system",
    "recall_user":    "gift-concierge-recall-user",
}

# ---------------------------------------------------------------------------
# Fallback: Distillation prompts
# Used to extract memorable facts from a conversation turn
# ---------------------------------------------------------------------------

_DISTILL_SYSTEM_FALLBACK = """\
You are a memory extraction specialist for a gift concierge AI assistant.

Your task is to extract important facts from conversations that should be remembered
long-term to personalise future gift recommendations.

EXTRACTION RULES:
1. Extract recipient details — who the user is buying for (name, relationship)
2. Extract preferences — what the recipient likes or dislikes
3. Extract budget information — how much the user typically spends
4. Extract upcoming occasions — birthdays, anniversaries, holidays with dates
5. Extract past gift history — what was given before and whether it was well-received
6. Extract delivery constraints — location, delivery date, special packaging needs
7. Skip casual chitchat and one-time situational details with no future value

AUTOMATIC CATEGORISATION:
Automatically determine the appropriate tags for each fact. Common categories:
- recipient, relationship, name
- preference, like, dislike, taste
- budget, price_range, affordability
- occasion, birthday, anniversary, wedding, christmas, valentines
- category, cake, flower, chocolate, hamper, jewellery, cosmetics
- delivery, location, date, urgent
- past_gift, gift_history, repeat_avoid
- dietary, allergy, vegetarian, vegan
- age, gender, personality

OUTPUT FORMAT:
Return a JSON array of facts. Each fact must have:
{
  "text": "The distilled fact in natural language",
  "tags": ["tag1", "tag2"],   // 2–4 auto-detected tags
  "has_reminder": false,       // true if this is a reminder request
  "time_info": null            // timing details if has_reminder is true
}

RULES:
- Be concise — one fact per item
- Maximum 10 facts per extraction
- Always include 2–4 relevant tags

Example output:
[
  {
    "text": "User is buying a gift for their wife who loves dark chocolate and orchids",
    "tags": ["recipient", "preference", "chocolate", "flower"],
    "has_reminder": false,
    "time_info": null
  },
  {
    "text": "Wife's birthday is on June 15th, budget around LKR 5000",
    "tags": ["occasion", "birthday", "budget"],
    "has_reminder": true,
    "time_info": "June 15th"
  },
  {
    "text": "Previously gifted a rose bouquet for last anniversary — well received",
    "tags": ["past_gift", "anniversary", "flower"],
    "has_reminder": false,
    "time_info": null
  },
  {
    "text": "User's mother dislikes overly sweet items, prefers fruit-based gifts",
    "tags": ["recipient", "dislike", "preference", "fruitbaskets"],
    "has_reminder": false,
    "time_info": null
  }
]"""

_DISTILL_USER_FALLBACK = """\
Extract memorable gift-related facts from this conversation:

{conversation}

Return a JSON array of facts:"""

# ---------------------------------------------------------------------------
# Fallback: Recall prompts
# Used to surface relevant memories when the user asks for a recommendation
# ---------------------------------------------------------------------------

_RECALL_SYSTEM_FALLBACK = """\
You are a memory recall assistant for a gift concierge AI.

Your job is to surface the most relevant remembered facts so the agent can
make a personalised gift recommendation from the kapruka.com catalog.

RECALL RULES:
1. Prioritise recipient preferences and dislikes — they directly affect product choice
2. Include budget constraints — filter products outside the user's range
3. Surface upcoming occasions and their dates — affects urgency and category
4. Include past gift history — avoid repeating the same gift
5. Keep total context under 500 tokens
6. Distinguish ST (short-term, this session) from LT (long-term, remembered facts)

OUTPUT FORMAT:
Return a concise formatted memory context ready to inject into the agent prompt."""

_RECALL_USER_FALLBACK = """\
Retrieve and format memories relevant to this gift request:

QUERY: {query}

SHORT-TERM CONTEXT (this conversation):
{st_context}

LONG-TERM FACTS (remembered preferences and history):
{lt_facts}

Format a concise memory context (≤500 tokens) the agent can use to \
recommend the right gift:"""

# ---------------------------------------------------------------------------
# Prompt builders — fetch from LangFuse, fall back to local strings
# ---------------------------------------------------------------------------

def build_distill_prompt(turns: list) -> tuple[str, str]:
    """
    Build the distillation prompt pair (system, user).

    Fetches from LangFuse if available; uses local fallbacks otherwise.
    ``turns`` is a list of ``ConversationTurn`` objects.
    """
    conversation = format_conversation_for_distill(turns)

    system_prompt = fetch_prompt(
        LANGFUSE_PROMPT_NAMES["distill_system"],
        fallback=_DISTILL_SYSTEM_FALLBACK,
    )
    user_prompt = fetch_prompt(
        LANGFUSE_PROMPT_NAMES["distill_user"],
        fallback=_DISTILL_USER_FALLBACK,
        conversation=conversation,
    )
    return system_prompt, user_prompt


def build_recall_prompt(
    query: str,
    st_turns: list,
    lt_facts: list,
) -> tuple[str, str]:
    """
    Build the recall prompt pair (system, user).

    Fetches from LangFuse if available; uses local fallbacks otherwise.
    ``st_turns`` is a list of ``ConversationTurn`` objects.
    ``lt_facts`` is a list of objects with ``.text``, ``.tags``, ``.score``.
    """
    st_context = format_st_context(st_turns)
    lt_context = format_lt_facts(lt_facts)

    system_prompt = fetch_prompt(
        LANGFUSE_PROMPT_NAMES["recall_system"],
        fallback=_RECALL_SYSTEM_FALLBACK,
    )
    user_prompt = fetch_prompt(
        LANGFUSE_PROMPT_NAMES["recall_user"],
        fallback=_RECALL_USER_FALLBACK,
        query=query,
        st_context=st_context,
        lt_facts=lt_context,
    )
    return system_prompt, user_prompt


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_conversation_for_distill(turns: list) -> str:
    """Format ConversationTurn list into a plain dialogue string."""
    lines = []
    for turn in turns:
        role = turn.role.capitalize()
        lines.append(f"{role}: {turn.content}")
    return "\n".join(lines)


def format_st_context(turns: list) -> str:
    """Format recent turns for injection into the recall prompt."""
    if not turns:
        return "(No recent context)"
    lines = []
    for turn in turns:
        role = turn.role.capitalize()
        content = (
            turn.content[:200] + "..." if len(turn.content) > 200 else turn.content
        )
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def format_lt_facts(facts: list) -> str:
    """
    Format long-term distilled facts for injection into the recall prompt.

    ``facts`` can be any objects with ``.text``, ``.tags``, ``.score`` attrs,
    or plain dicts with the same keys.
    """
    if not facts:
        return "(No long-term facts remembered yet)"
    lines = []
    for i, fact in enumerate(facts, 1):
        if isinstance(fact, dict):
            text  = fact.get("text", "")
            tags  = fact.get("tags", [])
            score = fact.get("score", 0.0)
        else:
            text  = getattr(fact, "text", "")
            tags  = getattr(fact, "tags", [])
            score = getattr(fact, "score", 0.0)
        tags_str = f"[{', '.join(tags)}]" if tags else ""
        lines.append(f"{i}. {text} {tags_str} (score: {score:.2f})")
    return "\n".join(lines)


def format_recipient_profiles(profiles: list) -> str:
    """
    Format a list of RecipientProfile objects for agent context injection.

    Produces a compact summary of everyone the user has profiles for.
    """
    if not profiles:
        return "(No recipient profiles saved yet)"
    lines = []
    for p in profiles:
        lines.append(f"• {p.preference_summary()}")
        if p.upcoming_occasions:
            for occ in p.upcoming_occasions:
                lines.append(f"  ↳ {occ.occasion} on {occ.date}")
    return "\n".join(lines)
