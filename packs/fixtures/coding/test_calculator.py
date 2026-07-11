from calculator import clamp


def test_clamp_inside_range() -> None:
    assert clamp(5, 0, 10) == 5
