import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .db import connect, init_db

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

app = typer.Typer(
    help="Semantic triage tool for the Koha community Bugzilla.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def harvest(
    years_back: int = typer.Option(5, "--years", help="How many years back to fetch (first run only)."),
) -> None:
    """Fetch bugs and comments from Koha Bugzilla into the local SQLite database."""
    from .harvest import harvest as run_harvest

    def on_page(page: int, count: int) -> None:
        console.print(f"  bugs page {page}: {count} records", style="dim")

    def on_comments(done: int, total: int) -> None:
        console.print(f"  comments: {done}/{total} bugs processed", style="dim")

    console.print("[cyan]Harvesting Koha Bugzilla...[/cyan]")
    counts = run_harvest(settings.db_path, years_back=years_back, on_page=on_page, on_comments=on_comments)
    console.print(
        f"  {counts['bugs']} bugs ({counts['new_bugs']} new, {counts['updated_bugs']} updated), "
        f"{counts['comments']} comments"
    )
    console.print("[green]Done.[/green]")


@app.command()
def backfill(
    batch_size: int = typer.Option(100, "--batch-size", help="Bugs per batch."),
    delay: float = typer.Option(2.0, "--delay", help="Seconds to pause between batches."),
) -> None:
    """Backfill descriptions and comments for bugs that don't have them yet.

    Processes in small batches with pauses to avoid overwhelming Bugzilla.
    Safe to interrupt — progress is saved after each batch.
    """
    from .harvest import backfill_comments

    def on_progress(done: int, total: int, comments: int) -> None:
        if done % 10 == 0 or done == total:
            console.print(f"  {done}/{total} bugs processed, {comments} comments saved", style="dim")

    console.print(f"[cyan]Backfilling comments (batch_size={batch_size}, delay={delay}s)...[/cyan]")
    counts = backfill_comments(settings.db_path, batch_size=batch_size, delay=delay, on_progress=on_progress)
    console.print(
        f"  Processed {counts['processed']} bugs in {counts['batches']} batches, "
        f"{counts['comments']} comments saved"
    )
    if counts["failed"]:
        console.print(f"  [yellow]{counts['failed']} bugs failed — re-run to retry[/yellow]")
    console.print("[green]Done.[/green]")


@app.command()
def status() -> None:
    """Show current harvest state."""
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM bugs").fetchone()[0]
        open_bugs = conn.execute("SELECT COUNT(*) FROM bugs WHERE status NOT IN ('RESOLVED','VERIFIED','CLOSED')").fetchone()[0]
        embedded = conn.execute("SELECT COUNT(*) FROM bugs WHERE embedding IS NOT NULL").fetchone()[0]
        comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        harvest = conn.execute("SELECT * FROM harvest_state WHERE id = 1").fetchone()

        components = conn.execute(
            "SELECT component, COUNT(*) as cnt FROM bugs GROUP BY component ORDER BY cnt DESC LIMIT 15"
        ).fetchall()

    table = Table(title="koha-triage status")
    table.add_column("Metric")
    table.add_column("Value", justify="right")

    table.add_row("Total bugs", str(total))
    table.add_row("Open bugs", str(open_bugs))
    table.add_row("Comments", str(comments))
    table.add_row("Embedded", str(embedded))
    table.add_row("Last harvested", (harvest["last_harvested_at"] if harvest else "never"))

    console.print(table)

    if components:
        comp_table = Table(title="Top components")
        comp_table.add_column("Component")
        comp_table.add_column("Bugs", justify="right")
        for c in components:
            comp_table.add_row(c["component"], str(c["cnt"]))
        console.print(comp_table)


@app.command()
def serve(
    host: str = typer.Option(DEFAULT_HOST, "--host", help="Interface to bind to."),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload for development."),
) -> None:
    """Run the web dashboard."""
    import uvicorn

    init_db(settings.db_path)
    console.print(f"[cyan]koha-triage serving on http://{host}:{port}[/cyan]")
    uvicorn.run(
        "koha_triage.web:app",
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def embed(
    batch_size: int = typer.Option(32, "--batch-size", help="Texts per embedding batch."),
    chunk_size: int = typer.Option(500, "--chunk-size", help="Bugs per chunk (saves after each chunk)."),
) -> None:
    """Compute embeddings for bugs whose summary/description changed since the last run."""
    from .embed import embed_pending

    def on_progress(stage: str, payload) -> None:
        if stage == "loading_model":
            console.print(f"[cyan]Loading embedding model {payload}...[/cyan]")
        elif stage == "embedding":
            console.print(f"[cyan]Embedding {payload} bugs in chunks of {chunk_size}...[/cyan]")
        elif stage == "chunk_done":
            console.print(f"  [dim]saved {payload}[/dim]")

    counts = embed_pending(settings.db_path, settings.embedding_model, batch_size, chunk_size=chunk_size, on_progress=on_progress)
    console.print(
        f"[green]Embedded {counts['embedded']} / {counts['total']}  "
        f"(skipped {counts['skipped']} unchanged)[/green]"
    )


@app.command()
def search(
    query: str = typer.Argument(..., help="Problem description to search for."),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of matches to return."),
) -> None:
    """Rank harvested bugs by semantic similarity to the query."""
    from .search import NoEmbeddingsError, search as semantic_search

    try:
        results = semantic_search(settings.db_path, query, settings.embedding_model, top_k=top_k)
    except NoEmbeddingsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Top {len(results)} matches for: {query!r}")
    table.add_column("Score", justify="right")
    table.add_column("Bug", justify="right")
    table.add_column("Status")
    table.add_column("Component")
    table.add_column("Summary")

    for r in results:
        status_str = r["status"]
        if r["resolution"]:
            status_str += f" ({r['resolution']})"
        table.add_row(
            f"{r['score']:.3f}",
            str(r["bug_id"]),
            status_str,
            r["component"],
            r["summary"],
        )
    console.print(table)


@app.command()
def classify(
    query: str = typer.Argument(..., help="Problem description to classify against."),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of matches to classify."),
) -> None:
    """Semantic search plus a Claude-generated verdict per match."""
    from .classify import classify as run_classify
    from .search import NoEmbeddingsError

    if not settings.anthropic_api_key:
        console.print("[red]KOHA_TRIAGE_ANTHROPIC_API_KEY is not set.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Classifying top {top_k} matches with {settings.classification_model}...[/cyan]")
    try:
        results, verdicts = run_classify(
            settings.db_path, query, settings.embedding_model,
            settings.anthropic_api_key, settings.classification_model, top_k=top_k,
        )
    except NoEmbeddingsError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    if not results:
        console.print("[yellow]No matches found.[/yellow]")
        return

    verdicts_by_idx = {i: v for i, v in enumerate(verdicts) if i < len(results)}
    for i, r in enumerate(results):
        console.print()
        status_str = r["status"]
        if r["resolution"]:
            status_str += f" ({r['resolution']})"
        console.print(
            f"[bold cyan]Bug {r['bug_id']}[/bold cyan] "
            f"[dim]({status_str}, {r['component']}, score {r['score']:.3f})[/dim]"
        )
        console.print(f"  [bold]{r['summary']}[/bold]")
        v = verdicts_by_idx.get(i)
        if v is not None:
            console.print(f"  Verdict:   [yellow]{v.verdict}[/yellow]")
            console.print(f"  Why:       {v.rationale}")
            console.print(f"  Suggested: {v.suggested_action}")
        else:
            console.print("  [dim](no verdict returned)[/dim]")
        console.print(f"  {r['url']}")


if __name__ == "__main__":
    app()
