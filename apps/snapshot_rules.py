"""Rename hints for snapshot import (controller/snapshot.py).

Import matches columns by name, so new columns and removed columns need no
entry here. Only fill these in when a refactor *renames* something; otherwise
the old values would be dropped and the new column left empty.

Examples:
    TABLE_RENAMES = {'client_sites': 'clients'}                 # old table -> new table
    COLUMN_RENAMES = {'documents.notes': 'catatan'}             # '<new table>.<old column>' -> new column
"""

TABLE_RENAMES = {}
COLUMN_RENAMES = {}
