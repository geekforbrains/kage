"""Multi-question AskUserQuestion: tab-bar detection and per-question extraction."""
from kage.backends.claude import ClaudeBackend
from tests.conftest import load_fixture

B = ClaudeBackend()


def fx(name):
    return load_fixture("claude", name)


def test_is_multi_question_true_on_tab_bar():
    assert B.is_multi_question(fx("multi_question_q1")) is True


def test_is_multi_question_true_on_submit_step():
    assert B.is_multi_question(fx("multi_question_submit")) is True


def test_is_multi_question_false_on_plain_menus():
    # ordinary single-question / permission / plan menus have no checkbox tab bar
    assert B.is_multi_question(fx("askuserquestion_menu")) is False
    assert B.is_multi_question(fx("permission_menu")) is False
    assert B.is_multi_question(fx("idle_prompt")) is False


def test_extract_menu_gets_current_question_in_tab_ui():
    menu = B.extract_menu(fx("multi_question_q1"))
    assert menu is not None
    assert "indentation" in menu.question.lower()
    assert menu.options[:2] == ["Tabs", "Spaces"]


def test_submit_step_is_auto_submittable():
    menu = B.extract_menu(fx("multi_question_submit"))
    assert B.auto_submit_option(menu) == 1  # "Submit answers"
