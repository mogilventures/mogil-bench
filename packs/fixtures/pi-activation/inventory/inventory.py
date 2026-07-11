def summarize(rows: list[dict[str, object]]) -> dict[str, int]:
    """Sum fictional inventory quantities by warehouse."""
    return {str(row["warehouse"]): int(row["quantity"]) for row in rows}
