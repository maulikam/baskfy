"""PM0.12: reading an action's subject and deciding whether it explains a one-day move."""

import datetime as dt

from portfolioml import action_audit as A


def test_subjects_give_the_share_factor():
    assert A.action_terms("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == [("split", 5.0)]
    assert A.action_terms("Bonus 1:1") == [("bonus", 2.0)]
    assert A.action_terms("Bonus 3 : 2") == [("bonus", 2.5)]
    assert A.action_terms("Consolidation Of Shares From Rs 1/- To Rs 10/-") == [("consolidation", 0.1)]
    assert A.action_terms("Scheme Of Demerger") == [("demerger", None)]


def test_debenture_bonus_and_rights_are_not_share_actions():
    assert A.action_terms("Scheme Of Arrangement - Bonus Debentures 6:1") == []
    assert A.action_terms("Rights 1:4 @ Premium Rs 10/-") == []


def test_a_combined_bonus_and_split_reads_both():
    assert A.action_terms("Bonus 1:1 and Face Value Split From Rs 10 To Rs 2") == [("bonus", 2.0), ("split", 5.0)]


def test_step_must_fit_the_action():
    assert A.classify(0.21, [("split", 5.0)]) == ("split", 0.2)
    assert A.classify(0.21, [("split", 2.0)]) == ("unexplained", None)


def test_consolidation_fits_a_jump():
    assert A.classify(9.5, [("consolidation", 0.1)]) == ("consolidation", 10.0)


def test_combined_actions_are_tried_together():
    name, multiplier = A.classify(0.1, [("split", 5.0), ("bonus", 2.0)])
    assert name == "split" and multiplier == 0.1


def test_demerger_only_explains_a_fall():
    assert A.classify(0.3, [("demerger", None)]) == ("demerger", None)
    assert A.classify(3.0, [("demerger", None)]) == ("unexplained", None)


def test_ex_date_must_lie_between_the_bars():
    assert A.in_window(dt.date(2024, 1, 10), dt.date(2024, 1, 9), dt.date(2024, 1, 10))
    assert not A.in_window(dt.date(2023, 1, 10), dt.date(2024, 1, 9), dt.date(2024, 1, 10))


def test_a_fall_needs_a_close_fit_and_a_real_factor():
    assert A.classify_fall(0.51, [("bonus", 2.0)]) == ("bonus", 0.5)
    assert A.classify_fall(0.7, [("bonus", 2.0)]) is None
    assert A.classify_fall(0.79, [("split", 1.1)]) is None
    assert A.classify_fall(0.5, [("demerger", None)]) is None
    assert A.classify_fall(0.3, [("bonus", 3.0)]) is None
