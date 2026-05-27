"""Detecting AskUserQuestion's 'Ready to submit your answers?' confirmation."""
from kage.backends import Menu
from kage.backends.claude import ClaudeBackend

B = ClaudeBackend()


def test_submit_confirmation_returns_submit_index():
    m = Menu(question="Ready to submit your answers?",
             options=["Submit answers", "Cancel"])
    assert B.auto_submit_option(m) == 1


def test_submit_index_when_not_first():
    m = Menu(question="Ready to submit your answers?",
             options=["Cancel", "Submit answers"])
    assert B.auto_submit_option(m) == 2


def test_regular_question_is_not_auto_submitted():
    m = Menu(question="What area do you want me to research?",
             options=["Agent Skills", "Prompt engineering", "Type something."])
    assert B.auto_submit_option(m) is None


def test_permission_menu_is_not_auto_submitted():
    m = Menu(question="Do you want to create hello.txt?",
             options=["Yes", "Yes, allow all edits", "No"])
    assert B.auto_submit_option(m) is None


def test_match_is_case_and_whitespace_tolerant():
    m = Menu(question="  ready to SUBMIT your answers?  ",
             options=["  Submit Answers  ", "Cancel"])
    assert B.auto_submit_option(m) == 1


def test_no_submit_option_returns_none():
    m = Menu(question="Ready to submit your answers?",
             options=["Cancel", "Go back"])
    assert B.auto_submit_option(m) is None
