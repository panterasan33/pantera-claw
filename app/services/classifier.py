"""
Classification engine for incoming messages.
Classifies into: task, reminder, memory, note, disclosure, question
"""
import json
import re
from enum import Enum
from typing import Optional
from dataclasses import dataclass
from datetime import datetime
import anthropic
import openai

from app.config import get_settings


class MessageType(str, Enum):
    TASK = "task"
    REMINDER = "reminder"
    MEMORY = "memory"  # Long-horizon items (MOT, birthdays, etc.)
    NOTE = "note"
    DISCLOSURE = "disclosure"  # Personal info mentioned in passing
    QUESTION = "question"
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


CLASSIFICATION_PROMPT = """You are Pantera's classification engine. Analyze the user's message and classify it.

Classifications:
- TASK: Something that needs to be done (has action, optionally with deadline)
- REMINDER: Time-triggered alert that needs acknowledgement ("remind me to...", "don't let me forget...")
- MEMORY: Long-horizon recurring event (birthdays, MOT, insurance renewals, anniversaries)
- NOTE: Information worth saving but not immediately actionable
- DISCLOSURE: Personal information mentioned in passing (preferences, relationships, plans)
- QUESTION: User wants information or to discuss something
- CONVERSATION: General chat, not actionable

For each classification, extract relevant structured data.

Respond in JSON format:
{
    "type": "task|reminder|memory|note|disclosure|question|conversation",
    "confidence": 0.0-1.0,
    "data": {
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
    },
    "reasoning": "brief explanation of classification"
}

Current date/time: {current_time}

User message:
{message}"""


class ClassificationService:
    def __init__(self):
        self.settings = get_settings()
        self.anthropic_client = None
        self.openai_client = None
        
        if self.settings.anthropic_api_key:
            self.anthropic_client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key
            )
        if self.settings.openai_api_key:
            self.openai_client = openai.OpenAI(
                api_key=self.settings.openai_api_key
            )
    
    async def classify(self, message: str) -> ClassificationResult:
        """Classify an incoming message."""
        prompt = CLASSIFICATION_PROMPT.format(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            message=message
        )
        
        # Try Anthropic first, fall back to OpenAI
        if self.anthropic_client:
            response = await self._classify_anthropic(prompt)
        elif self.openai_client:
            response = await self._classify_openai(prompt)
        else:
            # Fallback to simple rule-based classification
            response = self._classify_rules(message)
        
        return response
    
    async def _classify_anthropic(self, prompt: str) -> ClassificationResult:
        """Classify using Claude."""
        response = self.anthropic_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return self._parse_response(response.content[0].text)
    
    async def _classify_openai(self, prompt: str) -> ClassificationResult:
        """Classify using GPT."""
        response = self.openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        return self._parse_response(response.choices[0].message.content)
    
    def _classify_rules(self, message: str) -> ClassificationResult:
        """Simple rule-based fallback classification."""
        message_lower = message.lower()
        
        # Reminder patterns
        reminder_patterns = [
            r"remind me",
            r"don't let me forget",
            r"alert me",
            r"notify me"
        ]
        for pattern in reminder_patterns:
            if re.search(pattern, message_lower):
                return ClassificationResult(
                    message_type=MessageType.REMINDER,
                    confidence=0.8,
                    extracted_data={"content": message}
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
    
    def _parse_response(self, response_text: str) -> ClassificationResult:
        """Parse LLM response into ClassificationResult."""
        try:
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response_text)
            
            return ClassificationResult(
                message_type=MessageType(data.get("type", "note")),
                confidence=float(data.get("confidence", 0.5)),
                extracted_data=data.get("data", {})
            )
        except (json.JSONDecodeError, ValueError) as e:
            # Fallback
            return ClassificationResult(
                message_type=MessageType.NOTE,
                confidence=0.3,
                extracted_data={"content": response_text, "parse_error": str(e)}
            )


# Singleton instance
_classifier: Optional[ClassificationService] = None


def get_classifier() -> ClassificationService:
    global _classifier
    if _classifier is None:
        _classifier = ClassificationService()
    return _classifier
