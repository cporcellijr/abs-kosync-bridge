# Database Migrations with Alembic

This project uses Alembic for database schema migrations, providing a professional and reliable way to handle database schema changes like adding the `duration` field to the `books` table.

## Setup

### Files Added:
- `alembic.ini` - Alembic configuration file
- `alembic/` - Alembic migration directory
- `alembic/env.py` - Alembic environment configuration (configured to use our models)
- `alembic/versions/` - Migration files directory
- `requirements.txt` - Updated to include `alembic` dependency

### Current Migration:
- **Migration ID**: `4e9d56681692`
- **Description**: "Add duration column to books table"
- **Purpose**: Adds the `duration` column to the existing `books` table for storing audiobook duration in seconds

## How It Works

### Automatic Migration
The `DatabaseService` automatically runs Alembic migrations on initialization:

```python
def __init__(self, db_path: str):
    self.db_path = Path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self.db_manager = DatabaseManager(str(self.db_path))
    
    # Run Alembic migrations to ensure schema is up to date
    self._run_alembic_migrations()
```

### Migration Process
1. **New Databases**: Alembic creates the complete schema with all current fields including `duration`
2. **Existing Databases**: Alembic adds the `duration` column to existing `books` tables
3. **Fallback**: If Alembic fails, SQLAlchemy's `create_all()` provides basic table creation

## Benefits Over Manual Schema Changes

### ✅ **Professional Migration Management**
- **Version Control**: Each migration has a unique ID and can be tracked
- **Rollback Support**: Migrations can be reversed if needed
- **Dependency Management**: Migrations can depend on previous migrations

### ✅ **Reliable Schema Updates**
- **Idempotent**: Can run multiple times safely
- **Atomic**: Changes are applied as transactions
- **Cross-Platform**: Works consistently across different SQLite versions

### ✅ **Development Workflow**
- **Auto-Detection**: `alembic revision --autogenerate` can detect model changes
- **Review Process**: Migration files can be reviewed before deployment
- **History Tracking**: Complete history of schema changes

## Usage

### For New Fields/Tables:
1. **Update Models**: Add new fields to SQLAlchemy models in `src/db/models.py`
2. **Generate Migration**: 
   ```bash
   cd /path/to/project
   alembic revision --autogenerate -m "Add new field description"
   ```
3. **Review Migration**: Check the generated migration file in `alembic/versions/`
4. **Deploy**: Migrations run automatically when `DatabaseService` is initialized

### Manual Migration Commands:
```bash
# Check current migration status
alembic current

# Show migration history  
alembic history

# Upgrade to latest
alembic upgrade head

# Rollback to previous migration
alembic downgrade -1
```

## Duration Field Implementation

### Model Definition:
```python
class Book(Base):
    # ...existing fields...
    duration = Column(Float)  # Duration in seconds from AudioBookShelf
```

### Migration SQL:
```sql
-- Upgrade
ALTER TABLE books ADD COLUMN duration REAL;

-- Downgrade  
ALTER TABLE books DROP COLUMN duration;
```

### Data Flow:
1. **AudioBookShelf API** → Returns media with `duration` field
2. **SyncManager.get_duration()** → Extracts duration from API response
3. **Web Routes** → Use `get_duration()` when creating Book objects
4. **Database** → Stores duration as Float for precision
5. **Dashboard** → Uses stored duration for accurate progress calculations

## Migration Safety

### Production Considerations:
- **Backup First**: Always backup database before migrations in production
- **Test Migrations**: Test migration on copy of production data
- **Monitor Logs**: Check application logs for migration status
- **Graceful Fallback**: Application continues to work if Alembic fails

### Error Handling:
The migration system includes robust error handling:
- Warns if Alembic config is missing
- Falls back to basic table creation if migration fails
- Logs all migration attempts and results
- Continues application startup even if migration has issues

This migration system ensures that the `duration` field is properly added to both new and existing databases while maintaining data integrity and providing a professional upgrade path.
