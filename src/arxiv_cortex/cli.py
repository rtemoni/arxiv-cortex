from __future__ import annotations

import click
from flask import Flask, current_app

from arxiv_cortex.db import run_migrations


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
