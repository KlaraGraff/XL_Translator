"""The 翻译 phase folds every model-waiting step into one progress value."""

from core.phase_progress import PhaseProgress


def _tracker(sent):
    return PhaseProgress(
        sent.append,
        phase_index=2,
        phase_total=3,
        phase_name="翻译",
        steps=(("detect", 5), ("batches", 80), ("repair", 10), ("names", 5)),
    )


def _percents(sent):
    return [100 * msg.step_done / msg.step_total for msg in sent]


def test_each_step_moves_inside_its_own_share():
    sent = []
    progress = _tracker(sent)
    progress.report("detect", 1, 2)
    progress.report("batches", 0, 10)
    progress.report("batches", 5, 10)
    progress.report("repair", 3, 6)
    progress.report("names", 1, 1)
    assert _percents(sent) == [2.5, 5.0, 45.0, 90.0, 100.0]
    assert {(msg.phase_index, msg.phase_total, msg.phase_name) for msg in sent} == {(2, 3, "翻译")}


def test_a_skipped_step_is_a_small_jump_and_progress_never_goes_back():
    sent = []
    progress = _tracker(sent)
    progress.report("batches", 10, 10)
    # A late batch callback from another thread must not pull the bar back.
    progress.report("batches", 7, 10)
    progress.report("names", 0, 1)
    progress.report("repair", 1, 2)  # out of order: already past it
    progress.finish()
    assert _percents(sent) == [85.0, 95.0, 100.0]


def test_repeats_and_unknown_steps_send_nothing():
    sent = []
    progress = _tracker(sent)
    progress.report("recovery", 1, 2)
    progress.report("batches", 0, 4)
    progress.report("batches", 0, 4)
    assert _percents(sent) == [5.0]
