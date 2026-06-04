from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_quiz_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "plugins"
        / "department_training_quiz"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_department_training_quiz_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_numbered_quiz_submission() -> None:
    module = _load_quiz_module()

    submission = module.parse_quiz_submission(
        "部门：市场部；答案：1B 2C 3B 4A 5A；一句话反馈：想让它帮我写客户话术"
    )

    assert submission is not None
    assert submission.department == "市场部"
    assert submission.answers == {1: "B", 2: "C", 3: "B", 4: "A", 5: "A"}
    assert submission.feedback == "想让它帮我写客户话术"


def test_parse_compact_quiz_submission() -> None:
    module = _load_quiz_module()

    submission = module.parse_quiz_submission("部门：影视部；答案：BCBAA")

    assert submission is not None
    assert submission.department == "影视部"
    assert submission.answers == {1: "B", 2: "C", 3: "B", 4: "A", 5: "A"}


def test_incomplete_submission_is_ignored() -> None:
    module = _load_quiz_module()

    assert module.parse_quiz_submission("答案：1B 2C") is None
    assert module.parse_quiz_submission("这个答案怎么写比较好？") is None


def test_parse_training_card_request() -> None:
    module = _load_quiz_module()

    assert module.parse_card_activation("发培训卡片") == "training"
    assert module.parse_card_activation("发全部培训卡片") == "all"
    assert module.parse_card_activation("/training cards") == "all"
    assert module.parse_card_activation("/training quiz") == "quiz"
    assert module.parse_card_activation("/training") == "help"


def test_parse_natural_training_card_request() -> None:
    module = _load_quiz_module()

    assert (
        module.parse_card_activation("我们自己系统的培训，不是已经生成卡片了吗?")
        == "training"
    )
    assert module.parse_card_activation("把部门培训卡发一下") == "training"
    assert module.parse_card_activation("把培训和小测试两张卡都发一下") == "all"
    assert module.parse_card_activation("小测试卡片能不能发出来") == "quiz"
    assert module.parse_card_activation("我现在需要培训") == "training"
    assert module.parse_card_activation("可以开始培训了吗") == "training"
    assert module.parse_card_activation("我们讨论一下培训内容怎么设计") is None


def test_grade_quiz_returns_passed_result() -> None:
    module = _load_quiz_module()
    submission = module.QuizSubmission(
        department="市场部",
        answers={1: "B", 2: "C", 3: "B", 4: "A", 5: "A"},
        feedback="想用来写客户话术",
    )

    result = module.grade_quiz(submission, pass_score=4)

    assert "已通过" in result
    assert "得分：5/5" in result
    assert "破冰任务" in result
    assert "想用来写客户话术" in result


def test_grade_quiz_returns_review_hints_for_wrong_answers() -> None:
    module = _load_quiz_module()
    submission = module.QuizSubmission(
        department="策略部",
        answers={1: "A", 2: "C", 3: "B", 4: "A", 5: "D"},
        feedback="",
    )

    result = module.grade_quiz(submission, pass_score=4)

    assert "未通过" in result
    assert "得分：3/5" in result
    assert "第 1 题（正确答案 B）" in result
    assert "第 5 题（正确答案 A）" in result
    assert "安全提醒" in result
