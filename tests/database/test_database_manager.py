import pytest


@pytest.mark.asyncio
async def test_database_manager_create(tmp_db_path):
    from database.manager import DatabaseManager
    db = DatabaseManager(tmp_db_path)
    await db.connect()
    await db.close()
