import config

# Determine storage backend
if config.STORAGE_BACKEND == "database":
    from .database_storage import *
elif config.STORAGE_BACKEND == "file":
    from .file_storage import *
else:
    raise ValueError(f"Invalid STORAGE_BACKEND: {config.STORAGE_BACKEND}")

async def init_storage():
    """Initialize the selected storage backend"""
    if config.STORAGE_BACKEND == "database":
        from .database_storage import init_database
        await init_database()
    elif config.STORAGE_BACKEND == "file":
        from .file_storage import init_file_storage
        await init_file_storage()
