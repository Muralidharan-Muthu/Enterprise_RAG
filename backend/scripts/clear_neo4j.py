#!/usr/bin/env python3
"""
CLI script to inspect and delete Neo4j GraphRAG data.

Usage:
    python backend/scripts/clear_neo4j.py                # Interactively wipe all Neo4j data
    python backend/scripts/clear_neo4j.py --all          # Non-interactive wipe all Neo4j data
    python backend/scripts/clear_neo4j.py --doc-id <UUID> # Delete graph data for specific document
"""
import argparse
import sys
from pathlib import Path

# Ensure backend root is in sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.config import settings
from app.services import graph_service


def main():
    parser = argparse.ArgumentParser(description="Clean or wipe Neo4j graph data.")
    parser.add_argument("--all", "-a", action="store_true", help="Delete all nodes and relationships from Neo4j.")
    parser.add_argument("--doc-id", "-d", type=str, help="Delete graph data for a specific document ID.")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    print("=" * 60)
    print(" NEO4J GRAPHRAG DATABASE CLEANUP")
    print("=" * 60)
    print(f"Target URI:      {settings.NEO4J_URI}")
    print(f"Target Database: {settings.NEO4J_DATABASE or 'default'}")
    print(f"Target User:     {settings.NEO4J_USERNAME}")
    print("=" * 60)

    if not graph_service.is_available():
        print("❌ Error: Cannot connect to Neo4j instance. Please check your .env credentials.")
        sys.exit(1)

    # Display current stats
    stats = graph_service.get_graph_stats()
    print("\nCurrent Graph Statistics:")
    print(f"  - Documents:     {stats.get('documents', 0)}")
    print(f"  - Entities:      {stats.get('entities', 0)}")
    print(f"  - Communities:   {stats.get('communities', 0)}")
    print(f"  - Relationships: {stats.get('relationships', 0)}")
    print("-" * 60)

    if args.doc_id:
        doc_id = args.doc_id.strip()
        if not args.yes:
            confirm = input(f"Are you sure you want to delete graph data for document '{doc_id}'? (y/N): ")
            if confirm.lower() not in ("y", "yes"):
                print("Operation cancelled.")
                return

        print(f"\nDeleting graph data for document {doc_id}...")
        graph_service.clear_document_graph(doc_id)
        print("[OK] Graph data for document deleted.")
    else:
        if not args.yes and not args.all:
            confirm = input("[WARNING] Are you sure you want to delete ALL nodes and relationships in Neo4j? (y/N): ")
            if confirm.lower() not in ("y", "yes"):
                print("Operation cancelled.")
                return

        print("\nWiping all Neo4j graph data (MATCH (n) DETACH DELETE n)...")
        res = graph_service.clear_all_graph_data()
        print(f"[OK] Successfully deleted {res.get('nodes_deleted', 0)} nodes and {res.get('relationships_deleted', 0)} relationships!")

    # Verify final stats
    new_stats = graph_service.get_graph_stats()
    print("\nUpdated Graph Statistics:")
    print(f"  - Documents:     {new_stats.get('documents', 0)}")
    print(f"  - Entities:      {new_stats.get('entities', 0)}")
    print(f"  - Communities:   {new_stats.get('communities', 0)}")
    print(f"  - Relationships: {new_stats.get('relationships', 0)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
