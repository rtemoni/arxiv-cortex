from __future__ import annotations

import json
from pathlib import Path

import click
from flask import Flask, current_app

from arxiv_cortex.db import get_db, run_migrations
from arxiv_cortex.services.groups import PaperGroupService
from arxiv_cortex.services.imports import PaperImportError, PaperImportService
from arxiv_cortex.services.papers import PaperService


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        run_migrations(current_app.config["DATABASE"])
        click.echo("Database migrations applied.")

    @app.cli.command("sync")
    def sync_command() -> None:
        run_id = current_app.extensions["job_manager"].run_sync_inline("cli")
        click.echo(f"Synchronization run {run_id} finished.")

    @app.cli.command("reindex")
    @click.option("--reset", is_flag=True, help="Delete active-model vectors before indexing.")
    def reindex_command(reset: bool) -> None:
        service = current_app.extensions["embedding_service"]
        if reset:
            deleted = service.reset_active_model()
            click.echo(f"Removed {deleted} existing vectors.")
        generated = service.index_pending()
        click.echo(f"Generated {generated} embeddings.")

    @app.cli.command("import-papers")
    @click.option(
        "--manifest",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        required=True,
        help="JSON list of paper URLs or curated paper metadata.",
    )
    @click.option("--group", required=True, help="Library group to create or reuse.")
    @click.option("--embed/--no-embed", default=True, help="Index imported papers.")
    def import_papers_command(manifest: Path, group: str, embed: bool) -> None:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise click.ClickException(f"Could not read manifest: {error}") from error
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise click.ClickException("The manifest must be a JSON list of paper objects")

        connection = get_db()
        importer = PaperImportService(connection)
        groups = PaperGroupService(connection)
        papers = PaperService(connection)
        selected_group = groups.get_by_name(group) or groups.create(group)
        imported: list[dict[str, object]] = []
        errors: list[str] = []

        arxiv_items: list[tuple[str, str]] = []
        remaining_items: list[dict[str, object]] = []
        for raw_item in payload:
            item = dict(raw_item)
            url = str(item.get("url", "")).strip()
            arxiv_id = importer.arxiv_id_from_url(url) if url else None
            if arxiv_id and set(item).issubset({"url", "title"}):
                arxiv_items.append((arxiv_id, url))
            else:
                remaining_items.append(item)

        if arxiv_items:
            try:
                arxiv_papers = importer.import_arxiv_ids([item[0] for item in arxiv_items])
                imported.extend(arxiv_papers)
                imported_ids = {str(paper["arxiv_id"]) for paper in arxiv_papers}
                errors.extend(
                    f"{url}: arXiv paper {arxiv_id} was not found"
                    for arxiv_id, url in arxiv_items
                    if arxiv_id not in imported_ids
                )
            except PaperImportError as error:
                errors.extend(f"{url}: {error}" for _arxiv_id, url in arxiv_items)

        for item in remaining_items:
            url = str(item.pop("url", "")).strip()
            try:
                if set(item).issubset({"title"}):
                    imported.append(importer.import_url(url, title=str(item.get("title", ""))))
                else:
                    item.setdefault("webpage_url", url)
                    imported.append(importer.import_metadata(item))
            except (PaperImportError, TypeError) as error:
                errors.append(f"{url or item.get('title', 'paper')}: {error}")

        for paper in imported:
            paper_key = str(paper["arxiv_id"])
            papers.set_saved(paper_key, True)
            groups.add_paper(paper_key, int(selected_group["id"]))

        generated = current_app.extensions["embedding_service"].index_pending() if embed else 0
        click.echo(
            f"Imported {len(imported)} paper(s) into {selected_group['name']}; "
            f"generated {generated} embedding(s)."
        )
        for error in errors:
            click.echo(f"Skipped {error}", err=True)
        if errors:
            raise click.ClickException(f"{len(errors)} paper(s) could not be imported")
