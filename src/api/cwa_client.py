import os
import requests
import logging
import base64
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
        
        # Standardize headers for all requests
        headers = {
            "User-Agent": "ABS-KoSync-Bridge/1.0",
            "Accept": "application/atom+xml,application/xml,application/xhtml+xml,text/xml;q=0.9,*/*;q=0.8",
        }

        # Force Pre-emptive Basic Auth
        # This sends credentials immediately without waiting for a 401 Challenge,
        # bypassing the "Redirect to Login Page" issue.
        if self.username and self.password:
            # We still set session.auth for compatibility, but the header takes precedence
            # REMOVED: self.session.auth = (self.username, self.password)
            
            # Manually construct the header
            user_pass = f"{self.username}:{self.password}"
            encoded_u = base64.b64encode(user_pass.encode()).decode()
            headers["Authorization"] = f"Basic {encoded_u}"
            
        self.session.headers.update(headers)
            
        self.timeout = 30
        self.search_template = None

    def _make_request(self, url, **kwargs):
        """Helper to make requests with cookies cleared to force Basic Auth."""
        try:
            # Clear cookies to prevent 'Guest' session sticking
            self.session.cookies.clear()
            # Merge kwargs with default timeout if not present
            kwargs.setdefault('timeout', self.timeout)
            return self.session.get(url, **kwargs)
        except Exception as e:
            logger.error(f"CWA Request failed: {e}")
            raise

    def is_configured(self):
        """Check if CWA is enabled and configured."""
        return self.enabled and bool(self.base_url)

    def check_connection(self):
        """Check connection to CWA and validate response type."""
        if not self.is_configured():
            logger.warning("⚠️ CWA not configured (skipping)")
            return False

        try:
            url = f"{self.base_url}/opds"
            # Use helper
            r = self._make_request(url, timeout=5)
            
            # Check for soft login redirect (status 200 but HTML content)
            if r.status_code == 200:
                if r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                    logger.error("❌ CWA Connection Failed: Server returned HTML login page instead of XML. Authentication failed.")
                    return False
                
                logger.info(f"✅ Connected to CWA at {self.base_url}")
                return True
                
            elif r.status_code in [401, 403]:
                logger.error(f"❌ CWA Connection Failed: Unauthorized ({r.status_code}). Check credentials.")
                return False
            else:
                logger.warning(f"❌ CWA Connection Failed: {r.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ CWA Connection Error: {e}")
            return False

    def _get_search_template(self):
        """
        Dynamically discover the search URL template from the OPDS root.
        Returns: URL template string (e.g. '/opds/search/{searchTerms}') or None.
        """
        if self.search_template:
            return self.search_template

        try:
            logger.debug(f"🔍 CWA: Discovering search endpoint from {self.base_url}/opds")
            # Use helper
            r = self._make_request(f"{self.base_url}/opds")
            
            # Check if we got an HTML login page disguised as 200 OK
            if r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                 logger.warning("⚠️ CWA Discovery Failed: Server returned HTML content. Likely authentication failure (Soft Redirect).")
                 return None

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
            # Use helper
            r = self._make_request(search_url)
            
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
            # Check for HTML response (common if auth failed or 404 page returned as 200)
            if xml_content.lstrip().lower().startswith(('<!doctype html', '<html')):
                logger.warning("⚠️ CWA returned HTML content instead of XML. Check configuration/URL.")
                logger.debug(f"HTML Snippet: {xml_content[:200]}")
                return []

            # OPDS is Atom-based
            # Namespaces are annoying in ElementTree, ignore them or handle them
            # For simplicity, we'll try to handle standard Atom namespace
            namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
            
            root = ET.fromstring(xml_content)
            
            entries = []
            # Check if root is a feed or an entry
            if root.tag.endswith('entry'):
                entries = [root]
            else:
                entries = root.findall('atom:entry', namespaces)

            for entry in entries:
                title_elem = entry.find('atom:title', namespaces)
                title = title_elem.text if title_elem is not None else "Unknown"
                
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
            logger.debug(f"Failed XML content (first 500 chars): {xml_content[:500]}")
            return []

    def get_book_by_id(self, cwa_id):
        """
        Fetch a specific book by its CWA ID (calibre ID) via OPDS.
        Tries standard endpoints: /opds/book/{id}
        """
        if not self.is_configured(): return None
        
        # Try standard endpoints
        # Calibre-Web usually supports /opds/books/{id} or /opds/book/{id}
        endpoints = [f"/opds/book/{cwa_id}", f"/opds/books/{cwa_id}"]
        
        for ep in endpoints:
            try:
                url = f"{self.base_url}{ep}"
                logger.debug(f"🔍 CWA: Trying direct ID lookup at {url}")
                # Use helper
                r = self._make_request(url)
                if r.status_code == 200:
                    # Parse single entry
                    # Note: API might return a feed with one entry, or just the entry
                    results = self._parse_opds(r.text)
                    if results:
                        # Return the first result that matches (should be only one)
                        for res in results:
                            # If parser extracted ID, check match. Or just assume it's the right one.
                            if str(res['id']) == str(cwa_id):
                                return res
                        # Fallback: if we just got one result from a direct ID call, it's probably it
                        if len(results) == 1:
                            return results[0]
            except Exception as e:
                logger.warning(f"CWA ID lookup failed for {url}: {e}")
                
        return None

    def download_ebook(self, download_url, output_path):
        """Download ebook file from URL to output_path."""
        try:
            logger.info(f"⬇️ CWA: Downloading ebook from {download_url}")
            # Clear cookies manually for download too
            self.session.cookies.clear()
            
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
