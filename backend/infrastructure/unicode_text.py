"""Make extracted external text valid Unicode without altering source bytes."""
import re

_SURROGATES = re.compile('[\ud800-\udfff]')

#: The one full-width form that actually breaks money reading: a separator
#: *between digits* (７４，８００).  Folding the character globally would be
#: wrong -- Chinese notices use ， as ordinary sentence punctuation
#: ("成交公告，预算 12000 元"), and rewriting that changes citations that were
#: never ambiguous.  Full-width digits need no help: ``\d``, ``float()`` and
#: ``parse_amount`` already accept them.
_NUMBER_SEPARATOR = re.compile(r'(?<=\d)([，．])(?=\d)')


def repair_surrogates(text: str) -> tuple[str, int]:
    if not _SURROGATES.search(text):
        return text, 0
    repaired = text.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='replace')
    return repaired, repaired.count('\ufffd') - text.count('\ufffd')


def fold_width(text: str) -> str:
    """Fold the full-width thousands separator OCR prints between digits.

    An OCR'd award table states ``７４，８００``.  ``parse_amount`` rejects that
    outright, and the prose amount patterns in ``procurement_rules`` /
    ``price_leads`` do worse: ``\\d[\\d,]*`` splits it into ``７４`` and ``８００``,
    which is a wrong number rather than a missing one.  Only the digit-adjacent
    separator is rewritten, so prose punctuation and the excerpt of an ordinary
    notice stay byte-identical; the snapshot keeps the published bytes either
    way.
    """
    if not text:
        return text
    return _NUMBER_SEPARATOR.sub(lambda match: ',' if match.group(1) == '，' else '.', text)
