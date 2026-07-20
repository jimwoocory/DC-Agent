import warnings

from astrbot.core.star.star import StarMetadata, star_map

_warned_register_star = False


def register_plugin(
    name: str,
    author: str,
    desc: str,
    version: str,
    repo: str | None = None,
):
    """Register explicit metadata for one Star plugin class.

    Args:
        name: Plugin name.
        author: Plugin author.
        desc: Short plugin description.
        version: Plugin version.
        repo: Optional plugin repository URL.

    Returns:
        A class decorator that attaches metadata to the discovered Star class.
    """

    def decorator(cls):
        if not star_map.get(cls.__module__):
            metadata = StarMetadata(
                name=name,
                author=author,
                desc=desc,
                version=version,
                repo=repo,
            )
            star_map[cls.__module__] = metadata
        else:
            star_map[cls.__module__].name = name
            star_map[cls.__module__].author = author
            star_map[cls.__module__].desc = desc
            star_map[cls.__module__].version = version
            star_map[cls.__module__].repo = repo

        return cls

    return decorator


def register_star(
    name: str,
    author: str,
    desc: str,
    version: str,
    repo: str | None = None,
):
    """Register legacy Star metadata through the deprecated API.

    Args:
        name: Plugin name.
        author: Plugin author.
        desc: Short plugin description.
        version: Plugin version.
        repo: Optional plugin repository URL.

    Returns:
        A class decorator that attaches plugin metadata.
    """
    global _warned_register_star
    if not _warned_register_star:
        _warned_register_star = True
        warnings.warn(
            "The 'register_star' decorator is deprecated and will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )
    return register_plugin(name, author, desc, version, repo)
