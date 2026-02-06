import os
import requests
import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote

logger = logging.getLogger(__name__)

class CWAClient:
    def __init__(self):
        # Strip trailing slash and verify we don't duplicate /opds
        raw_url = os.environ.get("CWA_SERVER", "").rstrip('/')
        if raw_url.endswith('/opds'):
            raw_url = raw_url[:-5]
        self.base_url = raw_url
        self.username = os.environ.get("CWA_USERNAME", "")
        self.password = os.environ.get("CWA_PASSWORD", "")
        self.enabled = os.environ.get("CWA_ENABLED", "").lower() == "true"
        
        self.session = requests.Session()
        if self.username and self.password:
            self.session.auth = (self.username, self.password)
            
        self.timeout = 30
        self.search_template = None

    def is_configured(self):
        """Check if CWA is enabled and configured."""
        return self.enabled and bool(self.base_url)

    def _get_search_template(self):
        """
        Dynamically discover the search URL template from the OPDS root.
        Returns: URL template string (e.g. '/opds/search/{searchTerms}') or None.
        """
        if self.search_template:
            return self.search_template

        try:
            logger.debug(f"🔍 CWA: Discovering search endpoint from {self.base_url}/opds")
            r = self.session.get(f"{self.base_url}/opds", timeout=self.timeout)
            if r.status_code != 200:
                logger.warning(f"⚠️ CWA OPDS Root failed {r.status_code}")
                return None

            root = ET.fromstring(r.text)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            
            # Find proper search link (prefer atom+xml)
            search_link = None
            
            # Helper to check link
            def is_valid_search_link(link_elem):
                return link_elem.get('rel') == 'search'
            
            # 1. Try standard Atom namespace with type check
            for link in root.findall('atom:link', ns):
                if is_valid_search_link(link):
                    l_type = link.get('type', '')
                    l_href = link.get('href')
                    if 'atom+xml' in l_type:
                        search_link = l_href
                        break # Found best match
                    elif not search_link and 'opensearch' not in l_type:
                        # Backup candidate (if not explicitly OSD)
                        search_link = l_href

            # 2. Fallback: Namespace-agnostic search
            if not search_link:
                for child in root:
                    if child.tag.endswith('link') and is_valid_search_link(child):
                        l_type = child.get('type', '')
                        l_href = child.get('href')
                        if 'atom+xml' in l_type:
                            search_link = l_href
                            break
                        elif not search_link and 'opensearch' not in l_type:
                            search_link = l_href

            if search_link:
                self.search_template = search_link
                # Ensure absolute URL
                if self.search_template and not self.search_template.startswith('http'):
                    self.search_template = f"{self.base_url}{self.search_template}"
                logger.info(f"✅ CWA: Discovered search template: {self.search_template}")
                return self.search_template

        except Exception as e:
            logger.error(f"❌ CWA Discovery Error: {e}")
        
        return None

    def search_ebooks(self, query):
        """
        Search CWA via OPDS feed for ebook matches.
        Returns a list of dicts: {'title': str, 'author': str, 'download_url': str, 'ext': str}
        """
        if not self.is_configured():
            return []

        # Get search template (dynamic or fallback)
        template = self._get_search_template()
        
        if not template:
            # Fallback to legacy assumed standard if discovery fails
            safe_query = quote(query)
            search_url = f"{self.base_url}/opds/search?q={safe_query}"
            logger.warning("⚠️ CWA: Could not discover search template, falling back to legacy URL.")
        else:
            # Replace {searchTerms} with query
            # Note: We must encode the query, but the template syntax might vary.
            # Standard is {searchTerms}, we replace it.
            safe_query = quote(query)
            if "{searchTerms}" in template:
                search_url = template.replace("{searchTerms}", safe_query)
            else:
                 # If template doesn't have placeholder (weird), try appending
                 pass 
                 # Actually, let's assume if it returns a base URL, we append query?
                 # No, defined spec says it should have it.
                 # If missing, we might fail or try simple replace?
                 search_url = template.replace("{searchTerms}", safe_query)
        
        try:
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

                    # Extract ID from entry (OPDS uses atom:id)
                    id_elem = entry.find('atom:id', namespaces)
                    entry_id = None
                    if id_elem is not None and id_elem.text:
                        # Extract a usable ID from the atom:id (often a URN or URL)
                        import re
                        # Try to get the last numeric/alphanumeric portion
                        match = re.search(r'(\d+)$', id_elem.text)
                        if match:
                            entry_id = match.group(1)
                        else:
                            # Fallback: clean the title
                            entry_id = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]
                    else:
                        import re
                        entry_id = re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]

                    results.append({
                        "id": entry_id,
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
