import logging
from importlib.metadata import entry_points
from rove.exporters.builtin import JsonExporter, MarkdownExporter, ActionMapExporter

logger = logging.getLogger(__name__)

# Built-in plugins, keyed by CLI token.
_BUILTIN_EXPORTERS = {
    JsonExporter.name: JsonExporter,
    MarkdownExporter.name: MarkdownExporter,
    ActionMapExporter.name: ActionMapExporter,
}


def get_exporters() -> dict:
    """Built-in exporters plus any registered by community plugins
    via the 'rove.exporters' entry-point group. Works with zero plugins installed."""
    found = dict(_BUILTIN_EXPORTERS)
    try:
        for ep in entry_points(group="rove.exporters"):
            try:
                found[ep.name] = ep.load()
            except Exception as e:
                logger.warning(f"Failed to load exporter plugin {ep.name!r}: {e}")
    except Exception as e:
        logger.debug(f"entry-point discovery skipped: {e}")
    return found
