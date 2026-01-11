from dataclasses import dataclass
from typing import Optional, Callable, Tuple

@dataclass
class ServiceState:
    # can contain xpath, ts, pct, href, frag
    current: dict
    previous_pct: float
    delta: float
    threshold: float
    is_configured: bool
    display: Tuple[str, str]
    value_formatter: Callable[[float], str]
    value_seconds_formatter: Callable[[float], str] = None

@dataclass
class LocatorResult:
    percentage: float
    xpath: Optional[str] = None
    match_index: Optional[int] = None
    cfi: Optional[str] = None
    href: Optional[str] = None
    fragment: Optional[str] = None
    perfect_ko_xpath: Optional[str] = None
    css_selector: Optional[str] = None

@dataclass
class UpdateProgressRequest:
    locator_result: LocatorResult
    txt: Optional[str] = None
    # can be percentage or timestamp (ABS)
    previous_location: Optional[float] = None

@dataclass
class SyncResult:
    # can be percentage or timestamp (ABS)
    location: Optional[float] = None
    success: bool = False

class SyncClient:

    def __init__(self, ebook_parser):
        self.ebook_parser = ebook_parser

    def is_configured(self) -> bool:
        ...
    def get_service_state(self, mapping: dict, prev: dict, title_snip: str = "") -> ServiceState:
        ...
    def get_text_from_current_state(self, mapping: dict, state: ServiceState) -> Optional[str]:
        ...
    def update_progress(self, mapping: dict, request: UpdateProgressRequest) -> SyncResult:
        ...

    def get_locator_from_text(self, txt: str, epub_file_name: str, hint_percentage: float) -> Optional[LocatorResult]:
        if not txt or not epub_file_name:
            return None
        locator_result: LocatorResult = self.ebook_parser.find_text_location(epub_file_name, txt, hint_percentage=hint_percentage)
        if not locator_result:
            return None
        # Add perfect_xpath if match_index is present, special case for KoSync
        perfect_xpath = None
        if locator_result.match_index is not None:
            perfect_xpath = self.ebook_parser.get_perfect_ko_xpath(epub_file_name, locator_result.match_index)
        # Return a new LocatorResult with perfect_xpath included
        return LocatorResult(
            percentage=locator_result.percentage,
            xpath=locator_result.xpath,
            match_index=locator_result.match_index,
            cfi=locator_result.cfi,
            href=locator_result.href,
            fragment=locator_result.fragment,
            perfect_ko_xpath=perfect_xpath
        )
