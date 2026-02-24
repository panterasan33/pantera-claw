import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from app.bot.handlers import parse_search_query
from app.services.search_service import build_question_answer
from app.web.api import build_task_list_query, build_inbox_list_query
from app.models.task import TaskStatus


def test_parse_search_query_joins_args():
    assert parse_search_query(["hello", "world"]) == "hello world"
    assert parse_search_query([]) == ""


def test_build_question_answer_with_results():
    answer = build_question_answer(
        "what is due",
        [
            {"type": "task", "content": "Submit taxes before Friday"},
            {"type": "reminder", "content": "Call dentist tomorrow"},
        ],
    )
    assert "what is due" in answer
    assert "Submit taxes" in answer
    assert "Reminder" in answer


def test_build_question_answer_empty_results():
    answer = build_question_answer("query", [])
    assert "couldn't find" in answer.lower()


def test_build_task_query_default_excludes_subtasks():
    query = build_task_list_query(status=None, parent_id=None, my_day=None)
    sql = str(query)
    assert "tasks.parent_id IS NULL" in sql


def test_build_task_query_with_parent_id_targets_children():
    query = build_task_list_query(status=TaskStatus.NOT_STARTED, parent_id=42, my_day=True)
    sql = str(query)
    assert "tasks.parent_id =" in sql
    assert "tasks.my_day = true" in sql.lower()


def test_build_inbox_query_without_filter_returns_all_items():
    query = build_inbox_list_query(is_processed=None)
    sql = str(query)
    assert "WHERE inbox_items.is_processed" not in sql


def test_build_inbox_query_with_filter_applies_status_condition():
    query = build_inbox_list_query(is_processed=False)
    sql = str(query)
    assert "WHERE inbox_items.is_processed" in sql
