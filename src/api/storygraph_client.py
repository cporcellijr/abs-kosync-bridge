"""
StoryGraph Selenium Client - ON-DEMAND VERSION

Launches Chrome only when needed and kills it immediately to save RAM.
Uses the "Start → Run → Kill" pattern for low-memory environments.
Handles dynamic installation of Chromium if missing.
"""

import os
import logging
import time
import re
import subprocess
import shutil
from typing import Optional, Dict, Callable, Any
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_selenium_available = None

def _check_selenium():
    global _selenium_available
    if _selenium_available is None:
        try:
            import selenium
            # Also check if webdriver-manager is available
            import webdriver_manager
            _selenium_available = True
        except ImportError:
            _selenium_available = False
            logger.warning("Selenium or webdriver-manager not installed - StoryGraph sync disabled")
    return _selenium_available

class StoryGraphClient:
    """
    StoryGraph client using Selenium browser automation.
    Chrome is launched on-demand and killed immediately after each task.
    """
    
    BASE_URL = "https://app.thestorygraph.com"
    
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._logged_in = False
        self._chromium_path = None
        self._chromedriver_path = None
    
    def is_configured(self) -> bool:
        return bool(self.email and self.password and _check_selenium())
    
    def ensure_chromium_installed(self) -> bool:
        """
        Dynamically checks for and installs Chromium/Chromedriver if missing.
        Returns True if installation is successful or already present.
        """
        if not shutil.which('chromium') and not shutil.which('chromium-browser') and not shutil.which('google-chrome'):
            logger.info("📦 Chromium not found. Attempting dynamic installation...")
            try:
                # Update apt and install chromium
                # NOTE: This requires root permissions (typical in Docker)
                subprocess.run(['apt-get', 'update', '-qq'], check=True)
                subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'chromium', 'chromium-driver'], check=True)
                
                # Cleanup to save space
                subprocess.run(['rm', '-rf', '/var/lib/apt/lists/*'], check=False)
                
                logger.info("✅ Chromium installation successful")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to install Chromium dynamically: {e}")
                return False
                
        return True

    def check_connection(self) -> bool:
        if not self.is_configured():
            raise Exception("StoryGraph not configured - missing credentials or Selenium")
            
        # Ensure browser is present before trying to connect
        if not self.ensure_chromium_installed():
             raise Exception("Chromium browser could not be installed/found")
        
        def _test_login(driver, wait):
            return True
        
        result = self._run_task(_test_login)
        if result:
            logger.info("✅ StoryGraph connection verified")
            return True
        raise Exception("StoryGraph login failed")
    
    def _create_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-images")
        opts.add_argument("--blink-settings=imagesEnabled=false")
        opts.add_argument("--js-flags=--max-old-space-size=128")
        opts.add_argument("--single-process")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # New: Session Persistence
        DATA_DIR = os.environ.get("DATA_DIR", "/data")
        profile_path = os.path.join(DATA_DIR, "storygraph_chrome_profile")
        opts.add_argument(f"--user-data-dir={profile_path}")
        
        # Detect system chromium location
        if not self._chromium_path:
             self._chromium_path = shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')
             
        if self._chromium_path:
            opts.binary_location = self._chromium_path
            
        # Detect system chromedriver location - check explicit paths first
        if not self._chromedriver_path:
            # Common locations for Debian/Alpine chromedriver
            explicit_paths = ['/usr/bin/chromedriver', '/usr/lib/chromium/chromedriver']
            for path in explicit_paths:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    self._chromedriver_path = path
                    break
            if not self._chromedriver_path:
                self._chromedriver_path = shutil.which('chromedriver')

        try:
            if self._chromedriver_path:
                logger.debug(f"Using system chromedriver: {self._chromedriver_path}")
                service = Service(executable_path=self._chromedriver_path)
                driver = webdriver.Chrome(service=service, options=opts)
            else:
                # No system driver found - this will likely fail in Docker
                logger.warning("No system chromedriver found, falling back to webdriver-manager (may not work)")
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=opts)
        except Exception as e:
            logger.error(f"Failed to initialize Chrome driver: {e}")
            raise e
        
        driver.set_page_load_timeout(30)
        return driver
    
    def _login(self, driver, wait) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        
        logger.debug("🔐 Checking StoryGraph session...")
        driver.get(f"{self.BASE_URL}/")
        time.sleep(1)
        
        # Check if already logged in by looking for profile/user indicators
        try:
            # Short wait for indicators
            success_wait = WebDriverWait(driver, 5)
            success_wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR, 
                "a[href*='/profile'], a[href*='/users/edit'], .user-menu, .nav-user-links"
            )))
            logger.debug("✅ StoryGraph session active (no login needed)")
            return True
        except:
            logger.debug("🔑 StoryGraph session not found, proceeding to login...")

        driver.get(f"{self.BASE_URL}/users/sign_in")
        
        # Double check we are on sign-in page (in case of redirect)
        if "/sign_in" not in driver.current_url:
            return True

        email_selectors = [
            (By.NAME, "user[email]"),
            (By.ID, "user_email"),
            (By.CSS_SELECTOR, "input[type='email']"),
        ]
        
        email_field = None
        for by, selector in email_selectors:
            try:
                email_field = wait.until(EC.presence_of_element_located((by, selector)))
                break
            except Exception:
                continue
        
        if not email_field:
            logger.error("Could not find email field")
            return False
        
        email_field.clear()
        email_field.send_keys(self.email)
        
        password_selectors = [
            (By.NAME, "user[password]"),
            (By.ID, "user_password"),
            (By.CSS_SELECTOR, "input[type='password']"),
        ]
        
        password_field = None
        for by, selector in password_selectors:
            try:
                password_field = driver.find_element(by, selector)
                break
            except Exception:
                continue
        
        if not password_field:
            logger.error("Could not find password field")
            return False
        
        password_field.clear()
        password_field.send_keys(self.password)
        
        submit_selectors = [
            (By.XPATH, "//input[@type='submit']"),
            (By.XPATH, "//button[contains(., 'Sign in')]"),
            (By.XPATH, "//button[contains(., 'Log in')]"),
            (By.NAME, "commit"),
        ]
        
        for by, selector in submit_selectors:
            try:
                submit_btn = driver.find_element(by, selector)
                submit_btn.click()
                break
            except Exception:
                continue
        
        time.sleep(2)
        
        # Fast check for errors
        try:
            error_elem = driver.find_element(By.CSS_SELECTOR, ".alert-danger, .flash-alert, .error-message, .alert-error")
            if error_elem.is_displayed():
                logger.error(f"❌ StoryGraph login error: {error_elem.text.strip()}")
                return False
        except:
            pass

        # Combined check for success indicators
        try:
            # We use a shorter wait here since we already waited for redirect/sleep
            success_wait = WebDriverWait(driver, 10)
            success_wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR, 
                "a[href*='/profile'], a[href*='/users/edit'], .user-menu, .nav-user-links"
            )))
            logger.debug("✅ StoryGraph login successful")
            return True
        except Exception:
            # Fallback check
            if "/sign_in" not in driver.current_url:
                logger.debug("✅ StoryGraph login appears successful (redirected)")
                return True
        
        logger.error("❌ StoryGraph login failed (timeout or restricted access)")
        return False
    
    def _run_task(self, task_function: Callable[[Any, Any], Any]) -> Optional[Any]:
        if not _check_selenium():
            return None
        
        from selenium.webdriver.support.ui import WebDriverWait
        
        driver = None
        try:
            driver = self._create_driver()
            wait = WebDriverWait(driver, 20)
            
            if not self._login(driver, wait):
                return None
            
            return task_function(driver, wait)
            
        except Exception as e:
            logger.error(f"❌ StoryGraph task failed: {e}")
            return None
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                logger.debug("🧹 Chrome killed to save RAM")
    
    def search_book(self, title: str, author: str = None) -> Optional[Dict]:
        def _search(driver, wait):
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            
            # Helper to check if text contains query words roughly
            def matches_query(text, q_title, q_author):
                text_lower = text.lower()
                if q_title.lower() in text_lower: return True
                return False

            query = f"{title} {author or ''}".strip()
            encoded_query = quote_plus(query)
            
            logger.info(f"🔍 StoryGraph: Searching for '{query}'...")
            driver.get(f"{self.BASE_URL}/browse?search_term={encoded_query}")
            
            time.sleep(2)
            
            result_selectors = [
                (By.CSS_SELECTOR, ".book-title-author-and-series h3 a"),
                (By.CSS_SELECTOR, ".book-title a"),
                (By.CSS_SELECTOR, "a.book-title-link"),
                (By.CSS_SELECTOR, "a[href*='/books/']"),  # Fallback: any book link
            ]
            
            first_result = None
            for by, selector in result_selectors:
                try:
                    results = driver.find_elements(by, selector)
                    for res in results:
                        url = res.get_attribute("href")
                        if '/books/' in url and '/edit' not in url:
                            first_result = res
                            break
                    if first_result: break
                except Exception:
                    continue
            
            if not first_result:
                logger.warning(f"📚 StoryGraph: No results for '{query}'")
                return None
            
            book_url = first_result.get_attribute("href")
            book_title = first_result.text.strip()
            
            book_id_match = re.search(r'/books/([a-f0-9-]+)', book_url)
            if not book_id_match: return None
            
            book_id = book_id_match.group(1)
            
            # Author lookup refined
            found_author = None
            try:
                # Try sibling author link in same container
                parent = first_result.find_element(By.XPATH, "./ancestor::div[contains(@class, 'book-title-author-and-series')]")
                author_elem = parent.find_element(By.CSS_SELECTOR, ".author a")
                found_author = author_elem.text.strip()
            except:
                try:
                    # Fallback author search on result page
                    author_elem = driver.find_element(By.CSS_SELECTOR, "a[href*='/authors/']")
                    found_author = author_elem.text.strip()
                except:
                    pass
            
            pages = self._get_book_pages(driver, wait, book_url)
            
            return {
                'book_id': book_id,
                'title': book_title,
                'author': found_author,
                'pages': pages,
                'url': book_url
            }
            
            logger.info(f"✅ StoryGraph: Found '{book_title}' (ID: {book_id}, {pages} pages)")
            return result
        
        return self._run_task(_search)
    
    def _get_book_pages(self, driver, wait, book_url: str) -> int:
        from selenium.webdriver.common.by import By
        
        try:
            driver.get(book_url)
            time.sleep(1)
            
            page_source = driver.page_source
            match = re.search(r'(\d{2,4})\s*pages?', page_source, re.IGNORECASE)
            if match:
                return int(match.group(1))
        except Exception as e:
            logger.debug(f"Could not get page count: {e}")
        
        return 0
    
    def update_progress(
        self,
        book_id: str,
        pages_read: int,
        total_pages: int = None,
        progress_percent: float = None,
        is_finished: bool = False
    ) -> bool:
        def _update(driver, wait):
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import Select
            
            book_url = f"{self.BASE_URL}/books/{book_id}"
            
            # If finished, we might need a different flow or just set status "read"
            # StoryGraph often manages status via dropdown "Mark as read"
            
            logger.info(f"📖 StoryGraph: Updating to {pages_read} pages (Finished={is_finished})...")
            driver.get(book_url)
            time.sleep(2)

            # 1. Check current status via dropdown
            # Typically a button that says "To Read", "Currently Reading", or "Read"
            # We want to ensure it is "Currently Reading" if not finished
            
            # NOTE: StoryGraph UI is complex. For now, let's try to hit the "progress" button. 
            # If book is not 'Currently Reading', the progress button might not exist.
            # We might need to click "Generic interaction button" first.
            
            # Simple approach: Look for "progress/update" link. If missing, assume we need to mark as reading first.
            
            progress_btn = None
            progress_btn_selectors = [
                 (By.CSS_SELECTOR, "a[href*='/progress']"), # often works for update
                 (By.XPATH, "//a[contains(text(), 'Track progress')]"),
                 (By.XPATH, "//a[contains(text(), 'Update')]"),
            ]
            
            for by, sel in progress_btn_selectors:
                try:
                    progress_btn = driver.find_element(by, sel)
                    if progress_btn: break
                except:
                    pass
            
            if not progress_btn and not is_finished:
                # Try to mark as Currently Reading
                logger.debug("Progress button not found. Attempting to set status 'Currently Reading'...")
                try:
                     # This is tricky as selectors change. 
                     # Using a broad strategy: Find "to-read" button or dropdown and change it.
                     pass 
                except:
                    pass

            if is_finished:
                 # Logic to mark as finished
                 # Ideally find "Mark as read" button
                 pass
            
            # Fallback to standard update flow -> Click update -> Enter pages -> Save
            # Re-using the logic from the prompt which seems standard for "Currently Reading" books
            
            updated = False
            
            # Retry finding update button if we didn't click it yet
            if not progress_btn:
                 # Maybe reload?
                 pass
            
            if progress_btn:
                progress_btn.click()
                time.sleep(1)
                
                # Input pages
                input_field = None
                try:
                    # It might be in a modal now
                    input_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name*='pages_read']")))
                except:
                    # Maybe it is 'current_page'
                    try:
                        input_field = driver.find_element(By.CSS_SELECTOR, "input[name*='current_page']")
                    except:
                        pass
                
                if input_field:
                    input_field.clear()
                    input_field.send_keys(str(pages_read))
                    
                    # Submit
                    try:
                        save_btn = driver.find_element(By.XPATH, "//button[contains(., 'Save') or contains(., 'Update')]")
                        save_btn.click()
                        updated = True
                    except:
                        # Enter key?
                        from selenium.webdriver.common.keys import Keys
                        input_field.send_keys(Keys.RETURN)
                        updated = True
                        
            if updated:
                time.sleep(2)
                logger.info("✅ StoryGraph update submitted")
                return True
                
            return False
        
        return self._run_task(_update) is True

    def resolve_book_from_input(self, input_str: str) -> Optional[Dict]:
        """
        Resolve a StoryGraph book from a URL, ID, ISBN, or Title/Author search.
        Returns dict: { 'book_id', 'title', 'author', 'pages', 'url' } or None.
        """
        if not input_str:
            return None

        input_str = input_str.strip()
        
        # 1. Check if it's a URL
        if 'thestorygraph.com/books/' in input_str:
            book_id_match = re.search(r'/books/([a-f0-9-]+)', input_str)
            if book_id_match:
                book_id = book_id_match.group(1)
                # Fetch details for this specific ID
                def _get_by_id(driver, wait):
                    return self._get_book_details(driver, wait, f"{self.BASE_URL}/books/{book_id}")
                return self._run_task(_get_by_id)

        # 2. Check if it looks like a StoryGraph UUID (ID)
        # StoryGraph IDs look like '57726371-1220-4f0b-b4df-ea940b6e153e'
        if re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', input_str):
            def _get_by_id_direct(driver, wait):
                return self._get_book_details(driver, wait, f"{self.BASE_URL}/books/{input_str}")
            return self._run_task(_get_by_id_direct)

        # 3. Otherwise, treat as a search query (ISBN or Title/Author)
        return self.search_book(input_str)

    def _get_book_details(self, driver, wait, book_url: str) -> Optional[Dict]:
        """Internal helper to extract book metadata from a book page."""
        from selenium.webdriver.common.by import By
        
        try:
            logger.info(f"📄 StoryGraph: Fetching details from {book_url}...")
            driver.get(book_url)
            time.sleep(2)
            
            book_id_match = re.search(r'/books/([a-f0-9-]+)', book_url)
            book_id = book_id_match.group(1) if book_id_match else None
            
            # Title refined lookup
            title = "Unknown Title"
            title_selectors = [
                ".book-title-author-and-series h3",
                ".book-title-and-metadata h2",
                "h2.font-serif",
                ".main-container h2",
                "h3.font-semibold",
                "h2"
            ]
            for sel in title_selectors:
                try:
                    elem = driver.find_element(By.CSS_SELECTOR, sel)
                    if elem.text.strip():
                        title = elem.text.strip()
                        break
                except:
                    continue
                
            # Author refined lookup
            author = "Unknown Author"
            author_selectors = [
                ".book-title-author-and-series a[href*='/authors/']",
                ".book-title-and-metadata a[href*='/authors/']",
                "a[href*='/authors/']"
            ]
            for sel in author_selectors:
                try:
                    author_elem = driver.find_element(By.CSS_SELECTOR, sel)
                    if author_elem.text.strip():
                        author = author_elem.text.strip()
                        break
                except:
                    continue
                
            # Pages
            pages = self._get_book_pages(driver, None, book_url)
            
            return {
                'book_id': book_id,
                'title': title,
                'author': author,
                'pages': pages,
                'url': book_url
            }
        except Exception as e:
            logger.error(f"Failed to extract book details: {e}")
            return None
