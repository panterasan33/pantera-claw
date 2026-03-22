"""
Classification engine for incoming messages.
Classifies into: task, reminder, memory, note, disclosure, question,
correction, update, conversation
"""
import json
import re
from collections import defaultdict
from enum import Enum
from typing import Optional
from dataclasses import dataclass
from datetime import datetime
import anthropic
import openai

from app.config import get_settings
from app.services.classifier_learning import get_learning_service
from app.services.llm_usage_service import record_from_anthropic_message, record_from_openai_chat


class MessageType(str, Enum):
    TASK = "task"
    REMINDER = "reminder"
    MEMORY = "memory"  # Long-horizon items (MOT, birthdays, etc.)
    NOTE = "note"
    DISCLOSURE = "disclosure"  # Personal info mentioned in passing
    QUESTION = "question"
    CORRECTION = "correction"  # Reclassify the previously captured item
    UPDATE = "update"          # Modify a field of the previously captured item
    CONVERSATION = "conversation"


@dataclass
class ClassificationResult:
    message_type: MessageType
    confidence: float
    extracted_data: dict
    # Extracted fields based on type:
    # task: {title, notes, due_date, project, group}
    # reminder: {content, trigger_time, is_recurring, recurrence_pattern}
    # memory: {content, event_date, memory_type, lead_times}
    # note: {content, tags}
    # disclosure: {summary, original}
    # question: {query}
    # correction: {new_type, original_hint}
    # update: {field, new_value, original_hint}


CLASSIFICATION_PROMPT = """You are Pantera's classification engine. Analyze the user's message and classify it.

{history_block}
Classifications:
- TASK: Something that needs to be done (has action, optionally with deadline)
- REMINDER: Time-triggered alert that needs acknowledgement ("remind me to...", "don't let me forget...")
- MEMORY: Long-horizon recurring event (birthdays, MOT, insurance renewals, anniversaries)
- NOTE: Information worth saving but not immediately actionable
- DISCLOSURE: Personal information mentioned in passing (preferences, relationships, plans)
- QUESTION: User wants information about something already stored or a general query
- CORRECTION: User wants to reclassify the most recently captured item (e.g. "make that a reminder", "actually it's a task", "I meant a note")
- UPDATE: User wants to modify a field of the most recently captured item (e.g. "set it for Friday", "change the title to X", "due tomorrow", "actually 3pm")
- CONVERSATION: General chat, greetings, thanks — not actionable

Use CORRECTION when the user indicates the previous classification was wrong.
Use UPDATE when the user provides new/corrected details for the previous item (date, time, title, etc.) without changing its type.
If the message references "that", "it", "the last one" and recent context shows a captured item, strongly prefer CORRECTION or UPDATE.

For each classification, extract relevant structured data.

Respond in JSON format:
{{
    "type": "task|reminder|memory|note|disclosure|question|correction|update|conversation",
    "confidence": 0.0-1.0,
    "data": {{
        // For TASK:
        "title": "string",
        "notes": "string or null",
        "due_date": "natural language date or null",
        "project": "inferred project or null",
        "group": "inferred group/tag or null"

        // For REMINDER:
        "content": "what to remind about",
        "trigger_time": "natural language time",
        "is_recurring": true/false,
        "recurrence_pattern": "daily|weekly|monthly|yearly or null",
        "recurrence_detail": "e.g., 'every Tuesday' or null"

        // For MEMORY:
        "content": "description",
        "event_date": "date or null",
        "is_annual": true/false,
        "memory_subtype": "birthday|annual_event|note"

        // For NOTE:
        "content": "the note content",
        "tags": ["relevant", "tags"]

        // For DISCLOSURE:
        "summary": "clean factual summary",
        "category": "preference|relationship|plan|personal"

        // For QUESTION:
        "query": "what they're asking"

        // For CORRECTION:
        "new_type": "task|reminder|memory|note",
        "original_hint": "a short phrase from the original message or 'last item'"

        // For UPDATE:
        "field": "due_date|title|content|time|project|notes",
        "new_value": "the new value as a string",
        "original_hint": "a short phrase identifying which item"
    }},
    "reasoning": "brief explanation of classification"
}}

Current date/time: {current_time}

User message:
{message}"""


def _build_history_block(conversation_history: list[dict]) -> str:
    """Format conversation history into a prompt block."""
    if not conversation_history:
        return ""
    lines = ["Recent conversation context (use to resolve references like 'that', 'it', 'the last one'):"]
    for turn in conversation_history:
        role_label = "User" if turn["role"] == "user" else "Bot"
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        item_note = ""
        if turn["role"] == "user" and turn.get("item_type"):
            item_note = f" [saved as {turn['item_type']}]"
        lines.append(f"  [{role_label}]{item_note}: {text[:200]}")
    lines.append("")  # blank line before main prompt
    return "\n".join(lines) + "\n"


class ClassificationService:
    def __init__(self):
        self.settings = get_settings()
        self.anthropic_client = None
        self.openai_client = None

        # Try to initialize API clients, fail gracefully
        if self.settings.anthropic_api_key:
            try:
                self.anthropic_client = anthropic.AsyncAnthropic(
                    api_key=self.settings.anthropic_api_key
                )
            except Exception as e:
                print(f"Failed to init Anthropic client: {e}")

        if self.settings.openai_api_key:
            try:
                self.openai_client = openai.AsyncOpenAI(
                    api_key=self.settings.openai_api_key
                )
            except Exception as e:
                print(f"Failed to init OpenAI client: {e}")

    async def classify(
        self,
        message: str,
        conversation_history: Optional[list[dict]] = None,
    ) -> ClassificationResult:
        """Classify an incoming message, optionally using conversation history for context."""
        learning_hints = get_learning_service().build_prompt_hints()
        history_block = _build_history_block(conversation_history or [])

        # Escape braces in user message so they don't break str.format()
        safe_message = message.replace("{", "{{").replace("}", "}}")
        prompt = CLASSIFICATION_PROMPT.format(
            history_block=history_block,
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            message=safe_message,
        )
        if learning_hints:
            prompt += f"\n\nAdditional guidance from past corrections:\n{learning_hints}"

        # Try Anthropic first, fall back to OpenAI, then rules
        try:
            if self.anthropic_client:
                return await self._classify_anthropic(prompt)
            elif self.openai_client:
                return await self._classify_openai(prompt)
        except Exception as e:
            print(f"LLM classification failed: {e}")

        # Fallback to simple rule-based classification
        return self._classify_rules(message)

    async def _classify_anthropic(self, prompt: str) -> ClassificationResult:
        """Classify using Claude."""
        model_id = "claude-3-haiku-20240307"
        response = await self.anthropic_client.messages.create(
            model=model_id,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        await record_from_anthropic_message(model=model_id, operation="classification", response=response)
        return self._parse_response(response.content[0].text)

    async def _classify_openai(self, prompt: str) -> ClassificationResult:
        """Classify using GPT."""
        model_id = "gpt-4o-mini"
        response = await self.openai_client.chat.completions.create(
            model=model_id,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        await record_from_openai_chat(model=model_id, operation="classification", response=response)
        return self._parse_response(response.choices[0].message.content)

    def _classify_rules(self, message: str) -> ClassificationResult:
        """Simple rule-based fallback classification."""
        message_lower = message.lower()

        # Explicit syntax support (highest precedence)
        explicit_prefixes = {
            "task:": MessageType.TASK,
            "todo:": MessageType.TASK,
            "to-do:": MessageType.TASK,
            "reminder:": MessageType.REMINDER,
            "memory:": MessageType.MEMORY,
            "note:": MessageType.NOTE,
            "question:": MessageType.QUESTION,
        }
        for prefix, msg_type in explicit_prefixes.items():
            if message_lower.startswith(prefix):
                clean = message[len(prefix):].strip() or message
                return self._build_explicit_result(msg_type, clean)

        # Correction patterns
        correction_patterns = [
            r"\bactually\b.*(task|reminder|note|memory)\b",
            r"\bmake that a\b",
            r"\bchange that to\b",
            r"\bi meant\b",
            r"\bwrong type\b",
            r"\breclassify\b",
        ]
        for pattern in correction_patterns:
            if re.search(pattern, message_lower):
                # Extract new_type if mentioned
                for t in ("task", "reminder", "note", "memory"):
                    if t in message_lower:
                        return ClassificationResult(
                            message_type=MessageType.CORRECTION,
                            confidence=0.82,
                            extracted_data={"new_type": t, "original_hint": "last item"},
                        )
                return ClassificationResult(
                    message_type=MessageType.CORRECTION,
                    confidence=0.72,
                    extracted_data={"new_type": None, "original_hint": "last item"},
                )

        # Update patterns
        update_patterns = [
            r"\bchange the (date|title|time|due date|project)\b",
            r"\bset it for\b",
            r"\bdue (tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next)\b",
            r"\bupdate the\b",
            r"\bmove it to\b",
        ]
        for pattern in update_patterns:
            if re.search(pattern, message_lower):
                return ClassificationResult(
                    message_type=MessageType.UPDATE,
                    confidence=0.78,
                    extracted_data={"field": "due_date", "new_value": message, "original_hint": "last item"},
                )

        # Adaptive pattern support based on user reclassifications
        learning = get_learning_service()
        adaptive_patterns = learning.get_learned_keywords()
        adaptive_scores: dict[MessageType, int] = defaultdict(int)
        for raw_type, keywords in adaptive_patterns.items():
            try:
                msg_type = MessageType(raw_type)
            except ValueError:
                continue
            for keyword in keywords:
                if keyword in message_lower:
                    adaptive_scores[msg_type] += 1

        if adaptive_scores:
            best_type = max(adaptive_scores, key=adaptive_scores.get)
            if adaptive_scores[best_type] >= 2:
                return self._build_explicit_result(best_type, message, confidence=0.82)

        # Memory patterns (prefer these over generic reminder patterns)
        if any(token in message_lower for token in ("birthday", "anniversary", "every year", "yearly")):
            event_date = "today" if "today" in message_lower else None
            subtype = "birthday" if "birthday" in message_lower else "annual_event"
            return ClassificationResult(
                message_type=MessageType.MEMORY,
                confidence=0.75,
                extracted_data={
                    "content": message,
                    "event_date": event_date,
                    "is_annual": True,
                    "memory_subtype": subtype,
                },
            )

        # Reminder patterns
        reminder_patterns = [
            r"remind me",
            r"don't let me forget",
            r"alert me",
            r"notify me"
        ]
        for pattern in reminder_patterns:
            if re.search(pattern, message_lower):
                recurrence_pattern = None
                if "quarter" in message_lower or "quarterly" in message_lower:
                    recurrence_pattern = "quarterly"
                elif "yearly" in message_lower or "every year" in message_lower or "annual" in message_lower:
                    recurrence_pattern = "yearly"
                return ClassificationResult(
                    message_type=MessageType.REMINDER,
                    confidence=0.8,
                    extracted_data={
                        "content": message,
                        "is_recurring": recurrence_pattern is not None,
                        "recurrence_pattern": recurrence_pattern,
                        "trigger_time": message if recurrence_pattern else None,
                    }
                )

        # Task patterns
        task_patterns = [
            r"^(i need to|i have to|i should|i must|todo|to-do)",
            r"(buy|call|email|send|book|schedule|finish|complete|do)",
        ]
        for pattern in task_patterns:
            if re.search(pattern, message_lower):
                return ClassificationResult(
                    message_type=MessageType.TASK,
                    confidence=0.6,
                    extracted_data={"title": message}
                )

        # Question patterns
        if message.strip().endswith("?") or message_lower.startswith(("what", "how", "when", "where", "why", "who", "can you")):
            return ClassificationResult(
                message_type=MessageType.QUESTION,
                confidence=0.7,
                extracted_data={"query": message}
            )

        # Default to note
        return ClassificationResult(
            message_type=MessageType.NOTE,
            confidence=0.5,
            extracted_data={"content": message}
        )

    def _build_explicit_result(self, message_type: MessageType, message: str, confidence: float = 0.92) -> ClassificationResult:
        """Build structured data for explicit or learned type matches."""
        if message_type == MessageType.TASK:
            return ClassificationResult(message_type=message_type, confidence=confidence, extracted_data={"title": message})
        if message_type == MessageType.REMINDER:
            return ClassificationResult(
                message_type=message_type,
                confidence=confidence,
                extracted_data={"content": message, "trigger_time": None, "is_recurring": False, "recurrence_pattern": None},
            )
        if message_type == MessageType.MEMORY:
            return ClassificationResult(
                message_type=message_type,
                confidence=confidence,
                extracted_data={"content": message, "event_date": None, "is_annual": False, "memory_subtype": "note"},
            )
        if message_type == MessageType.QUESTION:
            return ClassificationResult(message_type=message_type, confidence=confidence, extracted_data={"query": message})
        return ClassificationResult(message_type=MessageType.NOTE, confidence=confidence, extracted_data={"content": message})

    def _parse_response(self, response_text: str) -> ClassificationResult:
        """Parse LLM response into ClassificationResult."""
        try:
            data = self._extract_json(response_text)
            message_type = MessageType(data.get("type", "note"))
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))
            extracted_data = data.get("data", {})
            if not isinstance(extracted_data, dict):
                extracted_data = {"raw_data": extracted_data}

            return ClassificationResult(
                message_type=message_type,
                confidence=confidence,
                extracted_data=extracted_data
            )
        except (json.JSONDecodeError, ValueError) as e:
            # Fallback
            return ClassificationResult(
                message_type=MessageType.NOTE,
                confidence=0.3,
                extracted_data={"content": response_text, "parse_error": str(e)}
            )

    def _extract_json(self, response_text: str) -> dict:
        cleaned = response_text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
        if fenced:
            cleaned = fenced.group(1)
        else:
            json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if json_match:
                cleaned = json_match.group()

        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("Classifier response was not a JSON object")
        return data


# Singleton instance
_classifier: Optional[ClassificationService] = None


def get_classifier() -> ClassificationService:
    global _classifier
    if _classifier is None:
        _classifier = ClassificationService()
    return _classifier
