#!/usr/bin/env python3
"""
Migrate PostgreSQL databases to Neon
Handles:
- Direct endpoint for CREATE DATABASE (pooler can't handle it)
- Percent-encoding for database names with spaces
"""

import os
import sys
import subprocess
import urllib.parse
from typing import Optional

def get_direct_url(pooler_url: str) -> str:
    """Convert pooled URL to direct endpoint for admin operations"""
    # Replace -pooler with direct endpoint
    direct = pooler_url.replace('-pooler', '')
    # Ensure sslmode is set
    if 'sslmode' not in direct:
        direct += '&sslmode=require' if '?' in direct else '?sslmode=require'
    return direct

def encode_db_name(db_name: str) -> str:
    """Percent-encode database name for URL"""
    return urllib.parse.quote(db_name, safe='')

def copy_database(source_url: str, target_url: str, db_name: str):
    """Copy a database from source to target using pg_dump and psql"""
    
    print(f"\n📦 Copying database: {db_name}")
    
    # Get direct endpoint for target (for CREATE DATABASE)
    direct_target = get_direct_url(target_url)
    
    # Encode database name for URL
    encoded_db = encode_db_name(db_name)
    
    # Build target URL with encoded database name
    target_with_db = direct_target.split('?')[0]  # Remove query params
    if '?' in direct_target:
        target_with_db += f'/{encoded_db}?{direct_target.split("?", 1)[1]}'
    else:
        target_with_db += f'/{encoded_db}?sslmode=require'
    
    # Step 1: Dump source database
    print(f"  📤 Dumping from source...")
    dump_cmd = [
        'pg_dump',
        '--no-owner',
        '--no-privileges',
        '--clean',
        '--if-exists',
        source_url
    ]
    
    try:
        # Step 2: Restore to target
        print(f"  📥 Restoring to target...")
        restore_cmd = [
            'psql',
            target_with_db,
            '-q'
        ]
        
        # Pipe pg_dump to psql
        with subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as dump_proc:
            with subprocess.Popen(restore_cmd, stdin=dump_proc.stdout, 
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) as restore_proc:
                dump_proc.stdout.close()
                stdout, stderr = restore_proc.communicate()
                
                if restore_proc.returncode != 0:
                    print(f"  ❌ Error restoring {db_name}:")
                    print(stderr.decode()[:500])
                    return False
                
                print(f"  ✅ Successfully copied {db_name}")
                return True
                
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def discover_databases(source_url):
    """Every database on the source worth copying.

    Asked of the server rather than written down, so this script works for
    whoever runs it - not only for the databases it was first written against.
    """
    import psycopg2
    from urllib.parse import urlparse, unquote

    p = urlparse(source_url)
    conn = psycopg2.connect(
        host=p.hostname or "localhost", port=p.port or 5432,
        user=unquote(p.username or "postgres"), password=unquote(p.password or ""),
        database=(p.path or "/postgres").lstrip("/") or "postgres")
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database "
                    "WHERE datistemplate = false AND datallowconn ORDER BY 1")
        names = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    skip = {"template0", "template1", "rdsadmin", "azure_maintenance",
            "azure_sys", "cloudsqladmin", "alloydbadmin"}
    return [n for n in names if n.lower() not in skip]


def main():
    if len(sys.argv) < 3:
        print("Usage: python migrate_to_host.py <source_url> <target_url> [db ...]")
        print("Example: python migrate_to_host.py 'postgresql://user:pass@localhost/postgres' 'postgresql://user:pass@neon.tech/neondb'")
        print("\nWithout a list of databases, every database on the source is copied.")
        sys.exit(1)

    source_url = sys.argv[1]
    target_url = sys.argv[2]

    # Name them on the command line, or let the source tell us what it has.
    databases = sys.argv[3:]
    if not databases:
        try:
            databases = discover_databases(source_url)
        except Exception as e:
            print(f"❌ Could not list databases on the source: {e}")
            sys.exit(1)
        if not databases:
            print("❌ No databases to copy.")
            sys.exit(1)
        print(f"Found {len(databases)} database(s): {', '.join(databases)}")

    print("🚀 Starting migration to Neon")
    print(f"Source: {source_url[:30]}...")
    print(f"Target: {target_url[:30]}...")
    
    success_count = 0
    for db_name in databases:
        if copy_database(source_url, target_url, db_name):
            success_count += 1
    
    print(f"\n✅ Migration complete: {success_count}/{len(databases)} databases copied")
    
if __name__ == "__main__":
    main()