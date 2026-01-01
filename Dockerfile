# [START FILE: abs-kosync-enhanced/Dockerfile]
FROM 00jlich/abs-kosync-bridge:latest

# Install additional Python dependencies (Flask, XML, Fuzzy Matching)
RUN pip install --no-cache-dir flask lxml rapidfuzz

# Copy enhanced Python modules to src/ directory
COPY main.py /app/src/main.py
COPY storyteller_db.py /app/src/storyteller_db.py
COPY storyteller_api.py /app/src/storyteller_api.py
COPY transcriber.py /app/src/transcriber.py
COPY ebook_utils.py /app/src/ebook_utils.py
COPY api_clients.py /app/src/api_clients.py
COPY json_db.py /app/src/json_db.py
COPY hardcover_client.py /app/src/hardcover_client.py
COPY suggestion_manager.py /app/src/suggestion_manager.py

# Copy web server to /app root
COPY web_server.py /app/web_server.py

# Create templates directory and copy HTML templates
RUN mkdir -p /app/templates
COPY templates/ /app/templates/

# Copy and set permissions for startup script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Expose web UI port
EXPOSE 5757

# Run startup script (starts both daemon and web server)
CMD ["/app/start.sh"]
# [END FILE]
