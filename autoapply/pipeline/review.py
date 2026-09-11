"""Review queue rendering.

`aa review` prints a ranked, filterable table of the current state —
the human-facing output of Step 1 and the feed for Step 2's applier.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from sqlmodel import select

from ..db import Listing, ListingStatus, get_session


def _row_status(row: dict) -> str:
    status = row.get("status", "")
    if row.get("needs_manual"):
        return "manual"
    return status


def render_review(
    status: str | None = None,
    min_score: float | None = None,
    apply_path: str | None = None,
    source: str | None = None,
) -> None:
    db = get_session()
    query = select(Listing)
    if source:
        query = query.where(Listing.source == source)
    if status:
        query = query.where(Listing.status == status)
    rows = db.exec(query).all()

    filtered = []
    for listing in rows:
        if min_score is not None and (listing.match_score or 0.0) < min_score:
            continue
        if apply_path and listing.apply_path != apply_path:
            continue
        filtered.append(
            {
                "id": listing.id,
                "score": listing.match_score
                if listing.match_score is not None
                else 0.0,
                "status": listing.status,
                "title": listing.title,
                "company": listing.company,
                "location": listing.location,
                "salary": listing.salary_raw,
                "apply_path": listing.apply_path,
                "ats": listing.ats_type or "",
                "reason": listing.reason,
                "needs_manual": listing.status == ListingStatus.NEEDS_MANUAL.value,
            }
        )

    filtered.sort(key=lambda r: r["score"], reverse=True)
    console = Console()
    table = Table(title="Application Review Queue", show_lines=False)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Status", no_wrap=True)
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Salary")
    table.add_column("Apply")
    table.add_column("ATS")
    table.add_column("Why")

    for row in filtered:
        table.add_row(
            str(row["id"]),
            f"{row['score']:.1f}",
            _row_status(row),
            row["title"][:42],
            row["company"][:26],
            row["location"][:22],
            (row["salary"] or "")[:18],
            row["apply_path"],
            row["ats"],
            row["reason"][:52],
        )

    console.print(table)
    console.print(
        f"[dim]{len(filtered)} listing(s) shown."
        f" Filters: status={status or 'any'} min_score={min_score or 'any'} apply_path={apply_path or 'any'}[/dim]"
    )
