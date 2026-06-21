"""Shared resource-blocking logic for any page the crawler or master agent opens."""

DEFAULT_BLOCKED_RESOURCES = frozenset({"image", "font", "media"})


def resolve_blocked_types(block_resources):
    """Normalize a `block_resources` setting into a frozenset of Playwright resource
    types to abort. `None` means "use the default block list"; `[]` means "block
    nothing". Raises ValueError on a bare string — the common caller mistake of
    passing a single resource-type name instead of a list containing it.
    """
    if isinstance(block_resources, str):
        raise ValueError(
            "block_resources must be a list of resource-type strings (e.g. "
            f"['image', 'font']), not a single string {block_resources!r}"
        )
    if block_resources is None:
        return DEFAULT_BLOCKED_RESOURCES
    return frozenset(block_resources)


async def install_resource_blocking(page, blocked_types):
    """Install a route interceptor that aborts the given resource types. No-op if
    blocked_types is empty (load everything)."""
    if blocked_types:
        await page.route("**/*", lambda route:
            route.abort() if route.request.resource_type in blocked_types
            else route.continue_()
        )
