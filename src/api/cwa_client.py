import os
import requests
import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote

logger = logging.getLogger(__name__)

class CWAClient:
    def __init__(self):
        self.base_url = os.environ.get("CWA_SERVER", "").rstrip('/')
        self.username = os.environ.get("CWA_USERNAME", "")
        self.password = os.environ.get("CWA_PASSWORD", "")
        self.enabled = os.environ.get("CWA_ENABLED", "").lower() == "true"
        
        self.session = requests.Session()
        if self.username and self.password:
            self.session.auth = (self.username, self.password)
            
        self.timeout = 30

    def is_configured(self):
        """Check if CWA is enabled and configured."""
        return self.enabled and bool(self.base_url)

    def search_ebooks(self, query):
        """
        Search CWA via OPDS feed for ebook matches.
        Returns a list of dicts: {'title': str, 'author': str, 'download_url': str, 'ext': str}
        """
        if not self.is_configured():
            return []

        # Sanitize query
        safe_query = quote(query)
        search_url = f"{self.base_url}/opds/search?q={safe_query}"
        
        try:
            logger.debug(f"🔍 CWA: Searching for '{query}'...")
            r = self.session.get(search_url, timeout=self.timeout)
            
            if r.status_code != 200:
                logger.warning(f"⚠️ CWA Search failed {r.status_code}: {search_url}")
                return []
                
            return self._parse_opds(r.text)

        except Exception as e:
            logger.error(f"❌ CWA Search Error: {e}")
            return []

    def _parse_opds(self, xml_content):
        """Parse Atom XML response from OPDS feed."""
        results = []
        try:
            # OPDS is Atom-based
            # Namespaces are annoying in ElementTree, ignore them or handle them
            # For simplicity, we'll try to handle standard Atom namespace
            namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
            
            root = ET.fromstring(xml_content)
            
            for entry in root.findall('atom:entry', namespaces):
                title = entry.find('atom:title', namespaces).text
                author_elem = entry.find('atom:author/atom:name', namespaces)
                author = author_elem.text if author_elem is not None else "Unknown"
                
                # Find EPUB link
                epub_link = None
                for link in entry.findall('atom:link', namespaces):
                    rel = link.get('rel')
                    mime = link.get('type')
                    href = link.get('href')
                    
                    if mime == "application/epub+zip" or (rel and "http://opds-spec.org/acquisition" in rel and mime == "application/epub+zip"):
                        epub_link = href
                        break
                
                if epub_link:
                    # Resolve relative URLs
                    if not epub_link.startswith('http'):
                         epub_link = f"{self.base_url}{epub_link}" if epub_link.startswith('/') else f"{self.base_url}/{epub_link}"

                    results.append({
                        "title": title,
                        "author": author,
                        "download_url": epub_link,
                        "ext": "epub",
                        "source": "CWA"
                    })
                    
            return results

        except Exception as e:
            logger.error(f"Error parsing CWA OPDS: {e}")
            return []

    def download_ebook(self, download_url, output_path):
        """Download ebook file from URL to output_path."""
        try:
            logger.info(f"⬇️ CWA: Downloading ebook from {download_url}")
            with self.session.get(download_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            # Verify file size
            if os.path.getsize(output_path) < 1024:
                logger.warning(f"⚠️ Downloaded file is too small ({os.path.getsize(output_path)} bytes), likely failed.")
                return False
                
            return True
        except Exception as e:
            logger.error(f"❌ CWA Download failed: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
