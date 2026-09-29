"""Ask GitHub whether a newer version has been tagged.

This is the whole network surface of the program, in one file so it can be
read in one sitting. It runs when somebody presses a button and at no other
time: nothing polls, nothing fires at startup, nothing is sent anywhere. The
request carries a version string and asks for a list of tags.

OfflineBrowser next door states the principle -- an application that controls
audio hardware has no business making requests on its own. This does not
contradict it. A person asked.

Tags rather than releases: a tag exists the moment it is pushed and needs no
publishing step, and anyone reading this builds from source. Releases could be
checked the same way later without changing what the button does.

Nothing here downloads a program or replaces a running one. A one-file build
cannot safely overwrite itself while executing, and an audio tool that tried
would deserve what it got. It reports a version and leaves the rest to the
person.

urllib rather than requests, because requests is optional here -- it exists
for the minidspd fallback -- and the people least likely to have it are the
ones on the direct USB path, who are most of them.

License: Apache-2.0
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

TAGS_URL = "https://api.github.com/repos/atorax/linidi/tags"
# Where to send somebody who wants the newer one. Tags rather than
# releases, to match what is actually checked.
TAGS_PAGE = "https://github.com/atorax/linidi/tags"

# GitHub refuses a request with no User-Agent, and a bare "python-urllib"
# says nothing useful in anybody's logs.
USER_AGENT = "linidi-update-check"

TIMEOUT = 6.0


def parse_version(text: str) -> tuple[int, ...] | None:
    """A tag like "v0.2.1" as (0, 2, 1), or None if it is not one.

    Leading "v" optional, trailing anything ignored, so "v1.0" and "1.0.0"
    and "v1.0.0-rc1" all read as the release they belong to. A tag that is
    not a version at all -- a branch point, somebody's bookmark -- returns
    None and is skipped rather than sorted into the middle of the list.
    """
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def newest_tag(url: str = TAGS_URL, timeout: float = TIMEOUT) -> str | None:
    """The highest version tag on the repository, or None if there are none.

    Sorted here rather than trusted in the order GitHub returns them, which
    is by creation and not by version -- a fix tagged on an older line would
    otherwise read as the newest thing there is.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        tags = json.load(resp)
    best, best_name = None, None
    for tag in tags:
        name = tag.get("name", "") if isinstance(tag, dict) else ""
        parsed = parse_version(name)
        if parsed is not None and (best is None or parsed > best):
            best, best_name = parsed, name
    return best_name


def check(current: str, url: str = TAGS_URL,
          timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Compare this build against the newest tag. Returns (newer, message).

    Every outcome is a message, including the failures: a button that reports
    nothing when the network is down is indistinguishable from one that
    reports good news, and this one is pressed precisely by somebody who
    wants an answer.
    """
    here = parse_version(current)
    try:
        latest = newest_tag(url, timeout)
    except urllib.error.HTTPError as exc:
        return False, (f"GitHub answered {exc.code} when asked for the "
                       f"version list.")
    except urllib.error.URLError as exc:
        return False, f"Could not reach GitHub: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return False, f"Could not reach GitHub: {exc}"
    except (ValueError, json.JSONDecodeError):
        return False, "GitHub's answer was not in the expected shape."

    if latest is None:
        return False, ("No versions have been tagged yet, so there is "
                       "nothing to compare this build against.")
    there = parse_version(latest)
    if here is None:
        return True, (f"The newest tagged version is {latest}. This build "
                      f"does not say which version it is, so they cannot be "
                      f"compared.")
    if there > here:
        return True, f"{latest} is available. This is {current}."
    if there < here:
        return False, (f"This build ({current}) is newer than anything "
                       f"tagged ({latest}).")
    return False, f"This is the newest tagged version ({current})."
