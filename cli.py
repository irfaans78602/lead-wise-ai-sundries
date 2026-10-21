"""
cli.py
Command-line entry point for the LeadWise AI sundries backend.

    python cli.py ingest <instance_id> <source> [target] [--scope] [--section] [--refresh]
    python cli.py update-secrets [--file <filename>]
    python cli.py serve
"""

import argparse
import sys
import os

from ingest_rag import run_ingest
from update_secrets import run_update_secrets

def main():
    parser = argparse.ArgumentParser(prog="sundries", description="Sundries CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Ingest Subcommand ---
    ingest_parser = sub.add_parser("ingest", help="Run RAG ingestion pipeline")
    ingest_parser.add_argument("instance_id", help="UUID of the instance")
    ingest_parser.add_argument("source", choices=["url", "pdf", "html", "json"], help="Type of data to ingest")
    ingest_parser.add_argument("target", nargs="?", default=None, help="URL or file path (required for url/pdf)")
    ingest_parser.add_argument("--scope", choices=["public", "internal"], default="public", help="Data scope")
    ingest_parser.add_argument("--section", default="General", help="Root hierarchy section")
    ingest_parser.add_argument("--refresh", action="store_true", help="Delete old chunks before inserting")

    # --- Update Secrets Subcommand ---
    secrets_parser = sub.add_parser("update-secrets", help="Inject secrets.json into the Master DB")
    secrets_parser.add_argument("--file", default="secrets.json", help="Path to secrets file (default: secrets.json)")

    # --- Serve Subcommand ---
    serve_parser = sub.add_parser("serve", help="Run the Flask web server")

    args = parser.parse_args()

    if args.command == "ingest":
        run_ingest(
            instance_id=args.instance_id,
            source=args.source,
            target=args.target,
            scope=args.scope,
            section=args.section,
            refresh=args.refresh
        )
    elif args.command == "update-secrets":
        run_update_secrets(args.file)
    elif args.command == "serve":
        # Start Flask app (import here so it doesn't require DB if just running scripts)
        os.environ.setdefault("FLASK_APP", "main.py")
        os.environ.setdefault("FLASK_ENV", "development")
        from main import app
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()