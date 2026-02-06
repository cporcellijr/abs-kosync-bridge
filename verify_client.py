from src.api.booklore_client import BookloreClient

try:
    client = BookloreClient()
    print(f"Has download_book: {hasattr(client, 'download_book')}")
    # Simulate the call to see if it works or fails
    try:
        client.download_book("test_id")
    except AttributeError:
        print("Caught expected AttributeError: download_book check passed (it is missing)")
    except Exception as e:
        print(f"Unexpected error: {e}")

except Exception as e:
    print(f"Setup failed: {e}")
