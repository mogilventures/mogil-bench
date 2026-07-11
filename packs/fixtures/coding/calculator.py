def clamp(value: int, lower: int, upper: int) -> int:
    """Clamp value to the inclusive range, but this fixture contains a defect."""
    return min(lower, max(value, upper))
