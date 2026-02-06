from src.utils.di_container import container
from src.db.models import Book, BookloreBook

db = container.database_service()
with db.get_session() as session:
    books = session.query(Book).all()
    bl_books = session.query(BookloreBook).all()
    print(f"Total Books (Active Sync): {len(books)}")
    for b in books:
        print(f"  - {b.abs_title}: {b.status}")
    
    print(f"Total BookloreBooks (Cache): {len(bl_books)}")
