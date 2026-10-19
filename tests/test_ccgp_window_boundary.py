"""The CCGP date window must not be widened past what the site answers honestly.

Measured 2026-09-23 against the live site: a window of at most 366 days returns
the true set, while 367, 370, 400 and 630 days all answer with the same 921 rows
where 365 days answers 1526 -- and the loss is real, not a change of scope (a
model matching 2 rows inside the year matches 1). Nothing raises, so the only
place this can be caught is the request builder.
"""
import urllib.parse
from datetime import datetime

from backend.infrastructure.search_providers import (
    CCGP_TRUTHFUL_WINDOW_DAYS,
    CCGPSearchProvider,
)


def _window(years_window):
    url = CCGPSearchProvider(years_window=years_window)._search_url("离心机")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    start = datetime.strptime(params["start_time"][0], "%Y:%m:%d")
    end = datetime.strptime(params["end_time"][0], "%Y:%m:%d")
    return (end - start).days


def test_the_default_window_stays_inside_the_measured_boundary():
    assert _window(1.0) == 365
    assert _window(1.0) <= CCGP_TRUTHFUL_WINDOW_DAYS


def test_asking_for_more_days_does_not_silently_narrow_the_search():
    """Widening used to reach the lossy branch, which returns fewer rows."""
    for asked in (1.01, 1.5, 2.0, 10.0):
        assert _window(asked) == CCGP_TRUTHFUL_WINDOW_DAYS, asked
    assert _window(2.0) > _window(1.0), "a wider request must not come back narrower"


def test_the_measured_boundary_is_pinned_where_it_was_measured():
    """The margin over the default is one day; changing it needs a re-measure."""
    assert CCGP_TRUTHFUL_WINDOW_DAYS == 366
