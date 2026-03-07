"""Self-improving classification helpers driven by user reclassification feedback."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

class ClassifierLearningService:
    """Persists lightweight adaptive classification hints from correction events."""

    def __init__(self, storage_path: str = "app/data/classifier_learning.json"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def get_learning_state(self) -> dict:
        if not self.storage_path.exists():
            return {"keyword_overrides": {}, "confusions": {}}
        try:
            return json.loads(self.storage_path.read_text())
        except json.JSONDecodeError:
            return {"keyword_overrides": {}, "confusions": {}}

    def get_learned_keywords(self) -> dict[str, list[str]]:
        state = self.get_learning_state()
        overrides = state.get("keyword_overrides", {})
        learned: dict[str, list[str]] = {}
        for raw_type, keywords in overrides.items():
            learned[raw_type] = list(keywords)
        return learned

    def review_and_improve_from_feedback(self, feedback_rows: Iterable[dict]) -> dict:
        """Build adaptive keyword hints/confusion matrix from past corrections."""
        keyword_counts: dict[str, Counter[str]] = defaultdict(Counter)
        confusion_counts: Counter[str] = Counter()

        for row in feedback_rows:
            corrected_type = row.get("corrected_type")
            predicted_type = row.get("predicted_type")
            source_text = (row.get("source_text") or "").lower()
            if not corrected_type or not source_text:
                continue

            if predicted_type and predicted_type != corrected_type:
                confusion_counts[f"{predicted_type}->{corrected_type}"] += 1

            tokens = [tok.strip(".,!?()[]{}") for tok in source_text.split()]
            for tok in tokens:
                if len(tok) >= 4 and tok.isascii():
                    keyword_counts[corrected_type][tok] += 1

        keyword_overrides: dict[str, list[str]] = {}
        for corrected_type, counts in keyword_counts.items():
            top_keywords = [word for word, count in counts.most_common(8) if count >= 2]
            if top_keywords:
                keyword_overrides[corrected_type] = top_keywords

        new_state = {
            "keyword_overrides": keyword_overrides,
            "confusions": dict(confusion_counts.most_common(20)),
        }
        self.storage_path.write_text(json.dumps(new_state, indent=2, sort_keys=True))
        return new_state

    def build_prompt_hints(self) -> str:
        """Return additional prompt hints generated from learned overrides/confusions."""
        state = self.get_learning_state()
        hints: list[str] = []
        keyword_overrides = state.get("keyword_overrides", {})
        for mtype, words in keyword_overrides.items():
            if words:
                hints.append(f"- Prefer {mtype} when message includes: {', '.join(words[:5])}.")

        confusions = state.get("confusions", {})
        if confusions:
            top = sorted(confusions.items(), key=lambda item: item[1], reverse=True)[:5]
            hints.append("Common mistakes to avoid:")
            hints.extend([f"- {name} ({count})" for name, count in top])

        return "\n".join(hints)


_learning_service: ClassifierLearningService | None = None


def get_learning_service() -> ClassifierLearningService:
    global _learning_service
    if _learning_service is None:
        _learning_service = ClassifierLearningService()
    return _learning_service
