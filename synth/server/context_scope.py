"""Bind an in-flight document to the recorder and the folder supplying context.

Owning a meeting after losing folder access does not grant its historical context.
This receipt is metadata, not a permission token; every use checks current rows.
"""


def snapshot(db, meeting_id, *, worker=False):
    # The worker may ask one yes/no question, never read the membership table.
    access = 'document_context_allowed(m.id)' if worker else "(f.owner_id=m.owner_id OR fm.role IN ('reader','editor'))"
    joins = '' if worker else '''LEFT JOIN folders f ON f.id=m.folder_id
        LEFT JOIN folder_memberships fm ON fm.folder_id=m.folder_id AND fm.principal_id=m.owner_id'''
    row = db.execute(f"""SELECT m.owner_id,m.folder_id,p.revoked_at,
        {access} AS folder_allowed
        FROM meetings m JOIN principals p ON p.id=m.owner_id AND p.kind='user'
        {joins}
        WHERE m.id=%s""", (meeting_id,)).fetchone()
    if not row or row['revoked_at'] is not None or (row['folder_id'] and not row['folder_allowed']):
        raise ValueError('generation_context_changed')
    return {'owner_id': str(row['owner_id']), 'folder_id': str(row['folder_id']) if row['folder_id'] else None}


def assert_current(db, meeting_id, expected, *, worker=False):
    if expected is None or snapshot(db, meeting_id,worker=worker) != expected:
        raise ValueError('generation_context_changed')
