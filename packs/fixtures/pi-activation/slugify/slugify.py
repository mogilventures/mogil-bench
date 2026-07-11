def slugify(value: str) -> str:
    """Return a lowercase hyphen-separated identifier."""
    return value.replace(" ", "-")
