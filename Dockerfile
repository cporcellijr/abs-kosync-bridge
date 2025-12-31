FROM 00jlich/abs-kosync-bridge:latest

# Install additional Python dependencies
RUN pip install --no-cache-dir flask lxml

# Copy enhanced Python modules to src/ directory
COPY main.py /app/src/main.py
COPY storyteller_db.py /app/src/storyteller_db.py
COPY transcriber.py /app/src/transcriber.py
COPY ebook_utils.py /app/src/ebook_utils.py
COPY api_clients.py /app/src/api_clients.py
COPY json_db.py /app/src/json_db.py

# Copy web server to /app root
COPY web_server.py /app/web_server.py

# Create templates directory and copy HTML templates
RUN mkdir -p /app/templates
COPY index.html /app/templates/index.html
COPY match.html /app/templates/match.html
COPY batch_match.html /app/templates/batch_match.html
COPY book_linker.html /app/templates/book_linker.html

# Copy and set permissions for startup script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Expose web UI port
EXPOSE 5757

# Run startup script (starts both daemon and web server)
CMD ["/app/start.sh"]
