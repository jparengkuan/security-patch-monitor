"""Data sources. ``default_sources`` is the single place where the active sources are listed."""

from patch_monitor.sources.base import Source, SourceContext
from patch_monitor.sources.drupal import DrupalAdvisorySource, DrupalPsaSource, DrupalReleaseWindowSource
from patch_monitor.sources.endoflife import EndOfLifeSource
from patch_monitor.sources.nextjs import NextJsAdvisorySource
from patch_monitor.sources.node import NodeSecuritySource
from patch_monitor.sources.php import PhpNewsSource, PhpReleaseEstimateSource


def default_sources() -> list[Source]:
    return [
        DrupalPsaSource(),
        DrupalAdvisorySource.core(),
        DrupalAdvisorySource.contrib(),
        DrupalReleaseWindowSource(),
        PhpNewsSource(),
        PhpReleaseEstimateSource(),
        NodeSecuritySource(),
        NextJsAdvisorySource(),
        EndOfLifeSource(),
    ]


__all__ = ["Source", "SourceContext", "default_sources"]
