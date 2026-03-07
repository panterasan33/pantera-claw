from app.services.classifier import ClassificationService, MessageType
from app.services.classifier_learning import ClassifierLearningService


def test_rules_support_explicit_prefixes():
    svc = ClassificationService()

    result_task = svc._classify_rules("Task: finish monthly report")
    assert result_task.message_type == MessageType.TASK
    assert result_task.extracted_data["title"] == "finish monthly report"

    result_note = svc._classify_rules("Note: remember this detail")
    assert result_note.message_type == MessageType.NOTE


def test_learning_review_builds_overrides(tmp_path):
    learner = ClassifierLearningService(storage_path=str(tmp_path / "learning.json"))

    state = learner.review_and_improve_from_feedback(
        [
            {
                "source_text": "ping me tomorrow about payroll",
                "predicted_type": "task",
                "corrected_type": "reminder",
            },
            {
                "source_text": "please ping me about payroll",
                "predicted_type": "task",
                "corrected_type": "reminder",
            },
            {
                "source_text": "book flights for madrid",
                "predicted_type": "note",
                "corrected_type": "task",
            },
            {
                "source_text": "book hotels for madrid",
                "predicted_type": "note",
                "corrected_type": "task",
            },
        ]
    )

    assert "reminder" in state["keyword_overrides"]
    assert "task->reminder" in state["confusions"]


def test_prompt_hints_include_confusions(tmp_path):
    learner = ClassifierLearningService(storage_path=str(tmp_path / "learning.json"))
    learner.review_and_improve_from_feedback(
        [
            {
                "source_text": "remind me tomorrow to email",
                "predicted_type": "task",
                "corrected_type": "reminder",
            },
            {
                "source_text": "please remind me to email",
                "predicted_type": "task",
                "corrected_type": "reminder",
            },
        ]
    )
    hints = learner.build_prompt_hints()
    assert "Prefer reminder" in hints
    assert "task->reminder" in hints
