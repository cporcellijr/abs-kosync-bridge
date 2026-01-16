# Unified Database Service Architecture

## ✅ Simplified Database Architecture

The abs-kosync-bridge application now has a **unified database service** that works exclusively with SQLAlchemy models, eliminating dictionary conversions and providing cleaner, type-safe code.

## 🏗️ Architecture Overview

### Single Database Service
- **`DatabaseService`** - Unified service working directly with SQLAlchemy models
- **`SQLiteJsonDBWrapper`** - Compatibility layer for existing JsonDB interface
- **`DatabaseMigrator`** - One-time migration from JSON to SQLAlchemy

### Models (ORM Objects)
- **`Book`** - Book metadata and mapping information
- **`State`** - Sync state per book and client (kosync, abs, absebook)
- **`Job`** - Job execution tracking with error handling

## 🎯 Key Benefits

1. **Model-First Design**: All operations work with typed SQLAlchemy models
2. **Type Safety**: No more `Dict[str, Any]` return types
3. **Clean Interface**: Direct model input/output for better code clarity
4. **Backward Compatibility**: Existing JsonDB code continues to work
5. **Single Responsibility**: One service handles all database operations

## 🚀 Usage Examples

### Working with Models (New Approach)

```python
from src.db.database_service import DatabaseService
from src.db.models import Book, State, Job
from src.utils.di_container import create_container
import time

# Get service via dependency injection
container = create_container()
db_service = container.database_service()

# Create a book model
book = Book(
    abs_id='book-123',
    abs_title='My Book',
    ebook_filename='book.epub',
    status='active'
)

# Save the book (returns Book model)
saved_book = db_service.save_book(book)
print(f"Created: {saved_book.abs_title}")

# Create a state model
state = State(
    abs_id='book-123',
    client_name='kosync',
    last_updated=time.time(),
    percentage=0.5,
    xpath='/chapter/1'
)

# Save the state (returns State model)
saved_state = db_service.save_state(state)
print(f"Progress: {saved_state.percentage:.1%}")

# Query models back
book = db_service.get_book('book-123')  # Returns Book model
states = db_service.get_states_for_book('book-123')  # Returns List[State]

# Work with typed models
for state in states:
    print(f"{state.client_name}: {state.percentage:.1%}")
```

## 📊 Database Operations

### Book Operations
- `get_book(abs_id: str) -> Optional[Book]`
- `get_all_books() -> List[Book]`
- `save_book(book: Book) -> Book`
- `delete_book(abs_id: str) -> bool`
- `get_books_by_status(status: str) -> List[Book]`

### State Operations
- `get_state(abs_id: str, client_name: str) -> Optional[State]`
- `get_states_for_book(abs_id: str) -> List[State]`
- `get_all_states() -> List[State]`
- `save_state(state: State) -> State`

### Job Operations
- `get_latest_job(abs_id: str) -> Optional[Job]`
- `get_jobs_for_book(abs_id: str) -> List[Job]`
- `save_job(job: Job) -> Job`
- `update_latest_job(abs_id: str, **kwargs) -> Optional[Job]`

### Advanced Queries
- `get_books_with_recent_activity(limit: int) -> List[Book]`
- `get_failed_jobs(limit: int) -> List[Job]`
- `get_statistics() -> dict`

## 🔧 Dependency Injection

The unified service is available through the DI container:

```python
from src.utils.di_container import create_container

container = create_container()

# Get the unified database service
db_service = container.database_service()

# JsonDB wrappers (for existing code)
db_handler = container.db_handler()
state_handler = container.state_handler()
```

## 🔄 Migration

- **Automatic**: Migration happens on application startup
- **One-time**: JSON data is imported once into SQLAlchemy models
- **Seamless**: No code changes needed for existing functionality

## 📁 File Structure

```
src/db/
├── models.py                    # SQLAlchemy model definitions
├── database_service.py         # Unified database service (models only)
├── sqlite_json_wrapper.py      # JsonDB compatibility wrapper
├── migration_utils.py          # Migration utilities
└── *.py.old                    # Backed up old services
```

## 🎉 Result

- **One unified service** instead of multiple database classes
- **Model-first approach** with proper type safety
- **Clean interfaces** without dictionary conversions
- **Full backward compatibility** for existing code
- **Better maintainability** with single responsibility

The architecture is now simplified while maintaining all functionality and providing a solid foundation for future enhancements.
