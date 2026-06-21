from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CrawlResult:
    """Everything an exporter may consume. Built from the crawl's page JSON + site graph."""
    pages: list = field(default_factory=list)        # list of page_data dicts
    output_dir: str = "output"


class Exporter(ABC):
    name: str = ""                                   # CLI token, e.g. "json"

    @abstractmethod
    def export(self, result: CrawlResult, dest: Path) -> Path:
        """Write the export under `dest` and return the path written."""
