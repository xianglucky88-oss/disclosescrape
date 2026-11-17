import pytest

def test_subject_map_is_open_gap():
    from src.disclosescrape import subject_map
    with pytest.raises(NotImplementedError):
        subject_map.process({"id": "x"})

def test_period_align_is_open_gap():
    from src.disclosescrape import period_align
    with pytest.raises(NotImplementedError):
        period_align.process({"id": "x"})
